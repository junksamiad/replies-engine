# LLD: Staging Lambda (Stage 1 - Webhook Handler)

## 1. Purpose and Responsibilities

The `StagingLambda` (Stage 1) function acts as the central, unified, and fast entry point for all incoming webhook requests (initially Twilio WhatsApp/SMS). Its primary responsibilities are:

*   **Receive & Parse:** Accept webhook POST requests forwarded by API Gateway and parse the raw payload into a structured `context_object`.
*   **Validate:** Perform initial validation and query the `ConversationsTable` to retrieve the current conversation state. Validate against business rules (project status, allowed channels, conversation lock status).
*   **Route:** Determine the appropriate next step: either automated processing via a specific Channel Queue (WhatsApp, SMS, Email) or manual review via the Human Handoff Queue.
*   **Stage:** Persist the validated message context (`context_object` and routing info) reliably to the `conversations-stage` DynamoDB table.
*   **Trigger/Queue (Conditional):**
    *   If routed to a Channel Queue, attempt to acquire a scheduling lock via the `conversations-trigger-lock` table. If successful, send a minimal trigger message (`{ "conversation_id": "..." }`) to the appropriate Channel Queue with a per-message delay (`DelaySeconds=W`) to initiate batch processing by the `MessagingLambda`.
    *   If routed to the Human Handoff Queue, send the full `context_object` immediately (no delay) to that queue.
*   **Acknowledge:** Return a timely response (e.g., HTTP 200 with TwiML for Twilio) to the webhook provider, confirming receipt or indicating a non-transient error, without waiting for downstream processing.

## 2. Trigger

*   **Source:** AWS API Gateway (HTTP API or REST API with Lambda Proxy Integration).
*   **Event:** Standard API Gateway Lambda Proxy event structure.

## 3. Core Components & Dependencies

*   **AWS API Gateway:** Forwards webhook requests.
*   **DynamoDB:**
    *   `ConversationsTable`: (Read-only via GSI) To fetch current conversation state.
    *   `conversations-stage`: (Write) To temporarily store validated message context.
    *   `conversations-trigger-lock`: (Conditional Write) To manage trigger scheduling atomicity using TTL.
*   **AWS SQS:**
    *   Channel Queues (`WhatsAppQueue`, `SMSQueue`, `EmailQueue`): (Send Message with Delay) To send trigger messages for `MessagingLambda`.
    *   `HumanHandoffQueue`: (Send Message) To queue messages for manual review.
*   **AWS Secrets Manager:** (Read-only) To fetch Twilio credentials for webhook signature validation (if implemented).
*   **AWS CloudWatch Logs:** For logging execution details and errors.
*   **Internal Libraries/Utils:**
    *   `utils/parsing_utils.py`: For `create_context_object`.
    *   `core/validation.py`: For `check_conversation_exists`, `validate_conversation_rules`.
    *   `core/routing.py`: For `determine_target_queue`.
    *   `utils/response_builder.py`: For standardizing responses.
    *   `services/dynamodb_service.py` (or similar): Wrapper for table interactions.
    *   `services/sqs_service.py` (or similar): Wrapper for sending messages.

## 4. Detailed Processing Steps

1.  **Reception & Parsing:**
    *   API Gateway triggers the `handler` function with an `event` dictionary.
    *   The `handler` calls `utils.parsing_utils.create_context_object(event)`.
    *   This function determines the `channel_type` (e.g., 'whatsapp', 'sms') from the event path/headers.
    *   It parses the `event['body']` (and potentially headers) based on the channel specification.
    *   It populates and returns a `context_object` dictionary with `snake_case` keys and performs basic field validation (e.g., presence of required IDs).
    *   *On Failure:* Return `None`. The main handler proceeds to Step 8 (Acknowledgment) signaling `'PARSING_ERROR'`.
    *   **Flow Diagram:**
        ```mermaid
        graph TD
            A[API Gateway Event] --> B(handler function);
            B --> C{Call create_context_object};
            C --> D{Determine Channel};
            D --> E{Parse Body/Headers};
            E --> F{Populate context_object};
            F --> G{Basic Key Validation};
            G -- OK --> H(Return context_object);
            G -- Fail --> I(Return None);
            H --> B;
            I --> B;
        ```

