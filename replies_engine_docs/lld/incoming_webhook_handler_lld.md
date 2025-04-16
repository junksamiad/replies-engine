# IncomingWebhookHandler Lambda - Low-Level Design

## 1. Purpose and Responsibilities

The IncomingWebhookHandler Lambda function serves as the first processing layer for incoming webhook requests from Twilio. Its primary responsibilities include:

- Validating that incoming webhooks are genuine Twilio requests using signature verification
- Parsing the webhook payload to extract essential information (sender number, message content)
- Looking up existing conversation records in DynamoDB using the sender's WhatsApp number
- Checking the conversation state to determine routing (AI processing vs. human handoff)
- Constructing appropriate messages and sending them to the correct SQS queue
- Returning appropriate responses to API Gateway for Twilio

## 2. Handler Function Structure

```python
def lambda_handler(event, context):
    """
    Main handler for incoming webhooks from Twilio.
    
    Args:
        event (dict): API Gateway event containing webhook data
        context (LambdaContext): AWS Lambda context
        
    Returns:
        dict: Response object for API Gateway
    """
    try:
        # Initialize logger
        logger = setup_logger(context)
        logger.info("Received webhook request")
        
        # Parse and validate the Twilio request
        twilio_request = parse_twilio_request(event, logger)
        validate_twilio_signature(event, twilio_request, logger)
        
        # Extract key information from the request
        sender_number = extract_sender_number(twilio_request)
        message_body = extract_message_body(twilio_request)
        
        # Look up conversation in DynamoDB
        conversation = lookup_conversation(sender_number, logger)
        
        # Determine routing based on conversation state
        if should_route_to_human(conversation):
            # Send to human handoff queue
            send_to_human_handoff_queue(twilio_request, conversation, logger)
        else:
            # Send to AI processing queue
            send_to_ai_processing_queue(twilio_request, conversation, logger)
        
        # Return success response to Twilio
        return create_twilio_response(True)
    
    except InvalidSignatureError:
        logger.warning("Invalid Twilio signature")
        return create_error_response(401, "Invalid signature")
    
    except ConversationNotFoundError:
        logger.warning(f"No conversation found for {sender_number}")
        # Consider how to handle unknown senders
        return create_twilio_response(True)  # Still return 200 to Twilio
    
    except Exception as e:
        logger.error(f"Unhandled exception: {str(e)}", exc_info=True)
        return create_error_response(500, "Internal server error")
```

## 3. Key Components and Modules

### 3.1 Request Parsing and Validation

#### Twilio Request Parsing

```python
def parse_twilio_request(event, logger):
    """
    Parse the API Gateway event to extract the Twilio webhook data.
    
    Args:
        event (dict): API Gateway event
        logger: Logger instance
        
    Returns:
        dict: Parsed Twilio request parameters
    """
    # Handle both application/x-www-form-urlencoded and JSON payloads
    if event.get('body'):
        # Check if body is URL-encoded form data
        body = event['body']
        content_type = get_header_value(event, 'Content-Type')
        
        if 'application/x-www-form-urlencoded' in content_type:
            # Parse URL-encoded form data
            return parse_qs(body)
        elif 'application/json' in content_type:
            # Parse JSON data
            return json.loads(body)
    
    logger.error("Unsupported or missing request body")
    raise InvalidRequestError("Unsupported or missing request body")
```

#### Twilio Signature Validation

```python
def validate_twilio_signature(event, parsed_request, logger):
    """
    Validate that the request came from Twilio using the X-Twilio-Signature header.
    
    Args:
        event (dict): API Gateway event
        parsed_request (dict): Parsed Twilio request
        logger: Logger instance
        
    Raises:
        InvalidSignatureError: If signature validation fails
    """
    # Extract the Twilio signature from headers
    twilio_signature = get_header_value(event, 'X-Twilio-Signature')
    if not twilio_signature:
        logger.warning("Missing X-Twilio-Signature header")
        raise InvalidSignatureError("Missing Twilio signature")
    
    # Get the full request URL from API Gateway event
    request_url = construct_request_url(event)
    
    # Get Twilio auth token from Secrets Manager
    auth_token = get_twilio_auth_token()
    
    # Validate the signature
    validator = RequestValidator(auth_token)
    if not validator.validate(request_url, parsed_request, twilio_signature):
        logger.warning("Invalid Twilio signature")
        raise InvalidSignatureError("Invalid Twilio signature")
    
    logger.info("Twilio signature validation successful")
```

