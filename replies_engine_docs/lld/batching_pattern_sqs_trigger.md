# Batching Pattern: SQS Delay Trigger + Holding Table

## 1. Problem Statement

Standard SQS `DelaySeconds` applies per-message delays independently. This prevents guaranteeing that all messages arriving from a user within a specific time window (e.g., 10 seconds of each other) are processed together as a single batch before potentially triggering downstream actions like AI interaction. We need a mechanism to reliably collect all parts of a potentially multi-message reply within a defined window before processing begins.

## 2. Goal

- Implement a consistent batching window (e.g., **W = 10 seconds**).
- Processing for a conversation should only begin once **W** seconds have passed since the *first* message for that batch arrived (alternative: trigger delay resets - more complex). This example uses a fixed delay from the first trigger.
- All messages received within that window should be processed together as a single batch.
- Keep the latency before processing starts reasonably low (target ~10s delay).

## 3. Proposed Architecture

This pattern uses an SQS delay queue to trigger batch processing after a window, combined with a holding table.

### 3.1 Components

1.  **API Gateway:** Receives the initial webhook request.
2.  **`webhook_handler` Lambda (Stage 1):**
    *   Triggered by API Gateway.
    *   **Responsibilities:**
        *   Receives raw `event`.
        *   Extracts minimal identifiers (sender/recipient).
        *   Writes raw message data + arrival timestamp to the Holding Table.
        *   **(Crucial Concurrency Handling):** Attempts an atomic operation (e.g., conditional DynamoDB update on a *separate state table* or attributes in the main table) to check if a processing trigger is already pending for this conversation within the window.
        *   **If no trigger is pending:** Sends a *single trigger message* (containing `conversation_id`) to the SQS Trigger Delay Queue with `DelaySeconds=W` (e.g., 10s) AND atomically sets the "trigger pending" state.
        *   **If a trigger *is* already pending:** Does *not* send another SQS trigger message.
        *   Immediately returns `200 OK` (e.g., empty TwiML) to acknowledge receipt.
3.  **Holding DynamoDB Table (NEW):**
    *   A separate table for temporary storage of *pending* raw messages.
    *   Schema: `conversation_id` (PK), `arrival_timestamp` (SK or attribute), `raw_message_data`.
4.  **Trigger State Store (Implicit or Explicit):**
    *   Needed for the atomic check in the `webhook_handler`. Could be attributes on the main `ConversationsTable` or a dedicated small table. Stores whether a trigger is pending for a `conversation_id` and potentially when the window expires.
5.  **SQS Trigger Delay Queue (NEW):**
    *   A standard SQS queue configured with `DelaySeconds=W` (e.g., 10s).
    *   Receives *only* the trigger messages (one per batch window).
6.  **`BatchProcessorLambda` (NEW Logic Location):**
    *   **Responsibility:** Gathers, processes, and routes the batch.
    *   Triggered by messages becoming visible on the SQS Trigger Delay Queue.
    *   **Logic:**
        *   Receives a trigger message containing `conversation_id`.
        *   Queries the Holding Table to gather *all* pending messages for that `conversation_id`.
        *   **Parses** the raw messages.
        *   **Validates** against the main `ConversationsTable`. Handles validation errors.
        *   **Determines Routing**.
        *   **(Optional but Recommended):** Acquires lock (`conversation_status='processing_reply'`) on main `ConversationsTable` before sending downstream.
        *   **Sends** the processed batch to the target SQS Queue (Handoff, WhatsApp, etc.).
        *   **Deletes** processed messages from the Holding Table.
        *   **Clears** the "trigger pending" state in the Trigger State Store.
        *   Releases lock (if acquired).
7.  **Conversations DynamoDB Table:** Used by the `BatchProcessorLambda` for validation.
8.  **Target SQS Queues (WhatsApp, SMS, Email, Handoff):** Receive the processed batches. Do not need `DelaySeconds`.

### 3.2 Flow Diagram

```mermaid
sequenceDiagram
    participant Twilio
    participant APIGW as API Gateway
    participant HandlerLambda as Webhook Handler (Stage 1)
    participant HoldingDB as Holding DynamoDB
    participant StateDB as Trigger State Store
    participant SQS_Delay as SQS Trigger Delay Queue (W=10s)
    participant ProcessorLambda as Batch Processor Lambda (Stage 2)
    participant ConversationsDB as Conversations DynamoDB
    participant SQS_Target as Target SQS Queues

    Twilio->>+APIGW: POST /webhook (Msg M1)
    APIGW->>+HandlerLambda: Trigger Event M1
    HandlerLambda->>+HoldingDB: Write M1 Data
    HandlerLambda->>+StateDB: Check/Set Trigger State (Atomic)
    alt Atomic Update Succeeds (First Msg)
        StateDB-->>-HandlerLambda: State Set OK
        HandlerLambda->>+SQS_Delay: Send Trigger(ConvX) w/ Delay=10s
    else Atomic Update Fails (Trigger already pending)
        StateDB-->>-HandlerLambda: State Exists
    end
    HandlerLambda-->>-APIGW: HTTP 200 OK TwiML
    APIGW-->>-Twilio: HTTP 200 OK

    Twilio->>+APIGW: POST /webhook (Msg M2 - ConvX, within 10s)
    APIGW->>+HandlerLambda: Trigger Event M2
    HandlerLambda->>+HoldingDB: Write M2 Data
    HandlerLambda->>+StateDB: Check/Set Trigger State (Atomic)
    StateDB-->>-HandlerLambda: State Exists (Fails)
    HandlerLambda-->>-APIGW: HTTP 200 OK TwiML
    APIGW-->>-Twilio: HTTP 200 OK

    %% After 10 second delay on SQS %%
    SQS_Delay->>+ProcessorLambda: Trigger(ConvX) becomes visible
    ProcessorLambda->>+HoldingDB: Get ALL pending msgs for ConvX
    HoldingDB-->>-ProcessorLambda: Raw Batch (M1, M2)
    ProcessorLambda->>ProcessorLambda: Parse Batch
    ProcessorLambda->>+ConversationsDB: Validate Existence & Rules
    ConversationsDB-->>-ProcessorLambda: Validation OK
    ProcessorLambda->>ProcessorLambda: Determine Routing
    ProcessorLambda->>+SQS_Target: Send Processed Batch
    ProcessorLambda->>+HoldingDB: Delete Processed Msgs (M1, M2)
    ProcessorLambda->>+StateDB: Clear Trigger State for ConvX
    ProcessorLambda-->>-SQS_Delay: (Ack Trigger Msg)

```

## 4. Benefits

*   **Guaranteed Batching Window:** Reliably groups messages based on the SQS delay.
*   **Managed Delay:** Leverages SQS `DelaySeconds` for the waiting period.
*   **Fast Initial Response:** `webhook_handler` remains fast, acknowledging Twilio quickly.
*   **Decoupled Processing:** Clear separation between receiving/staging and batch processing.

## 5. Trade-offs & Considerations

*   **Complexity:** Requires careful implementation of the atomic "trigger pending" check in the `webhook_handler` to prevent duplicate SQS trigger messages. This is the main challenge.
*   **Extra Resources:** Needs a Holding Table and potentially a separate state store/attributes, plus the SQS Trigger Queue.
*   **Processor Idempotency:** While the atomic check aims to prevent duplicate triggers, the `BatchProcessorLambda` should still ideally be idempotent (or use locking) as a defense-in-depth measure against potential SQS redeliveries or race conditions.
*   **Cleanup:** Robust logic needed in the `BatchProcessorLambda` to clear the Holding Table and the trigger state, even if processing fails partway through.