2.  **Initial Validation & Conversation Retrieval:**
    *   The `handler` calls `core.validation.check_conversation_exists(context_object)`.
    *   Queries the appropriate GSI on `ConversationsTable` using keys derived from the context and filters for active conversations (`task_complete = 0`).
    *   *On Failure (Transient DB Error):* Return `{'valid': False, 'error_code': 'DB_TRANSIENT_ERROR', ...}`. Handler proceeds to Step 8.
    *   *On Failure (Other DB/Config Error):* Return `{'valid': False, 'error_code': 'DB_QUERY_ERROR'/'CONFIGURATION_ERROR', ...}`. Handler proceeds to Step 8.
    *   *On Failure (Not Found):* Return `{'valid': False, 'error_code': 'CONVERSATION_NOT_FOUND', ...}`. Handler proceeds to Step 8.
    *   *On Success:* Updates the `context_object` with fields from the retrieved record and returns `{'valid': True, 'data': context_object}`. Handler proceeds to Step 3.
    *   **Flow Diagram:**
        ```mermaid
        graph TD
            A[handler w/ context_object] --> B(Call check_conversation_exists);
            B --> C{Get GSI Config};
            C -- Fail --> X[Return CONFIGURATION_ERROR];
            C -- OK --> D{Prepare GSI Keys};
            D -- Fail --> Y[Return MISSING_REQUIRED_FIELD];
            D -- OK --> E[Query DynamoDB GSI w/ Filter];
            E -- DB Error --> F{Transient?};
            F -- Yes --> Z[Return DB_TRANSIENT_ERROR];
            F -- No --> AA[Return DB_QUERY_ERROR etc.];
            E -- Success --> G{Results Count?};
            G -- 0 --> BB[Return CONVERSATION_NOT_FOUND];
            G -- '>0' --> H{Handle Multiple? Select Latest};
            H --> J[Update context_object];
            J --> K[Return valid: True, data: context_object];
            X --> A; Y --> A; Z --> A; AA --> A; BB --> A; K --> A;
        ```

3.  **Conversation Rule Validation:**
    *   The `handler` calls `core.validation.validate_conversation_rules(context_object)` using the *updated* context from Step 2.
    *   Checks `project_status == 'active'`, `channel_type` in `allowed_channels`, `conversation_status != 'processing_reply'`.
    *   *On Failure:* Returns `{'valid': False, 'error_code': 'PROJECT_INACTIVE'/'CHANNEL_NOT_ALLOWED'/'CONVERSATION_LOCKED', ...}`. Handler proceeds to Step 8.
    *   *On Success:* Returns `{'valid': True, 'data': context_object}`. Handler proceeds to Step 4.
    *   **Flow Diagram:**
        ```mermaid
        graph TD
            A[handler w/ updated context] --> B(Call validate_conversation_rules);
            B --> C{Project Active?};
            C -- No --> X[Return PROJECT_INACTIVE];
            C -- Yes --> D{Channel Allowed?};
            D -- No --> Y[Return CHANNEL_NOT_ALLOWED];
            D -- Yes --> E{Status Not Locked?};
            E -- No --> Z[Return CONVERSATION_LOCKED];
            E -- Yes --> F[Return valid: True];
            X --> A; Y --> A; Z --> A; F --> A;
        ```

4.  **Routing Logic:**
    *   The `handler` calls `core.routing.determine_target_queue(context_object)`.
    *   Determines the target queue URL (`WhatsAppQueue`, `SMSQueue`, `EmailQueue`, or `HumanHandoffQueue`) based on flags like `hand_off_to_human`, `auto_queue_reply_message`, or channel type.
    *   *On Failure (e.g., unknown channel):* Returns `None`. Handler proceeds to Step 8 signaling `'ROUTING_ERROR'`.
    *   *On Success:* Returns the `target_queue_url` string. Handler proceeds to Step 5.
    *   **Flow Diagram:**
        ```mermaid
        graph TD
            A[Start Routing] --> B{Handoff Flag True?};
            B -- Yes --> Z[HANDOFF_QUEUE];
            B -- No --> C{Auto-Queue Flag True?};
            C -- Yes --> Z;
            C -- No --> D{Recipient in Auto-Queue List?};
            D -- Yes --> Z;
            D -- No --> E{Channel?};
            E -- WhatsApp --> F[WHATSAPP_QUEUE];
            E -- SMS --> G[SMS_QUEUE];
            E -- Email --> H[EMAIL_QUEUE];
            E -- Other --> I[Error: Unknown Channel];
        ```

5.  **Write to Conversation Staging Table (`conversations-stage`):**
    *   **Action:** Perform a `PutItem` operation on the `conversations-stage` table.
    *   **Item:**
        *   PK: `conversation_id`
        *   SK: `message_sid` (or timestamp)
        *   Attributes: `context_object` (full map), `target_queue_url`, `received_at` (ISO timestamp), optional `expires_at` TTL.
    *   *On Failure:* Log error. Consider if this is transient (`'STAGE_WRITE_ERROR_TRANSIENT'`) or permanent (`'STAGE_WRITE_ERROR'`). Handler proceeds to Step 8.
    *   *On Success:* Handler proceeds to Step 6.
    *   *(See `conversations_stage_write_lld.md` for more detail if needed)*.

6.  **Attempt Trigger Scheduling Lock (`conversations-trigger-lock`):**
    *   **Condition:** Only attempt if `target_queue_url` is *not* the `HumanHandoffQueue`.
    *   **Action:** Perform a conditional `PutItem` to the `conversations-trigger-lock` table.
    *   **Item:** `{ "conversation_id": "...", "expires_at": now + W + buffer }`
    *   **Condition:** `attribute_not_exists(conversation_id)`
    *   *On Success (Lock Acquired):* Set a flag `trigger_lock_acquired = True`. Proceed to Step 7.
    *   *On Failure (Lock Exists - `ConditionalCheckFailedException`):* Set `trigger_lock_acquired = False`. Log info. Proceed to Step 7.
    *   *On Failure (Other DB Error):* Log error. Signal `'TRIGGER_LOCK_ERROR'`. Handler proceeds to Step 8.
    *   *(See `trigger_lock_mechanism_lld.md` for more detail if needed)*.

