# LLD: Write to Conversation Staging Table

## 1. Purpose

This document details the process within the `webhook_handler` Lambda (Stage 1) for temporarily storing the validated message context and routing information before potentially triggering the downstream batch processor. This staging step ensures that all message details are reliably captured even if multiple messages arrive close together for the same conversation.

## 2. Context within Flow

This step occurs *after* the following have successfully completed within the `webhook_handler`:
*   Parsing the incoming webhook event (`create_context_object`).
*   Validating conversation existence (`check_conversation_exists`).
*   Validating conversation rules (`validate_conversation_rules`).
*   Determining the target SQS queue (`determine_target_queue`).

And *before*:
*   Attempting the trigger scheduling lock (`conversations-trigger-lock` table).

## 3. Target Table: `conversations-stage`

A dedicated DynamoDB table is used for this temporary storage.

*   **Table Name:** `conversations-stage`
*   **Primary Key:**
    *   **Partition Key (PK):** `conversation_id` (Type: String) - Groups all messages belonging to the same conversation.
    *   **Sort Key (SK):** `message_sid` (Type: String) - Uniquely identifies each message within a conversation batch window using the provider's ID (e.g., Twilio SID).
*   **Key Attributes:**
    *   `conversation_id` (String): The PK.
    *   `message_sid` (String): The SK.
    *   `primary_channel` (String): The *company's* identifier for the channel being used (e.g., the company's Twilio WhatsApp number `+14...`). Allows direct `GetItem` on the main Conversations table.
    *   `body` (String): The raw text content of the incoming user fragment.
    *   `received_at` (String **or** Number): UTC ISO‑8601 timestamp (or epoch seconds) of when the fragment was received.
    *   `expires_at` (Number – TTL): Unix epoch seconds. `now + W + buffer` so DynamoDB auto‑purges the row after the batch window.
*   **TTL (Optional but Recommended):** Consider adding a TTL attribute (e.g., `expires_at`) set to a reasonable duration (e.g., 24-72 hours) to automatically clean up any records that might somehow be orphaned if the `BatchProcessorLambda` fails permanently. This acts as a safety net.

## 4. Process within `webhook_handler`

After successfully determining the `target_queue_url` for a validated `context_object`:

1.  **Prepare Item:** Construct the item to be written to the `conversations-stage` table.
    ```python
    import time
    import datetime
    import boto3
    from decimal import Decimal
    import json # To handle potential non-standard types if needed

    # Assume context_object is the validated dictionary
    # Assume target_queue_url is the determined URL string
    # Assume dynamodb_table is a boto3 DynamoDB Table resource for conversations-stage

    conversation_id = context_object['conversation_id']
    message_sid = context_object['message_sid']
    received_at_iso = datetime.datetime.utcnow().isoformat()

    # Optional: Convert floats to Decimals if present in context_object
    # item_context = json.loads(json.dumps(context_object), parse_float=Decimal)
    # Or handle specific known float fields

    stage_item = {
        'conversation_id': conversation_id,
        'message_sid': message_sid,
        'primary_channel': context_object['primary_channel'],
        'body': context_object['body'],
        'received_at': received_at_iso,
        'expires_at': int(time.time()) + (W + BUFFER)
    }
    # Remove None values before writing
    stage_item = {k: v for k, v in stage_item.items() if v is not None}

    ```
2.  **Execute `PutItem`:** Write the item to the `conversations-stage` table. A standard `PutItem` is used here, as overwriting based on `conversation_id` + `message_sid` is acceptable (and unlikely if `message_sid` is truly unique).
    ```python
    try:
        response = dynamodb_table.put_item(Item=stage_item)
        print(f"Successfully staged message {message_sid} for conversation {conversation_id}")
        # Proceed to the next step: Attempting Trigger Lock

    except Exception as e:
        print(f"Error writing message {message_sid} to conversations-stage table: {e}")
        # Handle error appropriately - potentially signal 'STAGE_WRITE_ERROR'
        # This might be considered a transient error, potentially raising an
        # exception to trigger API GW 5xx and webhook retry.
        raise # Example: Treat as transient

    ```

## 5. Outcome & Benefits

*   **Reliable Capture:** Ensures that every validated incoming message's context is saved, even if subsequent steps (like acquiring the trigger lock or sending the SQS message) fail transiently.
*   **Decoupling:** Further decouples the initial receipt and validation from the batch processing logic.
*   **Batch Assembly:** Provides the source data for the `MessagingLambda` to query by `conversation_id`, sort by `received_at`/`message_sid`, and assemble the full batch of message fragments.
*   **Atomicity:** Each message write is atomic at the item level. 