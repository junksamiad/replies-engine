# End-to-End Flow: SQS Delay Trigger + Holding Table (Integrated Post-Validation)

## 1. Overview

This document describes the end-to-end processing flow for an incoming webhook reply (e.g., from Twilio WhatsApp) using the SQS Delay Trigger + Holding Table pattern for message batching. This specific flow integrates the batching logic *after* the initial parsing, validation, and routing determination steps have successfully completed within the `webhook_handler` Lambda.

## 2. Core Components & Assumptions

*   **Pattern:** SQS Delay Trigger + Holding Table.
*   **Batching Integration Point:** Batching logic occurs *after* successful validation and routing in `webhook_handler`.
*   **Trigger Deduplication:** Uses a TTL Flag in a dedicated `ConversationBatchState` DynamoDB Table (Suggestion 1).
*   **Processor Idempotency:** Uses `conversation_status` lock in the main `ConversationsTable` (Suggestion 3A).
*   **Batching Window (W):** 10 seconds (configured on the SQS Trigger Delay Queue).
*   **Holding Table:** Stores the actual content (`context_object`, `target_queue_url`) of *all* incoming messages (M1, M2, etc.) within a batch window.
*   **`ConversationBatchState` Table:** Acts *only* as a lock/flag using DynamoDB TTL to indicate "a trigger message has already been scheduled for this conversation's current batch window". It prevents sending duplicate trigger messages but does *not* store message content.

## 3. Detailed Flow Steps

### 3.1. Stage 1: Webhook Reception & Initial Handling (`webhook_handler` Lambda)

1.  **Webhook Arrival:** Twilio sends a `POST` request to the relevant API Gateway endpoint (e.g., `/whatsapp`).
2.  **API Gateway:** Validates the request (Signature, Schema, Rate Limits) and triggers the `webhook_handler` Lambda via `AWS_PROXY` integration.
3.  **Parse Event:** The Lambda calls `parsing_utils.create_context_object` to parse the `event`, determine `channel_type`, extract fields, map keys to `snake_case`, and perform initial field validation, creating the `context_object`.
    *   *On Failure:* Determine final error response (likely 200 OK TwiML for Twilio) using `_determine_final_error_response` and return.
4.  **Validate Conversation Existence:** Calls `validation.check_conversation_exists` to query the main `ConversationsTable` GSI (filtering for `task_complete=0`), retrieve the latest active record, and update the `context_object`.
    *   *On Failure:* Handle `CONVERSATION_NOT_FOUND` (200 TwiML for Twilio) or transient DB errors (raise Exception) using `_determine_final_error_response`.
5.  **Validate Conversation Rules:** Calls `validation.validate_conversation_rules` to check `project_status`, `allowed_channels`, and `conversation_status != 'processing_reply'`.
    *   *On Failure:* Handle `PROJECT_INACTIVE`, `CHANNEL_NOT_ALLOWED`, `CONVERSATION_LOCKED` using `_determine_final_error_response` (specific TwiML for `LOCKED` on Twilio).
6.  **Determine Routing:** Calls `routing.determine_target_queue` to get the `target_queue_url`.
    *   *On Failure:* Determine final error response using `_determine_final_error_response`.
7.  **Write to Holding Table:** Save the validated `context_object` and the determined `target_queue_url` to the **Holding DynamoDB Table**, keyed by `conversation_id` and `arrival_timestamp` (or `message_sid`). This happens for *every* valid incoming message (M1, M2, etc.).
8.  **Attempt Trigger Scheduling Lock:** Perform a conditional `PutItem` to the **`ConversationBatchState` DynamoDB Table** for the `conversation_id` with an `expires_at` TTL attribute (e.g., `now + W + buffer`) and `ConditionExpression='attribute_not_exists(conversation_id)'`.
9.  **Send SQS Trigger (First Message Only):** If the `PutItem` in step 8 succeeded (meaning no trigger is currently scheduled for this `conversation_id`), send a **single trigger message** (`{ 'conversation_id': conv_id }`) to the **SQS Trigger Delay Queue** with `DelaySeconds = W` (e.g., 10s). If the `PutItem` failed (trigger already scheduled by a previous message like M1), do nothing here.
10. **Acknowledge Webhook (ACK):** Return `200 OK` (empty TwiML for Twilio, standard JSON for others) via `response_builder` to confirm receipt to the webhook sender.

### 3.2. Stage 2: SQS Delay & Batch Processing (`BatchProcessorLambda`)

11. **SQS Delay:** The trigger message (sent only once per batch window) waits invisibly in the SQS Trigger Delay Queue for `W` seconds.
12. **Lambda Invocation:** SQS triggers the `BatchProcessorLambda` with the trigger message after the delay expires.
13. **Extract `conversation_id`:** Get the ID from the SQS message body.
14. **Acquire Processing Lock (Idempotency):** Attempt conditional `UpdateItem` on the **main `ConversationsTable`** to `SET conversation_status = 'processing_reply'` where `conversation_status <> 'processing_reply'`.
    *   *On Failure (Lock Held):* Log a warning and exit successfully (allowing SQS to potentially retry later if needed, but preventing duplicate processing). SQS message should be deleted upon successful exit.