7.  **Queue Message (Conditional):**
    *   **If `target_queue_url` is `HumanHandoffQueue`:**
        *   **Action:** Send message to `HumanHandoffQueue`.
        *   **Message Body:** Full `context_object` (JSON string).
        *   **DelaySeconds:** 0 (or omit).
    *   **Else if `trigger_lock_acquired` is `True`:**
        *   **Action:** Send message to the determined Channel Queue (`target_queue_url`).
        *   **Message Body:** Minimal trigger `{"conversation_id": "..."}` (JSON string).
        *   **DelaySeconds:** `W` (e.g., 10).
    *   **Else (`target_queue_url` is Channel Queue but `trigger_lock_acquired` is `False`):**
        *   Do nothing (trigger already sent by a previous message).
    *   *On SQS Send Failure:* Log error. Signal `'QUEUE_ERROR'`. Handler proceeds to Step 8.
    *   *On Success / No Action Needed:* Handler proceeds to Step 8.

8.  **Acknowledgment / Final Response:**
    *   The `handler` determines the final HTTP response based on the outcomes of the previous steps, using the `_determine_final_error_response` helper.
    *   **Success Path (Steps 1-7 complete without fatal error):** Returns `200 OK` (Empty TwiML for Twilio, standard JSON for others).
    *   **Error Path:** Maps internal error codes (`PARSING_ERROR`, `DB_TRANSIENT_ERROR`, `CONVERSATION_NOT_FOUND`, `CONVERSATION_LOCKED`, `ROUTING_ERROR`, `STAGE_WRITE_ERROR...`, `TRIGGER_LOCK_ERROR`, `QUEUE_ERROR`, etc.) to appropriate HTTP responses following the logic in Section 5 below.

## 5. Error Handling & Response Logic

The `StagingLambda` implements specific logic, primarily for Twilio webhooks, to ensure correct retry behavior.

1.  **Internal Error Codes:** Steps 1-7 signal specific error codes upon failure.
2.  **Response Builder:** A `response_builder` utility suggests standard HTTP status codes/bodies based on error codes (e.g., 404 for `CONVERSATION_NOT_FOUND`, 503 for `DB_TRANSIENT_ERROR`).
3.  **Final Response Determination (`_determine_final_error_response`):**
    *   **For Twilio Channels (`whatsapp`, `sms`):**
        *   If error code is in `TRANSIENT_ERROR_CODES` (e.g., `DB_TRANSIENT_ERROR`, potentially `STAGE_WRITE_ERROR_TRANSIENT`, `TRIGGER_LOCK_ERROR`, `QUEUE_ERROR` if deemed transient): **Raise Exception**. API GW returns 5xx -> **Twilio Retries**.
        *   If error code is `CONVERSATION_LOCKED`: Return **200 OK TwiML** with specific `<Message>` body advising user agent is busy.
        *   For **all other non-transient errors** (e.g., `PARSING_ERROR`, `CONVERSATION_NOT_FOUND`, `PROJECT_INACTIVE`, `CHANNEL_NOT_ALLOWED`, `ROUTING_ERROR`, non-transient DB/Stage/Lock/Queue errors) or unexpected code errors: Return **200 OK TwiML** (empty). This acknowledges receipt but prevents Twilio retries.
    *   **For Other Channels (e.g., `email`):**
        *   Generally return the standard JSON error response (e.g., 4xx/5xx) suggested by the `response_builder`.

## 6. Security & IAM

*   **Execution Role:** `StagingLambdaRole` (defined in `iam_roles_policies_lld.md`).
*   **Required Permissions (Summary):**
    *   `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents` (via AWSLambdaBasicExecutionRole).
    *   `dynamodb:Query` on `ConversationsTable` and its GSIs.
    *   `dynamodb:PutItem` on `conversations-stage` table.
    *   `dynamodb:PutItem` on `conversations-trigger-lock` table (with condition expression support).
    *   `sqs:SendMessage` to `WhatsAppQueue`, `SMSQueue`, `EmailQueue`, `HumanHandoffQueue`.
    *   `secretsmanager:GetSecretValue` for Twilio credentials (if signature validation used).
*   Refer to `iam_roles_policies_lld.md` for detailed policy definitions.

## 7. Monitoring & Logging

*   **CloudWatch Logs:** Log key events (parsing result, validation outcome, routing decision, stage write, lock attempt outcome, queue send outcome) at INFO level. Log errors at ERROR level with context. Use structured logging.
*   **CloudWatch Metrics:** Monitor standard Lambda metrics (Invocations, Errors, Duration, Throttles). Consider custom metrics for specific validation failures or lock contention.
*   **X-Ray:** Enable Active Tracing for request tracing through API Gateway, Lambda, DynamoDB, and SQS. 