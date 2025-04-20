# LLD: Trigger Scheduling Lock Mechanism

## 1. Purpose

This document details the mechanism used within the `StagingLambda` (Stage 1) to ensure that only **one** SQS trigger message is sent to the appropriate **Channel Queue** (e.g., `WhatsAppQueue`, triggering the `MessagingLambda` - Stage 2) for each desired conversation batching window (defined by `W` seconds). This prevents redundant processor invocations when multiple messages (M1, M2, etc.) arrive for the same conversation in quick succession.

## 2. Mechanism: Atomic Conditional Write

The core mechanism relies on an **atomic DynamoDB `PutItem` operation with a `ConditionExpression`**.

*   **Operation:** `PutItem`
*   **Condition:** `attribute_not_exists(conversation_id)`
*   **Atomicity:** DynamoDB guarantees that checking the condition (does an item with this `conversation_id` already exist?) and performing the write (if the condition is met) occur as a single, indivisible operation. This prevents race conditions where two nearly simultaneous message handlers might both think they are the first and attempt to send a trigger.

## 3. Target Table: `conversations-trigger-lock`

A dedicated, simple DynamoDB table is used solely for managing these temporary trigger locks.

*   **Table Name:** `conversations-trigger-lock`
*   **Primary Key:**
    *   **Partition Key (PK):** `conversation_id` (Type: String) - Identifies the specific conversation the lock applies to.
*   **Attributes:**
    *   `conversation_id` (String): The partition key.
    *   `expires_at` (Type: Number): A Unix epoch timestamp (seconds since Jan 1, 1970). This attribute is configured as the **Time To Live (TTL)** attribute for the table, enabling automatic item deletion by DynamoDB after the specified time.
*   **Purpose of Table:** This table acts purely as a temporary flag or marker. It indicates "a trigger message has already been scheduled for this conversation's current batch window". It does **not** store any actual message content.

## 4. Process within `StagingLambda`

When a validated message arrives and needs to potentially trigger the batch processor:

1.  **Calculate Expiry Time:** Determine the Unix epoch timestamp when this lock record should automatically expire. This should be *after* the SQS message delay (`W`) plus a safety buffer to account for SQS processing time and potential visibility timeout variations.
    *   `W`: The `DelaySeconds` value (e.g., 10 seconds) to be set on the trigger message sent to the Channel Queue.
    *   `buffer`: A safety margin (e.g., 60 seconds).
    *   `expiry_timestamp = current_epoch_time + W + buffer`
2.  **Prepare DynamoDB Item:** Construct the item to be written. Using `boto3.resource`:
    ```python
    lock_item = {
        'conversation_id': context_object['conversation_id'],
        'expires_at': int(expiry_timestamp) # Ensure it's an integer for DynamoDB Number type
    }
    ```
    *(Note: If using `boto3.client`, DynamoDB type descriptors are needed: `{'conversation_id': {'S': conv_id}, 'expires_at': {'N': str(int(expiry_timestamp))}}`)*
3.  **Execute Conditional `PutItem`:** Call the DynamoDB `put_item` operation on the `conversations-trigger-lock` table with the condition expression:
    ```python
    import time
    from botocore.exceptions import ClientError
    import boto3

    # Assume dynamodb_table is a boto3 DynamoDB Table resource
    # Assume W = 10, buffer = 60
    # Assume context_object['conversation_id'] holds the ID

    conversation_id = context_object['conversation_id']
    W = 10
    buffer = 60
    expires_at = int(time.time()) + W + buffer

    try:
        dynamodb_table.put_item(
            Item={
                'conversation_id': conversation_id,
                'expires_at': expires_at
            },
            ConditionExpression='attribute_not_exists(conversation_id)'
        )
        # --- Success! PutItem was successful, meaning no lock existed. ---
        # Proceed to send the SQS Trigger Message to the appropriate Channel Queue here
        print(f"Successfully acquired trigger lock for {conversation_id}. Sending SQS trigger to Channel Queue.")
        # sqs_service.send_trigger_message(channel_queue_url, conversation_id, W) # Example call

    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            # --- Lock already exists. ---
            # This is expected for M2, M3 etc. within the window. Do nothing.
            print(f"Trigger lock already exists for {conversation_id}. No SQS trigger sent.")
            pass
        else:
            # --- Other DynamoDB error ---
            print(f"Error attempting to acquire trigger lock: {e}")
            # Decide how to handle other errors (e.g., raise exception for retry?)
            raise # Or handle appropriately

    ```

> **Cleanup Timing Note (UPDATED):**
> The lock item **is not deleted** by the `StagingLambda`.  It remains in the table so that retries/parallel webhook invocations within the same window see the lock and skip additional triggers.  The item is explicitly deleted **only after** the `MessagingLambda` has:
> 1. Successfully appended the merged user turn to the main conversation record, **and**
> 2. Forwarded the enriched `context_object` to the downstream processor.
>
> This explicit delete (performed in Step 10 of `messaging_lambda`) is in addition to the TTL expiry, ensuring fast reuse of the conversation if the downstream processing finishes quickly while also providing a fallback auto‑clean in case of failure.

## 5. Outcome & Benefits

*   **On Success (First Message):** If the `PutItem` succeeds (no existing item with that `conversation_id`), a simple record `{ "conversation_id": "...", "expires_at": ... }` is created in the `conversations-trigger-lock` table. The handler then proceeds to send the single SQS trigger message **to the appropriate Channel Queue** with `DelaySeconds=W`.
*   **On Failure (Subsequent Messages):** If the `PutItem` fails with `ConditionalCheckFailedException`, it means a lock record already exists (placed by a previous message in the window). The handler simply catches this specific exception and does *not* send another SQS trigger message.
*   **Self-Cleaning:** The `expires_at` TTL attribute ensures DynamoDB automatically deletes the lock record shortly after the batch window and processing *should* have completed, eliminating the need for explicit deletion logic for the lock itself.
*   **Robustness:** This mechanism provides an effective, atomic, and self-maintaining way to achieve the "send trigger only once per batch window" requirement. 