### 3.2 Data Extraction

```python
def extract_sender_number(twilio_request):
    """
    Extract the sender's WhatsApp number from the Twilio request.
    
    Args:
        twilio_request (dict): Parsed Twilio request
        
    Returns:
        str: Sender's phone number in E.164 format
    """
    # Twilio sends WhatsApp numbers in the format "whatsapp:+1234567890"
    from_field = twilio_request.get('From', [''])[0]
    
    # Extract just the number part
    if from_field.startswith('whatsapp:'):
        return from_field.split('whatsapp:')[1]
    
    return from_field  # Fallback to the full value

def extract_message_body(twilio_request):
    """
    Extract the message body from the Twilio request.
    
    Args:
        twilio_request (dict): Parsed Twilio request
        
    Returns:
        str: Message body text
    """
    return twilio_request.get('Body', [''])[0]
```

### 3.3 DynamoDB Conversation Lookup

```python
def lookup_conversation(sender_number, logger):
    """
    Look up the conversation record in DynamoDB using the sender's number.
    
    Args:
        sender_number (str): Sender's phone number
        logger: Logger instance
        
    Returns:
        dict: Conversation record from DynamoDB
        
    Raises:
        ConversationNotFoundError: If no matching conversation is found
    """
    try:
        # Initialize DynamoDB client
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(os.environ['CONVERSATIONS_TABLE'])
        
        # Query by recipient_tel (phone number)
        # Use a GSI if not part of the main key
        response = table.query(
            IndexName='recipient-tel-index',  # Secondary index on recipient_tel
            KeyConditionExpression=Key('recipient_tel').eq(sender_number),
            ScanIndexForward=False,  # Sort by newest first if using a sort key
            Limit=1  # We just need the most recent conversation
        )
        
        if not response.get('Items'):
            logger.warning(f"No conversation found for {sender_number}")
            raise ConversationNotFoundError(f"No conversation found for {sender_number}")
        
        # Return the most recent conversation
        return response['Items'][0]
    
    except ClientError as e:
        logger.error(f"DynamoDB error: {str(e)}")
        raise
```

### 3.4 Routing Logic

```python
def should_route_to_human(conversation):
    """
    Determine if the conversation should be routed to human handoff.
    
    Args:
        conversation (dict): Conversation record from DynamoDB
        
    Returns:
        bool: True if should route to human, False otherwise
    """
    # Check the handoff_to_human flag in the conversation record
    return conversation.get('handoff_to_human', False)

def send_to_human_handoff_queue(twilio_request, conversation, logger):
    """
    Send the message to the human handoff SQS queue.
    
    Args:
        twilio_request (dict): Parsed Twilio request
        conversation (dict): Conversation record from DynamoDB
        logger: Logger instance
    """
    # Create the message payload
    payload = {
        'twilio_message': twilio_request,
        'conversation_id': conversation.get('conversation_id'),
        'thread_id': conversation.get('thread_id'),
        'company_id': conversation.get('company_id'),
        'project_id': conversation.get('project_id'),
        'timestamp': datetime.utcnow().isoformat(),
        'channel': 'whatsapp'
    }
    
    # Send to the human handoff queue
    sqs_client = boto3.client('sqs')
    queue_url = os.environ['HUMAN_HANDOFF_QUEUE_URL']
    
    response = sqs_client.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps(payload)
    )
    
    logger.info(f"Message sent to human handoff queue: {response['MessageId']}")

def send_to_ai_processing_queue(twilio_request, conversation, logger):
    """
    Send the message to the AI processing SQS queue.
    
    Args:
        twilio_request (dict): Parsed Twilio request
        conversation (dict): Conversation record from DynamoDB
        logger: Logger instance
    """
    # Create the message payload
    payload = {
        'twilio_message': twilio_request,
        'conversation_id': conversation.get('conversation_id'),
        'thread_id': conversation.get('thread_id'),
        'company_id': conversation.get('company_id'),
        'project_id': conversation.get('project_id'),
        'timestamp': datetime.utcnow().isoformat(),
        'channel': 'whatsapp'
    }
    
    # Send to the AI processing queue
    sqs_client = boto3.client('sqs')
    queue_url = os.environ['WHATSAPP_REPLIES_QUEUE_URL']
    
    response = sqs_client.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps(payload),
        DelaySeconds=30  # 30-second delay to allow for message batching
    )
    
    logger.info(f"Message sent to AI processing queue: {response['MessageId']}")
```

