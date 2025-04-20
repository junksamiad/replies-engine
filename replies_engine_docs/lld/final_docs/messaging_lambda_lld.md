# LLD: Messaging Lambda (Stage 2 Batch Processing)

## 1. Purpose

This document details the low-level design for the `messaging_lambda` function (Stage 2). This Lambda is responsible for processing batches of messages for a specific conversation after a defined delay, ensuring idempotent execution, merging message content, sending the final payload downstream, and cleaning up temporary state.

## 2. Context within Flow & Trigger

*   **Trigger:** AWS SQS (Standard Queue - The "SQS Trigger Delay Queue" mentioned in the overall flow).
*   **Triggering Event:** A single JSON message becomes visible on the SQS queue after its `DelaySeconds` (set by the `webhook_handler`) expires. The message contains `{ "conversation_id": "..." }`.
*   **Preceding Steps:**
    *   `webhook_handler` Lambda (Stage 1) has successfully validated one or more incoming messages for a conversation.
    *   The context for each message has been written to the `conversations-stage` DynamoDB table.
    *   A lock entry has been successfully placed in the `conversations-trigger-lock` DynamoDB table (with TTL) by the *first* message handler for the batch window.
    *   The trigger message has been sent to the SQS Delay Queue by that first handler.
    *   The SQS delay (`W` seconds) has elapsed.

## 3. Detailed Processing Steps (`messaging_lambda`)

1.  **Extract `conversation_id`:** Parse the incoming SQS message body to retrieve the `conversation_id`.
2.  **Acquire Processing Lock (Idempotency):**
    *   **Action:** Attempt a conditional `UpdateItem` on the **main `ConversationsTable`**.
    *   **Details:** `SET conversation_status = 'processing_reply'` WHERE `conversation_status <> 'processing_reply'` (or potentially check against a list of valid 'idle' statuses).
    *   **On Success (Lock Acquired):** Proceed to the next step.
    *   **On Failure (Lock Held - `ConditionalCheckFailedException`):** Another instance of this Lambda (or a previous incomplete run) is processing this conversation. Log a warning (e.g., "Processing lock failed for conversation X, already held.") and **return success** to the SQS service. This acknowledges the *trigger message* (preventing infinite loops on the *same trigger*) without performing duplicate work.
3.  **Query Staging Table:**
    *   **Action:** Perform a `Query` operation on the `conversations-stage` DynamoDB table.
    *   **Details:** Use the `conversation_id` as the Partition Key. Retrieve *all* items matching the `conversation_id`. These items contain the staged `context_object`s and `target_queue_url`s for all messages received within the batch window.
    *   **Sort Results:** Sort the retrieved items chronologically based on the `received_at` timestamp or `message_sid` if it implies order.
4.  **Handle Empty Batch (Edge Case):**
    *   **Check:** If the query in Step 3 returns zero items. This could happen in rare race conditions or if a previous run partially failed after deleting stage items but before releasing the lock.
    *   **Action:** Log a warning (e.g., "No staged messages found for conversation X after acquiring lock."). Release the processing lock acquired in Step 2 (set `conversation_status` back to idle). Return success to SQS.
5.  **Process/Merge Batch:**
    *   **Action:** Iterate through the sorted list of staged items retrieved in Step 3.
    *   **Details:**
        *   Combine the `body` fields from each `context_object` into a single string (e.g., separated by newlines).
        *   Extract other relevant details from the context objects as needed (e.g., sender info from the first message, channel type).
        *   Determine the final `target_queue_url` (usually consistent across all items in a batch, take from the first item).
    *   **Output:** Create a final, merged `payload` object containing the combined message body and any other necessary context for downstream processing.
6.  **Send to Target SQS:**
    *   **Action:** Send the single, merged `payload` object (from Step 5) to the `target_queue_url` (determined in Step 5).
    *   **Details:** Use the standard SQS `SendMessage` API. Handle potential SQS errors.
7.  **Cleanup Staging Table:**
    *   **Action:** Delete the items processed in this batch from the `conversations-stage` table.
    *   **Details:** Use `BatchWriteItem` with `DeleteRequest` for efficiency, passing the keys (`conversation_id`, `message_sid`) of all items retrieved in Step 3. Handle potential DynamoDB errors.
8.  **Cleanup Trigger Lock State (Optional but Recommended):**
    *   **Action:** Explicitly delete the corresponding lock item from the `conversations-trigger-lock` table.
    *   **Details:** Use `DeleteItem` with the `conversation_id`. While TTL handles eventual cleanup, explicit deletion is cleaner and avoids potential edge cases with clock skew or TTL delays.
9.  **Release Processing Lock:**
    *   **Action:** Update the item in the **main `ConversationsTable`**.
    *   **Details:** Set the `conversation_status` back to an appropriate idle state (e.g., `'queued_for_ai'`, `'awaiting_agent'`, depending on the `target_queue_url`). Update `last_processed_at` timestamp if applicable.
10. **Lambda Success:**
    *   **Action:** Return success from the Lambda handler function.
    *   **Outcome:** SQS automatically deletes the processed trigger message from the SQS Delay Queue.

## 4. Error Handling Considerations

*   **Failure After Lock (Step 2):** Implement `try...finally` blocks. If an error occurs *after* acquiring the processing lock (Step 2) but *before* successfully releasing it (Step 9), the `finally` block should attempt to release the lock (setting status to an error state like `'processing_error'`) to prevent indefinite locking.
*   **SQS Send Failure (Step 6):** Decide on retry strategy. Should the Lambda fail (allowing SQS to retry the entire batch processing), or should it log the error and proceed with cleanup (potentially orphaning the message)? Failing the Lambda is often preferred initially.
*   **Cleanup Failures (Steps 7, 8):** Log errors. Staging table items might be reprocessed on retry (idempotency lock should prevent issues), and TTL will eventually clean up both staging and lock tables. Releasing the main lock (Step 9) is the most critical cleanup step.

## 5. Idempotency

*   The primary idempotency mechanism is the processing lock acquired in Step 2. If the lock fails, the function exits early, preventing duplicate processing runs initiated by the *same* SQS trigger message (e.g., if the Lambda timed out previously after acquiring the lock but before finishing).
*   Deduplication based on `message_sid` (checking if a message already exists in the main conversation history *before* appending) should ideally happen further downstream (e.g., when writing the final history to the `ConversationsTable`) or can be added as a secondary check here if needed. 