15. **Query Holding Table:** Retrieve *all* items (stored `context_object`s and `target_queue_url`s) for the `conversation_id` from the Holding Table.
16. **Handle Empty Batch:** If no items are found (e.g., previous run cleaned up, or race condition), log a warning, release the lock (step 14) if acquired, and exit successfully.
17. **Process/Merge Batch:** Combine the data from the retrieved context objects (e.g., concatenate message bodies). Determine the final `target_queue_url` (usually the same for all parts of a batch).
18. **Send to Target SQS:** Send the final processed payload (containing the merged data) to the determined `target_queue_url`.
19. **Cleanup Holding Table:** Delete all the processed items for this `conversation_id` from the Holding Table.
20. **Cleanup Trigger State (Optional but Recommended):** Explicitly delete the corresponding item from the `ConversationBatchState` table. While TTL will eventually remove it, explicit deletion prevents potential edge cases if the clock sync is off or TTL processing is delayed.
21. **Release Processing Lock:** Update `conversation_status` on the main `ConversationsTable` back to an appropriate idle state (e.g., `'queued_for_ai'`, `'awaiting_agent'`).
22. **Lambda Success:** Exit successfully. SQS automatically deletes the trigger message from the Delay Queue upon successful completion.

## 4. Flow Diagram (Illustrative)

```mermaid
sequenceDiagram
    participant Twilio
    participant APIGW as API Gateway
    participant HandlerLambda as Webhook Handler (Stage 1)
    participant ConvDB as Conversations DynamoDB
    participant HoldingDB as Holding DynamoDB
    participant StateDB as ConversationBatchState DB (TTL Lock)
    participant SQS_Delay as SQS Trigger Delay Queue (W=10s)
    participant ProcessorLambda as Batch Processor Lambda (Stage 2)
    participant SQS_Target as Target SQS Queues

    %% --- Message M1 Arrives ---
    Twilio->>+APIGW: POST /webhook (Msg M1)
    APIGW->>+HandlerLambda: Trigger Event M1
    HandlerLambda->>HandlerLambda: Parse, Validate, Route OK
    HandlerLambda->>+HoldingDB: Write Context M1
    HandlerLambda->>+StateDB: Attempt Trigger Lock (Put w/ TTL, Cond=NotExists)
    StateDB-->>-HandlerLambda: OK, State Set (Lock Acquired)
    HandlerLambda->>+SQS_Delay: Send Trigger(ConvX) Delay=10s
    HandlerLambda-->>-APIGW: HTTP 200 OK TwiML
    APIGW-->>-Twilio: HTTP 200 OK

    %% --- Message M2 Arrives (within W seconds) ---
    Twilio->>+APIGW: POST /webhook (Msg M2)
    APIGW->>+HandlerLambda: Trigger Event M2
    HandlerLambda->>HandlerLambda: Parse, Validate, Route OK
    HandlerLambda->>+HoldingDB: Write Context M2
    HandlerLambda->>+StateDB: Attempt Trigger Lock (Put w/ TTL, Cond=NotExists)
    StateDB-->>-HandlerLambda: FAIL (Already Exists)
    HandlerLambda-->>-APIGW: HTTP 200 OK TwiML
    APIGW-->>-Twilio: HTTP 200 OK

    %% --- After SQS Delay ---
    SQS_Delay->>+ProcessorLambda: Trigger(ConvX) becomes visible
    ProcessorLambda->>+ConvDB: Attempt Processing Lock (Update Status, Cond=NotLocked)
    alt Lock Acquired
        ConvDB-->>-ProcessorLambda: Lock OK
        ProcessorLambda->>+HoldingDB: Get ALL pending msgs for ConvX (M1, M2)
        HoldingDB-->>-ProcessorLambda: Context Batch [M1, M2]
        ProcessorLambda->>ProcessorLambda: Process/Merge Batch
        ProcessorLambda->>+SQS_Target: Send Final Processed Batch
        ProcessorLambda->>+HoldingDB: Delete Processed Msgs (M1, M2)
        ProcessorLambda->>+StateDB: Delete Trigger State ConvX (Optional Cleanup)
        ProcessorLambda->>+ConvDB: Release Processing Lock (Update Status)
        ConvDB-->>-ProcessorLambda: OK
    else Lock Failed (Other processor active)
        ConvDB-->>-ProcessorLambda: Lock Fail
        ProcessorLambda->>ProcessorLambda: Log & Exit (ACK SQS Msg)
    end
    ProcessorLambda-->>-SQS_Delay: (Return Success -> SQS Deletes Msg)

``` 