### 3.5 Response Generation

```python
def create_twilio_response(success=True):
    """
    Create an API Gateway response suitable for Twilio.
    
    Args:
        success (bool): Whether to return a success response
        
    Returns:
        dict: API Gateway response object
    """
    # Basic TwiML response that acknowledges receipt
    twiml_response = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
    
    return {
        'statusCode': 200,
        'headers': {
            'Content-Type': 'text/xml'
        },
        'body': twiml_response
    }

def create_error_response(status_code, message):
    """
    Create an error response for API Gateway.
    
    Args:
        status_code (int): HTTP status code
        message (str): Error message
        
    Returns:
        dict: API Gateway response object
    """
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json'
        },
        'body': json.dumps({
            'error': message
        })
    }
```

## 4. Custom Exception Types

```python
class WebhookHandlerError(Exception):
    """Base exception class for webhook handler errors."""
    pass

class InvalidRequestError(WebhookHandlerError):
    """Exception raised when the request format is invalid."""
    pass

class InvalidSignatureError(WebhookHandlerError):
    """Exception raised when the Twilio signature validation fails."""
    pass

class ConversationNotFoundError(WebhookHandlerError):
    """Exception raised when no matching conversation is found in DynamoDB."""
    pass
```

## 5. Configuration and Environment Variables

| Environment Variable | Description | Example Value |
|---------------------|-------------|--------------|
| `CONVERSATIONS_TABLE` | Name of the DynamoDB table storing conversations | `ai-multi-comms-conversations-dev` |
| `WHATSAPP_REPLIES_QUEUE_URL` | URL of the SQS queue for AI processing | `https://sqs.us-east-1.amazonaws.com/123456789012/ai-multi-comms-whatsapp-replies-queue-dev` |
| `HUMAN_HANDOFF_QUEUE_URL` | URL of the SQS queue for human handoff | `https://sqs.us-east-1.amazonaws.com/123456789012/ai-multi-comms-human-handoff-queue-dev` |
| `SECRETS_MANAGER_REGION` | AWS region for Secrets Manager | `us-east-1` |
| `TWILIO_AUTH_TOKEN_SECRET_ID` | ID of the Secret containing the Twilio auth token | `ai-multi-comms/whatsapp-credentials/company/project/twilio-auth-token` |
| `LOG_LEVEL` | Logging level | `INFO` |

## 6. File Structure

```
src_dev/
└── webhook_handler/
    └── app/
        ├── requirements.txt
        └── lambda_pkg/
            ├── __init__.py
            ├── index.py                # Main handler
            ├── utils/
            │   ├── __init__.py
            │   ├── logger.py           # Logging utilities
            │   ├── request_parser.py   # Request parsing utilities
            │   └── response_builder.py # Response utilities
            ├── services/
            │   ├── __init__.py
            │   ├── dynamodb_service.py # DynamoDB interactions
            │   ├── sqs_service.py      # SQS interactions
            │   └── secrets_service.py  # Secrets Manager interactions
            └── validators/
                ├── __init__.py
                └── twilio_validator.py # Signature validation
```

## 7. Dependencies

```
# AWS SDK
boto3==1.26.153
botocore==1.29.153

# API and Lambda Function
pydantic==2.0.2

# Twilio
twilio==8.5.0

# Utilities
structlog==23.1.0
```

## 8. Error Handling & Logging Strategy

### 8.1 Logging Strategy

- Use structured logging with `structlog` for easier log analysis
- Include conversation_id, request_id, and other context in all log entries
- Log at appropriate levels:
  - DEBUG: Details useful for debugging
  - INFO: Normal flow events
  - WARNING: Potential issues (like missing conversations)
  - ERROR: Issues that prevent normal processing

### 8.2 Error Handling Strategy

- Use custom exception types for clear error categorization
- Specific handling for each error type
- Catch-all for unexpected errors to prevent Lambda failures
- Return appropriate HTTP status codes based on error type
- Always log errors with full context

## 9. Performance Considerations

### 9.1 Lambda Configuration

- Memory: 256 MB (sufficient for webhook handling)
- Timeout: 10 seconds (more than enough for this processing)
- Concurrency: Default (can be increased if needed)

