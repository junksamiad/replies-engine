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
    *   Extract necessary config/state from `context_object`: `openai_thread_id` (if exists), `assistant_id` (ensure it's the reply one, e.g., `assistant_id_replies`), API key (from `context_object['secrets']['openai']`), `combined_body` (from `context_object['staging_table_merged_data']`).
    *   **Validate Inputs:** Check for missing `thread_id`, `assistant_id`, `combined_body`, or API key. *On Failure:* Log error, treat as `AI_INVALID_INPUT`, add SQS message ID to `batchItemFailures`, continue to next record.
    *   Call AI service function (`openai_service.process_reply_with_ai`), passing the thread ID and new user message (`combined_body`).
    *   The service handles creating/using threads, adding messages, running the assistant, and returns a tuple `(status_code, result_payload)`.
    *   **Result (on SUCCESS):** `result_payload` contains assistant's response content, token counts. Store in `context_object['open_ai_response']`.
    *   **Result (on TRANSIENT_ERROR):** Log warning. **Raise Exception** to trigger SQS retry.
    *   **Result (on NON_TRANSIENT_ERROR / INVALID_INPUT from service):** Log error. Add SQS message ID to `batchItemFailures`, continue to next record.
10. **Send Reply via Channel Provider (e.g., Twilio):**
    *   Extract necessary config/state from `context_object`: Twilio credentials (from `context_object['secrets']['twilio']`), recipient identifier (`primary_channel`), sender identifier (from `channel_config`).
    *   Extract AI response content from `context_object['open_ai_response']['response_content']`.
    *   Call Channel Provider service function (`twilio_service.send_whatsapp_reply`), passing credentials and response content. Returns `(status_code, result_payload)`.
    *   **Result (on SUCCESS):** Store `result_payload` (containing `message_sid`, `body`) in `context_object['twilio_response']`.
    *   **Result (on TRANSIENT_ERROR):** Log warning. **Raise Exception** to trigger SQS retry.
    *   **Result (on NON_TRANSIENT_ERROR / INVALID_INPUT):** Log error. Add SQS message ID to `batchItemFailures`, continue to next record.
11. **Construct Final Message Maps:**
    *   Generate distinct UTC timestamps: `user_msg_ts` and `assistant_msg_ts`.
    *   Create the **user message map** using `context_object['staging_table_merged_data']` (for `first_message_sid`, `combined_body`) and `user_msg_ts`.
    *   Create the **assistant message map** using `context_object['twilio_response']` (for `message_sid`, `body`), `context_object['open_ai_response']` (for token counts), and `assistant_msg_ts`.
    *   *On Failure (KeyError, etc.):* Log error, add SQS message ID to `batchItemFailures`, continue to next record.
12. **Final Atomic Update (Append BOTH Messages):**
    *   **Pre-Check:** Check `hand_off_to_human` flag in `context_object['conversations_db_data']`. Log warning if true, but proceed with update (further handoff logic is separate).
    *   **Calculate:** Determine `processing_time_ms` based on start/end times.
    *   **Action:** Single `UpdateItem` on the `ConversationsTable` using `primary_channel` + `conversation_id` as key.
    *   **UpdateExpression:** Uses `list_append` twice to append *both* the user map *and* the assistant map (created in Step 11) to `messages`. Also updates:
        *   `conversation_status` (to an idle state like 'reply_sent').
        *   `updated_at` (to current time).
        *   `last_assistant_message_sid` (from assistant map).
        *   Token counts (`prompt_tokens`, `completion_tokens`, `total_tokens` from assistant map).
        *   `initial_processing_time_ms` (calculated duration).
        *   `task_complete`, `hand_off_to_human`, `hand_off_to_human_reason` (using current values from `conversations_db_data` unless overridden by future logic).
        *   Does **not** update `openai_thread_id` in this reply flow.
    *   **ConditionExpression:** `conversation_status = :processing_reply`.
    *   Call DB service function `update_conversation_after_reply`. Returns `(status_code, error_message)`.
    *   **Result (on SUCCESS):** Log success. Proceed to Step 13 (Cleanup).
    *   **Result (on DB_LOCK_LOST):** Log CRITICAL error (Message sent, lock lost before final update). **DO NOT** add to `batchItemFailures`. Continue to next record.
    *   **Result (on DB_ERROR):** Log CRITICAL error (Message sent, DB update failed). **DO NOT** add to `batchItemFailures`. Continue to next record.
13. **Cleanup Staging & Trigger-Lock (Post-Success):**
    *   **Condition:** Only runs if Step 12 (Final Atomic Update) completed successfully.
    *   **Action 1 (Staging Table):**
        *   Extract keys (`conversation_id`, `message_sid`) from all items retrieved in `staged_items` (Step 4).
        *   Call DB service function `cleanup_staging_table` with the list of keys.
        *   This function uses `BatchWriteItem` with `DeleteRequest` for efficiency.
    *   **Action 2 (Trigger Lock Table):**
        *   Call DB service function `cleanup_trigger_lock` with the `conversation_id`.
        *   This function uses `DeleteItem`.
    *   **Error Handling:** Failures in cleanup functions are logged as warnings. Processing is *not* failed, as the main work is done. TTL is the fallback cleanup mechanism.
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