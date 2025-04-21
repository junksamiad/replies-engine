# LLD: Messaging Lambda (Stage 2 Batch Processing)

## 1. Purpose

This document details the low-level design for the `messaging_lambda` function (Stage 2). This Lambda is responsible for processing batches of messages for a specific conversation after a defined delay, ensuring idempotent execution, merging message content, sending the final payload downstream, and cleaning up temporary state.

## 2. Context within Flow & Trigger

*   **Trigger:** AWS SQS (Standard Queue - e.g., `WhatsAppQueue`, `SMSQueue`, `EmailQueue` as configured in `sqs_queues_lld.md`).
*   **Triggering Event:** A single JSON message becomes visible on the SQS queue after its message-specific `DelaySeconds` (set by the `StagingLambda`) expires. The message contains:
    ```json
    { 
      "conversation_id": "...", 
      "primary_channel": "..." 
    }
    ```
*   **Preceding Steps:**
    *   `StagingLambda` (Stage 1) has successfully validated one or more incoming messages for a conversation.
    *   The context for each message has been written to the `conversations-stage` DynamoDB table.
    *   A lock entry has been successfully placed in the `conversations-trigger-lock` DynamoDB table (with TTL) by the *first* message handler for the batch window.
    *   The trigger message (containing `conversation_id` and `primary_channel`) has been sent to the appropriate **Channel Queue** (e.g., `WhatsAppQueue`) with `DelaySeconds=W` by that first handler.
    *   The SQS message delay (`W` seconds) has elapsed.

## 3. Detailed Processing Steps (`messaging_lambda`)

*Initialize `context_object = {}` for the current SQS record.* 

1.  **Parse SQS Message & Extract IDs:**
    *   Parse the incoming SQS message body (JSON).
    *   Store the parsed dictionary (containing `conversation_id` and `primary_channel`) into `context_object['sqs_data']`.
    *   Extract `conversation_id` and `primary_channel` into local variables for easier access.
    *   *On Failure (No body, invalid JSON, missing keys):* Log error, add SQS message ID to `batchItemFailures`, continue to next record.
2.  **Acquire Processing Lock (Idempotency):**
    *   **Action:** Issue a conditional `UpdateItem` on the main `ConversationsTable` using `primary_channel` and `conversation_id` as the key.
    *   **Update:** Attempt `SET conversation_status = :processing_reply`.
    *   **Condition:** `attribute_not_exists(conversation_status) OR conversation_status <> :processing_reply`.
    *   **On Success (Lock Acquired):** Continue. Store lock status locally.
    *   **On Failure (`ConditionalCheckFailedException`):** Log warning (lock already held), continue to next record (treat as success for SQS message deletion).
    *   **On Failure (Other DB Error):** Log error, add SQS message ID to `batchItemFailures`, continue to next record.
3.  **Start SQS Heartbeat (Visibility Extension):**
    *   Instantiate `SQSHeartbeat(queue_url, receipt_handle, interval)` using the queue URL and message `receiptHandle`.
    *   Call `.start()`.
    *   Store the instance in `heartbeat` variable for cleanup in `finally` block.
    *   *On Failure:* Log error, continue processing without heartbeat.
4.  **Query Staging Table (Consistent Read):**
    *   **Action:** `Query` the `conversations-stage` table using `conversation_id` as PK, with `ConsistentRead=True`.
    *   **Result:** A list of staged item dictionaries (`staged_items`).
    *   *On Failure (DB Error):* Log error, add SQS message ID to `batchItemFailures`, continue to next record.
5.  **Handle Empty Batch (Edge Case):**
    *   If `staged_items` list is empty: Log warning (late trigger/cleanup issue), continue to next record (treat as success for SQS message deletion).
6.  **Merge Batch Fragments:**
    *   Sort `staged_items` list chronologically by `received_at`, using `message_sid` as a tie-breaker.
    *   Concatenate all `body` attributes from sorted items into `combined_body` string (newline separated).
    *   Extract `primary_channel` from `staged_items[0]` and verify it matches the `primary_channel` from the SQS message.
    *   Extract `message_sid` from `staged_items[0]` into `first_message_sid` variable.
    *   Store results in `context_object['staging_table_merged_data'] = {'combined_body': combined_body, 'first_message_sid': first_message_sid}`.
    *   *On Failure (Sort error, mismatching primary_channel, missing first_message_sid):* Log error, add SQS message ID to `batchItemFailures`, continue to next record.
7.  **Hydrate Canonical Conversation Row:**
    *   **Action:** `GetItem` on the `ConversationsTable` using `primary_channel` + `conversation_id` as key, with `ConsistentRead=True`.
    *   **Result:** The full conversation record dictionary.
    *   Store the result in `context_object['conversations_db_data']`.
    *   *On Failure (DB Error, Item Not Found):* Log error, add SQS message ID to `batchItemFailures`, continue to next record.
8.  **Fetch Secrets:**
    *   Extract `whatsapp_credentials_id` and `ai_api_key_reference` from `context_object['conversations_db_data']['channel_config']` and `...['ai_config']`.
    *   Call Secrets Manager service (`secrets_manager_service.get_secret`) for both references.
    *   Store the retrieved secret values (dictionaries) in `context_object['secrets'] = {'twilio': ..., 'openai': ...}`.
    *   *On Failure (Missing refs, Secret fetch error):* Log error, add SQS message ID to `batchItemFailures`, continue to next record.
