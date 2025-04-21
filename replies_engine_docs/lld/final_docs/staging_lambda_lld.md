# LLD: Staging Lambda (Stage 1 - Webhook Handler)

## 1. Purpose and Responsibilities

The `StagingLambda` (Stage 1) function acts as the central, unified, and fast entry point for all incoming webhook requests (initially Twilio WhatsApp/SMS). Its primary responsibilities are:

*   **Receive & Parse:** Accept webhook POST requests forwarded by API Gateway. Parse the raw payload, headers (including `X-Twilio-Signature`), and request metadata to reconstruct the signed URL and build a structured initial `context_object`.
*   **Minimal DB Lookup:** Query the `ConversationsTable` GSI using `From`/`To` identifiers to retrieve the conversation-specific `credential_ref` (Secrets Manager ID) and the definitive `conversation_id`.
*   **Fetch Credentials:** Retrieve the tenant-specific Twilio Auth Token from Secrets Manager using the `credential_ref`.
*   **Authenticate:** Validate the `X-Twilio-Signature` header using the fetched Auth Token and reconstructed request details.
*   **Fetch Full Context:** If authentication succeeds, perform a `GetItem` on `ConversationsTable` using the `primary_channel` (derived from `From` ID) and `conversation_id` to retrieve the full conversation state.
*   **Merge Context:** Merge the fetched database record into the initial `context_object`.
*   **Validate Business Rules:** Perform checks on the merged `context_object` (project status, allowed channels, conversation lock status).
*   **Route:** Determine the appropriate next step: Channel Queue or Human Handoff Queue.
*   **Stage:** Persist essential message details (using `message_sid` from the incoming request) reliably to the `conversations-stage` DynamoDB table.
*   **Trigger/Queue (Conditional):** Conditionally send trigger or context message to the target SQS queue.
*   **Acknowledge:** Return a timely response to the webhook provider.

## 2. Trigger

*   **Source:** AWS API Gateway (REST API with Lambda Proxy Integration).
*   **Event:** Standard API Gateway Lambda Proxy event structure.

## 3. Core Components & Dependencies

*   **AWS API Gateway:** Forwards webhook requests.
*   **AWS Secrets Manager:** Stores tenant-specific Twilio Auth Tokens.
*   **DynamoDB:**
    *   `ConversationsTable`: (GSI Query for creds, GetItem for context) To fetch configuration and state.
    *   `conversations-stage`: (Write) To temporarily store incoming message details.
    *   `conversations-trigger-lock`: (Conditional Write) To manage trigger scheduling.
*   **AWS SQS:**
    *   Channel Queues (`WhatsAppQueue`, `SMSQueue`, `EmailQueue`): (Send Message with Delay).
    *   `HumanHandoffQueue`: (Send Message).
*   **AWS CloudWatch Logs:** For logging.
*   **Boto3 SDK:** For interacting with AWS services.
*   **Twilio Python Library:** Specifically `twilio.request_validator.RequestValidator` for signature validation.
*   **Internal Libraries/Utils:**
    *   `utils/parsing_utils.py`: For `parse_incoming_request` (enhanced).
    *   `core/validation.py`: For `validate_conversation_rules`.
    *   `core/routing.py`: For `determine_target_queue`.
    *   `utils/response_builder.py`: For standardizing responses.
    *   `services/dynamodb_service.py`: Wrapper for table interactions (including `get_credential_ref_for_validation` and `get_full_conversation`).
    *   `services/sqs_service.py`: Wrapper for sending messages.
    *   `services/secrets_manager_service.py`: Wrapper for fetching secrets.

## 4. Detailed Processing Steps (Late Validation Flow)

1.  **Reception & Enhanced Parsing:**
    *   API Gateway triggers the `handler` function with `event`.
    *   Call `utils.parsing_utils.parse_incoming_request(event)`.
    *   Extracts `channel_type`, `from_id`, `to_id`, derives `conversation_id`, extracts `X-Twilio-Signature`, reconstructs the `request_url`, parses body params into `parsed_body_params`, and identifies `message_sid`.
    *   Returns a structured dictionary (`parsing_result`) containing these elements and an initial `context_object`.
    *   *On Failure:* Return `None` or `{'success': False}`. Handler proceeds to Step 10 signaling `'PARSING_ERROR'`.

