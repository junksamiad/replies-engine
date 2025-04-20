# End-to-End Flow: SQS Delay Trigger + Holding Table (Integrated Post-Validation)

## 1. Overview

This document describes the end-to-end processing flow for an incoming webhook reply (e.g., from Twilio WhatsApp) using the SQS Delay Trigger + Holding Table pattern for message batching. This specific flow integrates the batching logic *after* the initial parsing, validation, and routing determination steps have successfully completed within the `webhook_handler` Lambda.

## 2. Assumptions

*   **Batching Pattern:** Option C (SQS Delay Trigger + Holding Table).
*   **Integration Point:** Batching logic occurs *after* successful validation and routing in `webhook_handler`.
*   **Trigger Deduplication:** Uses TTL Flag in `ConversationBatchState` table (Suggestion 1).
*   **Processor Idempotency:** Uses `conversation_status` lock in main `ConversationsTable` (Suggestion 3A).
*   **Batching Window (W):** 10 seconds (configured on SQS Trigger Delay Queue).

## 3. Detailed Flow Steps

1.  **Webhook Arrival:** Twilio sends a `POST` request to the relevant API Gateway endpoint (e.g., `/whatsapp`).
2.  **API Gateway:** Validates the request (Signature, Schema, Rate Limits) and triggers the `webhook_handler` Lambda via `AWS_PROXY` integration.
3.  **`webhook_handler` Lambda Invocation:**
    a.  **Parse Event:** Calls `parsing_utils.create_context_object` to parse the `event`, determine `channel_type`, extract fields, and map keys to `snake_case`, creating the `context_object`.
    b.  **Handle Parsing Failure:** If `create_context_object` returns `None`, determine the final error response (likely 200 OK TwiML for Twilio) using `_determine_final_error_response` and return it.
    c.  **Validate Existence:** Calls `validation.check_conversation_exists` to query the main `ConversationsTable` GSI (filtering for `task_complete=0`), retrieve the latest active record, and update the `context_object`.
    d.  **Handle Existence Failure:** If no active record found (`CONVERSATION_NOT_FOUND`) or a DB error occurs, determine the final error response (200 TwiML for `NOT_FOUND` on Twilio, raise Exception for transient DB errors) using `_determine_final_error_response` and return/raise.
    e.  **Validate Rules:** Calls `validation.validate_conversation_rules` to check `project_status`, `allowed_channels`, and `conversation_status != 'processing_reply'` based on the updated `context_object`.
    f.  **Handle Rule Failure:** If validation fails (`PROJECT_INACTIVE`, `CHANNEL_NOT_ALLOWED`, `CONVERSATION_LOCKED`), determine the final error response (specific TwiML for `LOCKED` on Twilio, 200 TwiML for others) using `_determine_final_error_response` and return it.
    g.  **Determine Routing:** Calls `routing.determine_target_queue` to get the `target_queue_url` (e.g., `WHATSAPP_QUEUE_URL` or `HANDOFF_QUEUE_URL`).
    h.  **Handle Routing Failure:** If `determine_target_queue` fails (e.g., unknown channel), determine the final error response using `_determine_final_error_response` and return it.
    i.  **Write to Holding Table:** Save the validated `context_object` and the determined `target_queue_url` to the **Holding DynamoDB Table**, keyed by `conversation_id` and `arrival_timestamp` (or `message_sid`).
    j.  **Attempt Trigger Lock (TTL Flag):** Perform a conditional `PutItem` to the **`ConversationBatchState` DynamoDB Table** for the `conversation_id` with an `expires_at` TTL attribute (now + ~15s) and `ConditionExpression='attribute_not_exists(conversation_id)'`.
    k.  **Send SQS Trigger (If Lock Acquired):** If the `PutItem` in step (j) succeeded, send a **single trigger message** (`{ 'conversation_id': conv_id }`) to the **SQS Trigger Delay Queue** with `DelaySeconds = 10`.
    l.  **Acknowledge Webhook:** Return `200 OK` (empty TwiML for Twilio, standard JSON for others) via `response_builder`.