9.  **Process with AI (e.g., OpenAI):**
    *   Extract necessary config/state from `context_object`: `openai_thread_id` (if exists), `assistant_id`, API key (from `context_object['secrets']['openai']`), `combined_body` (from `context_object['staging_table_merged_data']`).
    *   Call AI service function (`openai_service.process_message_with_ai`), passing the thread ID and new user message (`combined_body`).
    *   The service handles creating/using threads, adding messages, running the assistant.
    *   **Result:** Assistant's response content, updated `thread_id`, token counts.
    *   Store results in `context_object['open_ai_response'] = {'response_content': ..., 'thread_id': ..., 'prompt_tokens': ..., ...}`.
    *   *On Failure (AI API error, Timeout):* Log error, add SQS message ID to `batchItemFailures`, continue to next record.
10. **Send Reply via Channel Provider (e.g., Twilio):**
    *   Extract necessary config/state from `context_object`: Twilio credentials (from `context_object['secrets']['twilio']`), recipient identifier (`primary_channel`), sender identifier (from `channel_config`).
    *   Extract AI response content from `context_object['open_ai_response']['response_content']`.
    *   Call Channel Provider service function (`twilio_service.send_whatsapp_message`), passing credentials and response content.
    *   **Result:** Sent message SID, status.
    *   Store results in `context_object['twilio_response'] = {'message_sid': ..., 'status': ...}`.
    *   *On Failure (Twilio API error, Invalid number):* Log error, add SQS message ID to `batchItemFailures`, continue to next record.
11. **Construct Final Message Maps:**
    *   Create the **user message map** using data from `context_object['staging_table_merged_data']` and a current timestamp.
    *   Create the **assistant message map** using data from `context_object['open_ai_response']`, `context_object['twilio_response']`, and a current timestamp.
12. **Final Atomic Update (Append BOTH Messages):**
    *   **Action:** Single `UpdateItem` on the `ConversationsTable`.
    *   **Key:** `primary_channel` + `conversation_id`.
    *   **UpdateExpression:** Uses `list_append` twice to append *both* the user message map *and* the assistant message map to the `messages` list attribute. Also updates `conversation_status` (to an idle state like 'reply_sent'), `updated_at`, `openai_thread_id`, `last_assistant_message_sid`, and token counts.
    *   **ConditionExpression:** `conversation_status = :processing_reply` (ensures the lock acquired in Step 2 is still effectively held).
    *   *On Failure (ConditionalCheckFailedException - unlikely here but possible, other DB error):* Log CRITICAL error (message sent but DB update failed), add SQS message ID to `batchItemFailures`, continue to next record.
13. **Cleanup Staging & Trigger-Lock (Post-Success):**
    *   Only if Step 12 succeeds:
        *   `BatchWriteItem` to delete all processed items (using keys from `staged_items` list) from `conversations-stage` table.
        *   `DeleteItem` to remove the corresponding row (using `conversation_id`) from `conversations-trigger-lock` table.
    *   *On Failure:* Log errors, but proceed (TTL will eventually clean up).
14. **Release Processing Lock (Implicit via Step 12):** The `UpdateItem` in Step 12 changes the `conversation_status` away from `processing_reply`, effectively releasing the lock.
15. **Lambda Message Success:**
    *   (Handled by reaching end of `try` block without adding to `batchItemFailures`).

*Cleanup (In `finally` block for each record):*
*   **Stop Heartbeat & Check Errors:** Call `heartbeat.stop()`. If `heartbeat.check_for_errors()` returns an exception, log error and ensure message ID is in `batchItemFailures`.
*   **Release Lock on Error:** If the lock was acquired (`lock_status == LOCK_ACQUIRED`) and an exception occurred *before* Step 12 completed, attempt to `UpdateItem` to set `conversation_status` to an error state (e.g., `'processing_error'`) to unlock the record. Log any errors during release.

## 4. Error Handling Considerations

*   **Mid-Process Failure (After Lock, Before Final Update):** If the Lambda fails during AI (Step 9) or Twilio (Step 10) calls after acquiring the lock, the `finally` block attempts to release the lock by setting status to `'processing_error'`. The SQS message is marked for failure and retried. The user message is *not* yet persisted in the main table. Upon retry, the process restarts (lock acquisition will succeed if released correctly, staging query runs again). OpenAI history (via `thread_id`) allows conversation continuation.
*   **Final Update Failure (Step 12):** If the final `UpdateItem` fails after the reply *was sent* via Twilio, this is logged critically. The SQS message is marked for failure. Retries will re-attempt the entire process, but the idempotency lock (Step 2) should prevent duplicate AI/Twilio calls if the lock wasn't properly released on the first failure. However, the crucial state is the DB inconsistency, which requires manual monitoring/intervention based on critical logs/alarms.
*   **Cleanup Failures (Step 13):** Logged as errors, but processing is considered complete. TTL mechanisms will eventually clean up orphaned stage/lock records.

## 5. Idempotency

*   The primary idempotency mechanism is the processing lock acquired in Step 2 via conditional `UpdateItem` on `conversation_status`.
*   The final `UpdateItem` (Step 12) also uses the `conversation_status = :processing_reply` condition, preventing double appends if a retry occurs *after* the initial lock acquisition but *before* the final update completes successfully.
*   Deduplication based on `message_sid` is not explicitly performed in this Lambda; it relies on the idempotency lock and the eventual cleanup of the staging table. 