2.  **Get Credential Reference (Minimal DB Query):**
    *   Handler calls `services.dynamodb_service.get_credential_ref_for_validation(channel_type, from_id, to_id)`.
    *   Queries appropriate GSI on `ConversationsTable` using prefix-stripped `to_id` and `from_id`.
    *   Uses `ProjectionExpression` for `channel_config` and `conversation_id`.
    *   Extracts the channel-specific `credential_ref`.
    *   *On Failure (DB Error, Not Found, Missing Config):* Return dict with error `status`. Handler proceeds to Step 10.
    *   *On Success:* Returns `{'status': 'FOUND', 'credential_ref': '...', 'conversation_id': '...'}`. Handler proceeds to Step 3.

3.  **Fetch Auth Token (Secrets Manager):**
    *   Handler calls `services.secrets_manager_service.get_twilio_auth_token(credential_ref)`.
    *   *On Failure (Secret Not Found, Access Denied, etc.):* Return `None`. Handler proceeds to Step 10 signaling `'SECRET_FETCH_FAILED'`.
    *   *On Success:* Returns the `retrieved_auth_token`. Handler proceeds to Step 4.

4.  **Validate Twilio Signature:**
    *   Initialize `RequestValidator(retrieved_auth_token)`.
    *   Call `validator.validate(request_url, parsed_body_params, signature_header)`.
    *   *On Failure (Signature Invalid):* Log critical error. Handler proceeds to Step 10 signaling `'INVALID_SIGNATURE'`.
    *   *On Success:* Log success. Handler proceeds to Step 5.

5.  **Fetch Full Context (Main DB Query):**
    *   Determine `primary_channel_key` by stripping prefix from `from_id`.
    *   Call `services.dynamodb_service.get_full_conversation(primary_channel_key, definitive_conversation_id)`.
    *   *On Failure (DB Error, Item Not Found):* Return dict with error `status`. Handler proceeds to Step 10.
    *   *On Success:* Returns `{'status': 'FOUND', 'data': full_item_dict}`. Handler proceeds to Step 6.

6.  **Merge Context:**
    *   Handler performs `context_object.update(db_data)` using the `data` from the previous step and the initial `context_object` from parsing.

7.  **Conversation Rule Validation:**
    *   Handler calls `core.validation.validate_conversation_rules(context_object)` using the merged context.
    *   Checks `project_status`, `allowed_channels`, `conversation_status`.
    *   *On Failure:* Returns `{'valid': False, ...}`. Handler proceeds to Step 10.
    *   *On Success:* Returns `{'valid': True, ...}`. Handler proceeds to Step 8.

8.  **Routing Logic:**
    *   Handler calls `core.routing.determine_target_queue(context_object)`.
    *   *On Failure:* Returns `None`. Handler proceeds to Step 10 signaling `'ROUTING_ERROR'`.
    *   *On Success:* Returns `target_queue_url`. Handler proceeds to Step 9.

9.  **Stage, Lock & Queue (Conditional):**
    *   a. **Write to Stage Table:** `PutItem` to `conversations-stage` using `conversation_id` and `message_sid`. Handle DB errors -> Step 10.
    *   b. **Attempt Lock:** If target is not Handoff Queue, conditional `PutItem` to `conversations-trigger-lock`. Handle DB errors (Skip queueing on `ConditionalCheckFailedException`) -> Step 10.
    *   c. **Send to SQS:** If needed based on routing and lock status, `SendMessage` to `target_queue_url`. 
        *   **Handoff Queue:** Send full `context_object` with `DelaySeconds=0`.
        *   **Channel Queue:** Send minimal JSON `{"conversation_id": "...", "primary_channel": "..."}` with `DelaySeconds=W`. 
        *   Handle SQS errors -> Step 10.

