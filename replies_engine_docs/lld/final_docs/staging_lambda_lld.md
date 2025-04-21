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
    *   c. **Send to SQS:** If needed based on routing and lock status, `SendMessage` to `target_queue_url`. Handle SQS errors -> Step 10.

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

## 17. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 18. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 19. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 20. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 21. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 22. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 23. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 24. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 25. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 26. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 27. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 28. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 29. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 30. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 31. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 32. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 33. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 34. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 35. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 36. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 37. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 38. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 39. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 40. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 41. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 42. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 43. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 44. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 45. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 46. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 47. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 48. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 49. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 50. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 51. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 52. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 53. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 54. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 55. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 56. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 57. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 58. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 59. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 60. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 61. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 62. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 63. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 64. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 65. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 66. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 67. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 68. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 69. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 70. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 71. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 72. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 73. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 74. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 75. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 76. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 77. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 78. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 79. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 80. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 81. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 82. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 83. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 84. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 85. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 86. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 87. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 88. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 89. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 90. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 91. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 92. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 93. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 94. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 95. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 96. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 97. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 98. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 99. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 100. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 101. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 102. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 103. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 104. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 105. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 106. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 107. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 108. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 109. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 110. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 111. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 112. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 113. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 114. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 115. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 116. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 117. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 118. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 119. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 120. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 121. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 122. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 123. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 124. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 125. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 126. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 127. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 128. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 129. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 130. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 131. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 132. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 133. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 134. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 135. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 136. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 137. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 138. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 139. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 140. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 141. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 142. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 143. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 144. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 145. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 146. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 147. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 148. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 149. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 150. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 151. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 152. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 153. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 154. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 155. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 156. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 157. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 158. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 159. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 160. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 161. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 162. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 163. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 164. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 165. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 166. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 167. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 168. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 169. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 170. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 171. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 172. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 173. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 174. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 175. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 176. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 177. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 178. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 179. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 180. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 181. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 182. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 183. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 184. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 185. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 186. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 187. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 188. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 189. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 190. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 191. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 192. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 193. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 194. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 195. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 196. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 197. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 198. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 199. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 200. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 201. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 202. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 203. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 204. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 205. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 206. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 207. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 208. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 209. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 210. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 211. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 212. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 213. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 214. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 215. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 216. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 217. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 218. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 219. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 220. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 221. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 222. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 223. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 224. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 225. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 226. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 227. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 228. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 229. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 230. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 231. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 232. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 233. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 234. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 235. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 236. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 237. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 238. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 239. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 240. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 241. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 242. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 243. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 244. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 245. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 246. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 247. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 248. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 249. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 250. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 251. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 252. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 253. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 254. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 255. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 256. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 257. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 258. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 259. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 260. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 261. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 262. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 263. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 264. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 265. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 266. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 267. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 268. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 269. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 270. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 271. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 272. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 273. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 274. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 275. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 276. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 277. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 278. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 279. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 280. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 281. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 282. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 283. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 284. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 285. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 286. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 287. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 288. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 289. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 290. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 291. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 292. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 293. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 294. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 295. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 296. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 297. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 298. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 299. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 300. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 301. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 302. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 303. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 304. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 305. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 306. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 307. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 308. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 309. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 310. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 311. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 312. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 313. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 314. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 315. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 316. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 317. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 318. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 319. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 320. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 321. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 322. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 323. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 324. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 325. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 326. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 327. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 328. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 329. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 330. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 331. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 332. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 333. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 334. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 335. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 336. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 337. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 338. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 339. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 340. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 341. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 342. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 343. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 344. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 345. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 346. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 347. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 348. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 349. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 350. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 351. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 352. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 353. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 354. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 355. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 356. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 357. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 358. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 359. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 360. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 361. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 362. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 363. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 364. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 365. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 366. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 367. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 368. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 369. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 370. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 371. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 372. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 373. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 374. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 375. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 376. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 377. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 378. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 379. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 380. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 381. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 382. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 383. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 384. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 385. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 386. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 387. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 388. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 389. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 390. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 391. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 392. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 393. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 394. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 395. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 396. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 397. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 398. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 399. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 400. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 401. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 402. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 403. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 404. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 405. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 406. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 407. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 408. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 409. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 410. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 411. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 412. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 413. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 414. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 415. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 416. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 417. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 418. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 419. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 420. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 421. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 422. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 423. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 424. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 425. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 426. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 427. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 428. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 429. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 430. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 431. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 432. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 433. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 434. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 435. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 436. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 437. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 438. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 439. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 440. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 441. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 442. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 443. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 444. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 445. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 446. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 447. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 448. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 449. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 450. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 451. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 452. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 453. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 454. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 455. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 456. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 457. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 458. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 459. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 460. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 461. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 462. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 463. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 464. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 465. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 466. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 467. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 468. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 469. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 470. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 471. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 472. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 473. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 474. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 475. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 476. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 477. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 478. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 479. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 480. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 481. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 482. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 483. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 484. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 485. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 486. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 487. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 488. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 489. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 490. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 491. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 492. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 493. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 494. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 495. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 496. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 497. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 498. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 499. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 500. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 501. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 502. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 503. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 504. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 505. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 506. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 507. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 508. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 509. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 510. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 511. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 512. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 513. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 514. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 515. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 516. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 517. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 518. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 519. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 520. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 521. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 522. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 523. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 524. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 525. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 526. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 527. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 528. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 529. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 530. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 531. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 532. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 533. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 534. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 535. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 536. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 537. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 538. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 539. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 540. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 541. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 542. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 543. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 544. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 545. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 546. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 547. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 548. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 549. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 550. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 551. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 552. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 553. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 554. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 555. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 556. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 557. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 558. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 559. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 560. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 561. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 562. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 563. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 564. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 565. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 566. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 567. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 568. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 569. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 570. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 571. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 572. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 573. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 574. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 575. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 576. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 577. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 578. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 579. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 580. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 581. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 582. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 583. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 584. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 585. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 586. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 587. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 588. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 589. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 590. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 591. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 592. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 593. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 594. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 595. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 596. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 597. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 598. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 599. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 600. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 601. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 602. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 603. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 604. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 605. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 606. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 607. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 608. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 609. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 610. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 611. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 612. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 613. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 614. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 615. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 616. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 617. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 618. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 619. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 620. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 621. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 622. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 623. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 624. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 625. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 626. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 627. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 628. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 629. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 630. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 631. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 632. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 633. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 634. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 635. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 636. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 637. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 638. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 639. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 640. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 641. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 642. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 643. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 644. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 645. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 646. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 647. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 648. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 649. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 650. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 651. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 652. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 653. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 654. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 655. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 656. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 657. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 658. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 659. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 660. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 661. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 662. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 663. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 664. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 665. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 666. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 667. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 668. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 669. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 670. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 671. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 672. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 673. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 674. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 675. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 676. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 677. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 678. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 679. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 680. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 681. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 682. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 683. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 684. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 685. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 686. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 687. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 688. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 689. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 690. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 691. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 692. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 693. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 694. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 695. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 696. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 697. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 698. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 699. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 700. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 701. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 702. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 703. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 704. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 705. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 706. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 707. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 708. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 709. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 710. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 711. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 712. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 713. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 714. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 715. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 716. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 717. Error Handling