4.  **SQS Delay:** The trigger message waits invisibly in the SQS Trigger Delay Queue for 10 seconds.
5.  **`BatchProcessorLambda` Invocation:** SQS triggers the `BatchProcessorLambda` with the trigger message after the delay expires.
6.  **`BatchProcessorLambda` Execution:**
    a.  **Extract `conversation_id`:** Get the ID from the SQS message.
    b.  **(Idempotency Check - Lock):** Attempt conditional `UpdateItem` on the **main `ConversationsTable`** to `SET conversation_status = 'processing_reply'` where `conversation_status <> 'processing_reply'`. If this fails (lock held), log warning and exit successfully (deleting SQS message).
    c.  **Query Holding Table:** Retrieve *all* items (stored `context_object`s) for the `conversation_id` from the Holding Table.
    d.  **Handle Empty Batch:** If no items are found (edge case, maybe previous run cleaned up), log warning, release lock, exit successfully.
    e.  **Process/Merge Batch:** Combine the data from the retrieved context objects. Determine the final `target_queue_url` from the retrieved data.
    f.  **Send to Target SQS:** Send the final processed payload to the `target_queue_url`.
    g.  **Cleanup Holding Table:** Delete the processed items from the Holding Table.
    h.  **Cleanup Trigger State:** Delete the corresponding item from the `ConversationBatchState` table (although TTL will eventually get it, explicit deletion is cleaner).
    i.  **Release Lock:** Update `conversation_status` on the main `ConversationsTable` back to an appropriate idle state (e.g., `'queued_for_ai'`).
    j.  **Lambda Success:** Exit successfully. SQS deletes the trigger message.

## 4. Flow Diagram

```mermaid
sequenceDiagram
    participant Twilio
    participant APIGW as API Gateway
    participant HandlerLambda as Webhook Handler (Stage 1)
    participant ConvDB as Conversations DynamoDB
    participant HoldingDB as Holding DynamoDB
    participant StateDB as Trigger State Store
    participant SQS_Delay as SQS Trigger Delay Queue (W=10s)
    participant ProcessorLambda as Batch Processor Lambda (Stage 2)
    participant SQS_Target as Target SQS Queues

    Twilio->>+APIGW: POST /webhook (Msg M1)
    APIGW->>+HandlerLambda: Trigger Event M1
    HandlerLambda->>HandlerLambda: Parse (create_context_object)
    HandlerLambda->>+ConvDB: Validate Existence (check_conversation_exists)
    ConvDB-->>-HandlerLambda: Record Found, Context Updated
    HandlerLambda->>HandlerLambda: Validate Rules (validate_rules)
    HandlerLambda->>HandlerLambda: Determine Routing (determine_target_queue)
    HandlerLambda->>+HoldingDB: Write Validated Context M1
    HandlerLambda->>+StateDB: Attempt Trigger Lock (Atomic Put w/ TTL)
    alt Lock Succeeded
        StateDB-->>-HandlerLambda: OK, State Set
        HandlerLambda->>+SQS_Delay: Send Trigger(ConvX) Delay=10s
    else Lock Failed
        StateDB-->>-HandlerLambda: Fail (Already Exists)
    end
    HandlerLambda-->>-APIGW: HTTP 200 OK TwiML
    APIGW-->>-Twilio: HTTP 200 OK

    %% Optional: M2 arrives within 10s %%
    Twilio->>+APIGW: POST /webhook (Msg M2)
    APIGW->>+HandlerLambda: Trigger Event M2
    HandlerLambda->>HandlerLambda: Parse, Validate, Route (all pass)
    HandlerLambda->>+HoldingDB: Write Validated Context M2
    HandlerLambda->>+StateDB: Attempt Trigger Lock (Atomic Put w/ TTL)
    StateDB-->>-HandlerLambda: Fail (Already Exists)
    HandlerLambda-->>-APIGW: HTTP 200 OK TwiML
    APIGW-->>-Twilio: HTTP 200 OK

    %% After SQS Delay %%
    SQS_Delay->>+ProcessorLambda: Trigger(ConvX) becomes visible
    ProcessorLambda->>+ConvDB: Attempt Processing Lock (Conditional Update)
    alt Lock Acquired
        ConvDB-->>-ProcessorLambda: Lock OK
        ProcessorLambda->>+HoldingDB: Get ALL pending msgs for ConvX
        HoldingDB-->>-ProcessorLambda: Context Batch (M1, M2)
        ProcessorLambda->>ProcessorLambda: Process/Merge Batch
        ProcessorLambda->>+SQS_Target: Send Processed Batch
        ProcessorLambda->>+HoldingDB: Delete Processed Msgs (M1, M2)
        ProcessorLambda->>+StateDB: Delete Trigger State (Optional)
        ProcessorLambda->>+ConvDB: Release Processing Lock (Update Status)
        ConvDB-->>-ProcessorLambda: OK
    else Lock Failed (Other processor active)
        ConvDB-->>-ProcessorLambda: Lock Fail
        ProcessorLambda->>ProcessorLambda: Log & Exit
    end
    ProcessorLambda-->>-SQS_Delay: (Ack Trigger Msg)

``` 