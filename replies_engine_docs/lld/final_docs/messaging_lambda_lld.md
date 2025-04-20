# LLD: Messaging Lambda (Stage 2 Batch Processing)

## 1. Purpose

This document details the low-level design for the `messaging_lambda` function (Stage 2). This Lambda is responsible for processing batches of messages for a specific conversation after a defined delay, ensuring idempotent execution, merging message content, sending the final payload downstream, and cleaning up temporary state.

## 2. Context within Flow & Trigger

*   **Trigger:** AWS SQS (Standard Queue - e.g., `WhatsAppQueue`, `SMSQueue`, `EmailQueue` as configured in `sqs_queues_lld.md`).
*   **Triggering Event:** A single JSON message becomes visible on the SQS queue after its message-specific `DelaySeconds` (set by the `StagingLambda`) expires. The message contains `{ "conversation_id": "..." }`.
*   **Preceding Steps:**
    *   `StagingLambda` (Stage 1) has successfully validated one or more incoming messages for a conversation.
    *   The context for each message has been written to the `conversations-stage` DynamoDB table.
    *   A lock entry has been successfully placed in the `conversations-trigger-lock` DynamoDB table (with TTL) by the *first* message handler for the batch window.
    *   The trigger message has been sent to the appropriate **Channel Queue** (e.g., `WhatsAppQueue`) with `DelaySeconds=W` by that first handler.
    *   The SQS message delay (`W` seconds) has elapsed.

## 3. Detailed Processing Steps (`messaging_lambda`)

1.  **Extract `conversation_id`:** Parse the incoming SQS message body to retrieve the `conversation_id`.
2.  **Acquire Processing Lock (Idempotency):**
    *   **Action:** Issue a conditional `UpdateItem` on the **main `ConversationsTable`**:
        ```text
        SET conversation_status = :processing_reply
        ```
        with the `ConditionExpression` `conversation_status <> :processing_reply`.
    *   **On Success (Lock Acquired):** Continue.
    *   **On Failure (`ConditionalCheckFailedException`):** Another Lambda instance is already working. **Return success** to SQS so the trigger message is deleted.
3.  **Query Staging Table (Consistent Read):**
    *   **Action:** `Query` the `conversations-stage` table **with `ConsistentRead=True`** to guarantee visibility of the most recent fragment writes.
    *   **Key:** `PK = conversation_id`.
    *   **Sort:** In‑memory sort by `(received_at, message_sid)` to reconstruct arrival order.
4.  **Handle Empty Batch (Edge Case):** If no items are returned, release the processing lock (set `conversation_status` back to its previous value) and exit successfully.
5.  **Merge Batch Fragments:**
    *   Concatenate all `body` attributes using newlines:
        ```python
        combined_body = "\n".join(item['body'] for item in items)
        ```
    *   Take `primary_channel` from the first item (all items share the same value).
6.  **Hydrate Canonical Conversation Row:**
    *   **Action:** Strongly‑consistent `GetItem` on the Conversations table using `PK = primary_channel` + `SK = conversation_id`.
7.  **Atomic Append + Status Update:**
    *   **Action:** Single `UpdateItem` on the Conversations table that:
        1. `list_append`s a new **user** message map onto `messages`.
        2. Keeps the processing lock by ensuring the `ConditionExpression` `conversation_status = :processing_reply` (so retries don't double‑append).
        3. Updates `updated_at`.
        Example snippet (pseudo‑JSON):
        ```text
        SET
          messages = list_append(if_not_exists(messages, :empty), :new_msg),
          updated_at = :now
        CONDITION conversation_status = :processing_reply
        ```
8.  **Fetch Updated Record for Downstream:** Immediately `GetItem` (consistent) to retrieve the now‑updated conversation record. This becomes the `context_object` for the next processor / AI call.
9.  **Send To Downstream Service/Queue:** Package the updated record (or a trimmed version) and send to the appropriate target (AI queue, next Lambda, etc.). Handle SQS/API errors.
10. **Cleanup Staging & Trigger‑Lock (Post‑Success):**
    *   Only **after** the downstream send **and** any final status update succeed:
        *   `BatchWriteItem` deletes the processed items from `conversations-stage`.
        *   Delete the corresponding row from `conversations-trigger-lock`.
11. **Release Processing Lock:** Update `conversation_status` back to an idle value (e.g., `reply_sent` or `queued_for_ai`).
12. **Lambda Success:** Return success so SQS deletes the trigger message.

## 4. Error Handling Considerations

*   **Failure After Lock (Step 2):** Implement `try...finally` blocks. If an error occurs *after* acquiring the processing lock (Step 2) but *before* successfully releasing it (Step 9), the `finally` block should attempt to release the lock (setting status to an error state like `'processing_error'`) to prevent indefinite locking.
*   **SQS Send Failure (Step 6):** Decide on retry strategy. Should the Lambda fail (allowing SQS to retry the entire batch processing), or should it log the error and proceed with cleanup (potentially orphaning the message)? Failing the Lambda is often preferred initially.
*   **Cleanup Failures (Steps 7, 8):** Log errors. Staging table items might be reprocessed on retry (idempotency lock should prevent issues), and TTL will eventually clean up both staging and lock tables. Releasing the main lock (Step 9) is the most critical cleanup step.

## 5. Idempotency

*   The primary idempotency mechanism is the processing lock acquired in Step 2. If the lock fails, the function exits early, preventing duplicate processing runs initiated by the *same* SQS trigger message (e.g., if the Lambda timed out previously after acquiring the lock but before finishing).
*   Deduplication based on `message_sid` (checking if a message already exists in the main conversation history *before* appending) should ideally happen further downstream (e.g., when writing the final history to the `ConversationsTable`) or can be added as a secondary check here if needed. 