The `StagingLambda` implements specific logic to handle errors and return appropriate HTTP responses. If an error occurs, the handler calls `_determine_final_error_response()` with the appropriate error code and message. The handler then returns the determined HTTP response based on the error code.

## 718. Security

The `StagingLambda` uses AWS IAM to ensure that only authorized users can invoke the function. The function's execution role must have the necessary permissions to interact with AWS services and to access secrets.

## 719. Monitoring

The `StagingLambda` uses AWS CloudWatch to monitor the function's performance and to log key events. The handler logs key events such as parsing results, validation outcomes, routing decisions, stage writes, lock attempts, and queue sends.

## 720. Signature Validation

The `StagingLambda` uses the Twilio Request Validator to ensure the authenticity of incoming webhook requests. The validator is initialized with the tenant's Twilio Auth Token, and it validates the signature of incoming requests. If the signature is invalid, the handler logs a critical error and returns a 200 TwiML response to prevent Twilio from retrying the request.

## 721. Secrets Manager

The `StagingLambda` retrieves the tenant's Twilio Auth Token from AWS Secrets Manager using the conversation-specific `credential_ref`. If the token is not found or access is denied, the handler logs a warning and proceeds to the next step.

## 722. DynamoDB

The `StagingLambda` interacts with the `ConversationsTable` to retrieve conversation-specific data and to persist incoming message details. The handler queries the table using GSI for credentials and retrieves the full conversation state using a GetItem operation.

## 723. SQS

The `StagingLambda` sends messages to SQS queues based on the routing decision. If the target queue is the Human Handoff Queue, the handler sends the full context message. If the target is a Channel Queue and the trigger lock is acquired, the handler sends a minimal trigger message with a delay.

## 724. Response

The `StagingLambda` returns a timely response to the webhook provider. If the operation is successful or if the error is non-transient, the handler returns a 200 OK response. If the error is transient, the handler raises an exception to trigger API GW 5xx -> Twilio retry.

## 