### 9.2 DynamoDB Access

- Use GSIs effectively to enable fast lookups by recipient phone number
- Implement retries with exponential backoff for DynamoDB operations

### 9.3 Cold Start Optimization

- Keep code size minimal
- Initialize AWS clients outside the handler function
- Consider Provisioned Concurrency for production

## 10. Security Considerations

### 10.1 Signature Validation

- Always validate Twilio signatures
- Use correct request URL in validation (including query parameters)
- Fail closed (reject requests if validation fails)

### 10.2 Secrets Management

- Store Twilio auth tokens in AWS Secrets Manager
- Use IAM role with least privilege access to secrets
- Do not log sensitive information

### 10.3 Input Validation

- Validate all input parameters
- Sanitize inputs to prevent injection attacks
- Validate payload structure

## 11. Testing Strategy

### 11.1 Unit Tests

```python
# Example unit test for signature validation
def test_validate_twilio_signature_valid():
    # Mock event with valid signature
    event = {
        'headers': {
            'X-Twilio-Signature': 'valid_signature'
        },
        'body': 'Body=Test&From=whatsapp%3A%2B1234567890',
        'requestContext': {
            'path': '/whatsapp',
            'domainName': 'example.execute-api.us-east-1.amazonaws.com',
            'stage': 'dev'
        }
    }
    
    # Mock dependencies
    parsed_request = {'Body': ['Test'], 'From': ['whatsapp:+1234567890']}
    logger = MagicMock()
    
    # Mock the validator
    with patch('twilio.request_validator.RequestValidator.validate', return_value=True):
        with patch('lambda_pkg.services.secrets_service.get_twilio_auth_token', return_value='mock_token'):
            # Should not raise exception
            validate_twilio_signature(event, parsed_request, logger)
```

### 11.2 Integration Tests

- Test with real DynamoDB tables (using localstack)
- Test with real SQS queues (using localstack)
- Test end-to-end flow with mock Twilio requests

### 11.3 Mock Strategies

- Mock AWS services for unit tests
- Use moto for AWS service mocking
- Create fixtures for common test data

## 12. Deployment Considerations

### 12.1 IAM Permissions

Minimum required permissions:
- `dynamodb:Query` on the ConversationsTable
- `sqs:SendMessage` on the WhatsAppRepliesQueue and HumanHandoffQueue
- `secretsmanager:GetSecretValue` on the Twilio auth token secret

### 12.2 Resource Naming

- Lambda Function: `${ProjectPrefix}-incoming-webhook-handler-${EnvironmentName}`
- Log Group: `/aws/lambda/${ProjectPrefix}-incoming-webhook-handler-${EnvironmentName}`

## 13. Happy Path Analysis

### 13.1 Preconditions

- API Gateway is configured to forward requests to this Lambda
- DynamoDB table contains conversation records
- SQS queues are configured and accessible
- Twilio auth token is stored in Secrets Manager

### 13.2 Flow

1. Webhook arrives from Twilio with valid signature
2. Lambda parses the request and extracts sender information
3. Lambda looks up the conversation in DynamoDB
4. Lambda determines routing based on handoff_to_human flag
5. Lambda sends the message to the appropriate SQS queue
6. Lambda returns a 200 OK response with TwiML

### 13.3 Expected Outcome

- Message is successfully sent to the correct queue
- Twilio receives a 200 OK response
- Complete transaction takes < 500ms

## 14. Unhappy Path Analysis

### 14.1 Invalid Signature

1. Webhook arrives with invalid signature
2. Signature validation fails
3. Lambda returns 401 Unauthorized
4. Error is logged

### 14.2 Conversation Not Found

1. Webhook arrives with valid signature
2. No matching conversation is found in DynamoDB
3. ConversationNotFoundError is caught
4. Lambda still returns a 200 OK to Twilio
5. Warning is logged
6. (Optional) Create a new conversation or implement specific handling

### 14.3 DynamoDB Failure

1. Webhook arrives with valid signature
2. DynamoDB query fails (service error)
3. Exception is caught
4. Lambda returns 500 Internal Server Error
5. Error is logged with full context

## 15. Next Steps

1. Implement Lambda code according to this design
2. Create unit tests for all components
3. Set up local test environment with localstack
4. Deploy to AWS using CLI
5. Test with mock Twilio requests
6. Document actual implementation details 