10. **Acknowledgment / Final Response:**
    *   Handler calls `_determine_final_error_response()` based on outcome.
    *   Returns `200 OK` (TwiML or JSON) on success or non-transient errors (including `INVALID_SIGNATURE`).
    *   Raises Exception for specific transient errors to trigger API GW 5xx -> Twilio retry.

## 5. Error Handling & Response Logic

The `StagingLambda` implements specific logic, primarily for Twilio webhooks, to ensure correct retry behavior.

1.  **Internal Error Codes:** Includes `PARSING_ERROR`, `DB_TRANSIENT_ERROR`, `DB_QUERY_ERROR`, `CONVERSATION_NOT_FOUND`, `MISSING_CREDENTIAL_CONFIG`, `SECRET_FETCH_FAILED`, `INVALID_SIGNATURE`, `PROJECT_INACTIVE`, `CHANNEL_NOT_ALLOWED`, `CONVERSATION_LOCKED`, `ROUTING_ERROR`, `STAGE_WRITE_ERROR...`, `TRIGGER_LOCK_WRITE_ERROR`, `QUEUE_ERROR`, `INTERNAL_ERROR`.
2.  **Response Builder:** A `response_builder` utility suggests standard HTTP status codes/bodies based on error codes (e.g., 404 for `CONVERSATION_NOT_FOUND`, 503 for `DB_TRANSIENT_ERROR`).
3.  **Final Response Determination (`_determine_final_error_response`):**
    *   **For Twilio Channels (`whatsapp`, `sms`):**
        *   If error code is in `TRANSIENT_ERROR_CODES` (now including potential `SECRET_FETCH_TRANSIENT_ERROR`): **Raise Exception** -> Twilio Retries.
        *   If error code is `CONVERSATION_LOCKED`: Return **200 OK TwiML** with specific message.
        *   For **all other non-transient errors** (including `INVALID_SIGNATURE`): Return **200 OK TwiML** (empty) -> Prevents Twilio retries.
    *   **For Other Channels (e.g., `email`):**
        *   Generally return the standard JSON error response (e.g., 4xx/5xx). `INVALID_SIGNATURE` might map to 403.

## 6. Security & IAM

*   **Execution Role:** `StagingLambdaRole` (defined in `iam_roles_policies_lld.md`).
*   **Required Permissions (Summary):**
    *   `logs:*` (via AWSLambdaBasicExecutionRole or explicit definition).
    *   `dynamodb:Query` on `ConversationsTable` indexes.
    *   `dynamodb:GetItem` on `ConversationsTable`.
    *   `dynamodb:PutItem` on `conversations-stage` table.
    *   `dynamodb:PutItem` on `conversations-trigger-lock` table.
    *   `sqs:SendMessage` to all relevant queues.
    *   `secretsmanager:GetSecretValue` on secrets matching defined patterns (e.g., `ai-multi-comms/whatsapp-credentials/*/*/twilio-dev`).
*   **Resource-Based Policy:** Lambda must have a resource policy allowing `lambda:InvokeFunction` from the `apigateway.amazonaws.com` principal, scoped to the specific API Gateway ARN.
*   Refer to `iam_roles_policies_lld.md` for detailed policy definitions.

## 7. Monitoring & Logging

*   **CloudWatch Logs:** Log key events (parsing results including URL/Sig, GSI lookup outcome, secret fetch attempt/outcome, **signature validation result**, full context fetch, rule validation result, routing decision, stage write, lock attempt outcome, queue send outcome) at appropriate levels (DEBUG/INFO/ERROR/CRITICAL). Log errors with context.
*   **CloudWatch Metrics:** Monitor standard Lambda metrics. Add custom metrics for `INVALID_SIGNATURE` count and `SECRET_FETCH_FAILED` count.
*   **X-Ray:** Enable Active Tracing. Consider adding custom subsegments around validation, DB calls, and Secrets Manager calls.

## 8. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 9. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 10. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 11. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 12. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 13. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 14. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 15. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 16. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

