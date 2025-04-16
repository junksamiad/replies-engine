# ReplyProcessorLambda - Low-Level Design

## 1. Purpose and Responsibilities

The ReplyProcessorLambda function is the core component of the AI reply processing workflow in the replies-engine microservice. Its primary responsibilities include:

- Processing user messages from the WhatsApp Replies Queue
- Adding user messages to the corresponding OpenAI thread
- Running the appropriate OpenAI Assistant to generate a response
- Sending the AI-generated response back to the user via Twilio
- Updating the conversation record in DynamoDB with the message history
- Handling errors and implementing retry logic for transient failures

## 2. Handler Function Structure

```python
def lambda_handler(event, context):
    """
    Main handler for processing WhatsApp reply messages from SQS.
    
    Args:
        event (dict): SQS event containing message data
        context (LambdaContext): AWS Lambda context
        
    Returns:
        dict: Processing results
    """
    # Initialize logger
    logger = setup_logger(context)
    logger.info("Received SQS message for processing")
    
    # Process each record in the SQS batch (typically batch size will be 1)
    results = []
    for record in event.get('Records', []):
        try:
            # Parse the SQS message
            message = parse_sqs_message(record, logger)
            
            # Extract key information
            twilio_message = message.get('twilio_message', {})
            conversation_id = message.get('conversation_id')
            thread_id = message.get('thread_id')
            company_id = message.get('company_id')
            project_id = message.get('project_id')
            
            # Validate required fields
            validate_message_fields(message, logger)
            
            # 1. Add the user message to the OpenAI thread
            user_message = extract_user_message(twilio_message)
            add_message_to_thread(thread_id, user_message, logger)
            
            # 2. Run the OpenAI Assistant on the thread
            run_id = run_assistant_on_thread(thread_id, company_id, project_id, logger)
            
            # 3. Wait for the run to complete and get the response
            response = wait_for_assistant_response(thread_id, run_id, logger)
            
            # 4. Send the response back to the user via Twilio
            recipient = extract_recipient_number(twilio_message)
            message_sid = send_twilio_response(recipient, response, logger)
            
            # 5. Update the conversation record in DynamoDB
            update_conversation_record(
                conversation_id,
                thread_id,
                user_message,
                response,
                message_sid,
                logger
            )
            
            logger.info(f"Successfully processed message for conversation {conversation_id}")
            results.append({
                'conversation_id': conversation_id,
                'status': 'success'
            })
            
        except OpenAIError as e:
            logger.error(f"OpenAI API error: {str(e)}", exc_info=True)
            # Don't delete the message from the queue (will be retried)
            raise
            
        except TwilioError as e:
            logger.error(f"Twilio API error: {str(e)}", exc_info=True)
            # Don't delete the message from the queue (will be retried)
            raise
            
        except DynamoDBError as e:
            # If we got here, the message was sent to the user but we failed to update the DB
            logger.critical(f"Final DynamoDB update failed: {str(e)}", exc_info=True)
            # Consider this a critical error, but don't retry since the message was already sent
            results.append({
                'conversation_id': conversation_id,
                'status': 'error',
                'error': 'db_update_failed'
            })
            
        except Exception as e:
            logger.error(f"Unhandled exception: {str(e)}", exc_info=True)
            # Don't delete the message from the queue (will be retried)
            raise
    
    return {
        'batchItemFailures': [],  # SQS will delete all messages if this is empty
        'results': results
    }
```

## 3. Key Components and Modules

### 3.1 Message Parsing and Validation

```python
def parse_sqs_message(record, logger):
    """
    Parse the SQS message from the event record.
    
    Args:
        record (dict): SQS record from the event
        logger: Logger instance
        
    Returns:
        dict: Parsed message
    """
    try:
        # Extract the message body
        body = record.get('body', '{}')
        
        # Parse the JSON message
        message = json.loads(body)
        
        return message
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse SQS message: {str(e)}")
        raise MessageParsingError(f"Invalid JSON in SQS message: {str(e)}")

def validate_message_fields(message, logger):
    """
    Validate that the message contains all required fields.
    
    Args:
        message (dict): Parsed SQS message
        logger: Logger instance
        
    Raises:
        ValidationError: If any required fields are missing
    """
    required_fields = ['twilio_message', 'conversation_id', 'thread_id', 'company_id', 'project_id']
    
    for field in required_fields:
        if field not in message or not message[field]:
            logger.error(f"Missing required field: {field}")
            raise ValidationError(f"Missing required field: {field}")
    
    # Ensure twilio_message contains required fields
    twilio_message = message.get('twilio_message', {})
    if 'Body' not in twilio_message or 'From' not in twilio_message:
        logger.error("Missing required Twilio message fields")
        raise ValidationError("Twilio message missing Body or From fields")
```

### 3.2 OpenAI Integration

```python
def add_message_to_thread(thread_id, user_message, logger):
    """
    Add the user's message to the OpenAI thread.
    
    Args:
        thread_id (str): OpenAI thread ID
        user_message (str): User's message text
        logger: Logger instance
        
    Returns:
        str: Message ID
    """
    try:
        # Initialize OpenAI client with API key from Secrets Manager
        openai_client = get_openai_client()
        
        # Add message to thread
        response = openai_client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=user_message
        )
        
        logger.info(f"Added message to thread {thread_id}")
        return response.id
    except Exception as e:
        logger.error(f"Failed to add message to thread: {str(e)}")
        raise OpenAIError(f"Failed to add message to thread: {str(e)}")

def run_assistant_on_thread(thread_id, company_id, project_id, logger):
    """
    Run the OpenAI Assistant on the thread.
    
    Args:
        thread_id (str): OpenAI thread ID
        company_id (str): Company ID for fetching the assistant ID
        project_id (str): Project ID for fetching the assistant ID
        logger: Logger instance
        
    Returns:
        str: Run ID
    """
    try:
        # Get the assistant ID from configuration based on company/project
        assistant_id = get_assistant_id(company_id, project_id)
        
        # Initialize OpenAI client
        openai_client = get_openai_client()
        
        # Run the assistant on the thread
        run = openai_client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=assistant_id
        )
        
        logger.info(f"Started assistant run {run.id} on thread {thread_id}")
        return run.id
    except Exception as e:
        logger.error(f"Failed to run assistant: {str(e)}")
        raise OpenAIError(f"Failed to run assistant: {str(e)}")

def wait_for_assistant_response(thread_id, run_id, logger, max_wait_time=300):
    """
    Wait for the assistant run to complete and get the response.
    
    Args:
        thread_id (str): OpenAI thread ID
        run_id (str): Run ID
        logger: Logger instance
        max_wait_time (int): Maximum wait time in seconds
        
    Returns:
        str: Assistant's response
    """
    openai_client = get_openai_client()
    
    # Poll the run status with exponential backoff
    start_time = time.time()
    backoff = 1
    
    while (time.time() - start_time) < max_wait_time:
        try:
            # Get the run status
            run = openai_client.beta.threads.runs.retrieve(
                thread_id=thread_id,
                run_id=run_id
            )
            
            # Check if run is completed
            if run.status == "completed":
                # Get the latest assistant message
                messages = openai_client.beta.threads.messages.list(
                    thread_id=thread_id
                )
                
                # Find the first assistant message (newest messages come first)
                for message in messages.data:
                    if message.role == "assistant":
                        # Extract the text content from the message
                        content = message.content[0].text.value
                        logger.info(f"Received assistant response: {content[:50]}...")
                        return content
                
                logger.warning("No assistant message found after completed run")
                raise OpenAIError("No assistant message found after completed run")
            
            # If run failed or requires action, handle accordingly
            elif run.status == "failed":
                logger.error(f"Assistant run failed: {run.last_error}")
                raise OpenAIError(f"Assistant run failed: {run.last_error}")
            
            elif run.status == "requires_action":
                logger.error("Assistant run requires action, not supported in this flow")
                raise OpenAIError("Assistant run requires action, not supported in this flow")
            
            # If still running, wait with exponential backoff
            else:
                logger.debug(f"Run status: {run.status}, waiting {backoff}s before checking again")
                time.sleep(backoff)
                backoff = min(backoff * 2, 15)  # Exponential backoff with max of 15 seconds
        
        except Exception as e:
            logger.error(f"Error checking assistant run status: {str(e)}")
            raise OpenAIError(f"Error checking assistant run status: {str(e)}")
    
    # If we get here, we've timed out
    logger.error(f"Timed out waiting for assistant response after {max_wait_time}s")
    raise OpenAIError("Timed out waiting for assistant response")
```

### 3.3 Twilio Integration

```python
def send_twilio_response(recipient, response_text, logger):
    """
    Send the AI-generated response back to the user via Twilio.
    
    Args:
        recipient (str): Recipient's WhatsApp number
        response_text (str): AI-generated response text
        logger: Logger instance
        
    Returns:
        str: Twilio message SID
    """
    try:
        # Get Twilio credentials from Secrets Manager
        twilio_account_sid, twilio_auth_token, twilio_phone_number = get_twilio_credentials()
        
        # Initialize Twilio client
        twilio_client = Client(twilio_account_sid, twilio_auth_token)
        
        # Format recipient for WhatsApp (ensure it has the whatsapp: prefix)
        if not recipient.startswith('whatsapp:'):
            recipient = f'whatsapp:{recipient}'
        
        # Format sender for WhatsApp
        sender = f'whatsapp:{twilio_phone_number}'
        
        # Send the message
        message = twilio_client.messages.create(
            body=response_text,
            from_=sender,
            to=recipient
        )
        
        logger.info(f"Sent WhatsApp message: {message.sid}")
        return message.sid
    
    except TwilioRestException as e:
        logger.error(f"Twilio error: {str(e)}")
        raise TwilioError(f"Failed to send message via Twilio: {str(e)}")
```

### 3.4 DynamoDB Integration

```python
def update_conversation_record(conversation_id, thread_id, user_message, ai_response, message_sid, logger):
    """
    Update the conversation record in DynamoDB with the new messages.
    
    Args:
        conversation_id (str): Conversation ID
        thread_id (str): OpenAI thread ID
        user_message (str): User's message text
        ai_response (str): AI-generated response
        message_sid (str): Twilio message SID for the sent response
        logger: Logger instance
    """
    try:
        # Initialize DynamoDB client
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(os.environ['CONVERSATIONS_TABLE'])
        
        # Get the current time for the update
        current_time = datetime.utcnow().isoformat()
        
        # Define the conversation record update
        update_expression = """
        SET 
            updated_at = :updated_at,
            last_user_message = :user_message,
            last_ai_response = :ai_response,
            last_message_sid = :message_sid,
            message_count = if_not_exists(message_count, :zero) + :one
        """
        
        # Create a list of messages if it doesn't exist and append new messages
        update_expression += """
        SET 
            messages = list_append(
                if_not_exists(messages, :empty_list),
                :new_messages
            )
        """
        
        # Define expression attribute values
        expression_values = {
            ':updated_at': current_time,
            ':user_message': user_message,
            ':ai_response': ai_response,
            ':message_sid': message_sid,
            ':zero': 0,
            ':one': 1,
            ':empty_list': [],
            ':new_messages': [
                {
                    'role': 'user',
                    'content': user_message,
                    'timestamp': current_time
                },
                {
                    'role': 'assistant',
                    'content': ai_response,
                    'timestamp': current_time,
                    'message_sid': message_sid
                }
            ]
        }
        
        # Update the conversation record
        response = table.update_item(
            Key={
                'primary_channel': 'whatsapp',  # Hardcoded for now, could be made dynamic
                'conversation_id': conversation_id
            },
            UpdateExpression=update_expression,
            ExpressionAttributeValues=expression_values,
            ReturnValues='UPDATED_NEW'
        )
        
        logger.info(f"Updated conversation record: {conversation_id}")
        return response
    
    except ClientError as e:
        logger.error(f"DynamoDB error: {str(e)}")
        raise DynamoDBError(f"Failed to update conversation record: {str(e)}")
```

### 3.5 Helper Functions

```python
def extract_user_message(twilio_message):
    """
    Extract the user's message text from the Twilio message.
    
    Args:
        twilio_message (dict): Twilio message parameters
        
    Returns:
        str: User's message text
    """
    # Handle both potential formats (list or string)
    body = twilio_message.get('Body', [''])[0] if isinstance(twilio_message.get('Body'), list) else twilio_message.get('Body', '')
    return body

def extract_recipient_number(twilio_message):
    """
    Extract the recipient's phone number from the Twilio message.
    
    Args:
        twilio_message (dict): Twilio message parameters
        
    Returns:
        str: Recipient's phone number
    """
    # Get the 'From' field (sender's number) which becomes our recipient for the reply
    from_field = twilio_message.get('From', [''])[0] if isinstance(twilio_message.get('From'), list) else twilio_message.get('From', '')
    
    # Return the number, stripping the 'whatsapp:' prefix if present
    return from_field.replace('whatsapp:', '') if from_field.startswith('whatsapp:') else from_field

def get_assistant_id(company_id, project_id):
    """
    Get the OpenAI Assistant ID for the specified company and project.
    
    Args:
        company_id (str): Company ID
        project_id (str): Project ID
        
    Returns:
        str: OpenAI Assistant ID
    """
    # In a real implementation, this could fetch from DynamoDB or another config source
    # For now, use environment variables with a naming convention
    assistant_key = f"{company_id}_{project_id}_ASSISTANT_ID"
    assistant_id = os.environ.get(assistant_key)
    
    # Fallback to default assistant if specific one not found
    if not assistant_id:
        assistant_id = os.environ.get('DEFAULT_ASSISTANT_ID')
    
    if not assistant_id:
        raise ConfigurationError(f"No assistant ID found for {company_id}/{project_id}")
    
    return assistant_id
```

## 4. Custom Exception Types

```python
class ReplyProcessorError(Exception):
    """Base exception class for reply processor errors."""
    pass

class MessageParsingError(ReplyProcessorError):
    """Exception raised when SQS message parsing fails."""
    pass

class ValidationError(ReplyProcessorError):
    """Exception raised when message validation fails."""
    pass

class OpenAIError(ReplyProcessorError):
    """Exception raised for OpenAI API errors."""
    pass

class TwilioError(ReplyProcessorError):
    """Exception raised for Twilio API errors."""
    pass

class DynamoDBError(ReplyProcessorError):
    """Exception raised for DynamoDB errors."""
    pass

class ConfigurationError(ReplyProcessorError):
    """Exception raised for configuration errors."""
    pass
```

## 5. Configuration and Environment Variables

| Environment Variable | Description | Example Value |
|---------------------|-------------|--------------|
| `CONVERSATIONS_TABLE` | Name of the DynamoDB table storing conversations | `ai-multi-comms-conversations-dev` |
| `OPENAI_API_KEY_SECRET_ID` | ID of the Secret containing the OpenAI API key | `ai-multi-comms/openai-api-key/whatsapp` |
| `TWILIO_CREDENTIALS_SECRET_ID` | ID of the Secret containing Twilio credentials | `ai-multi-comms/whatsapp-credentials/twilio` |
| `DEFAULT_ASSISTANT_ID` | Default OpenAI Assistant ID for replies | `asst_abc123def456` |
| `SECRETS_MANAGER_REGION` | AWS region for Secrets Manager | `us-east-1` |
| `LOG_LEVEL` | Logging level | `INFO` |
| `{company_id}_{project_id}_ASSISTANT_ID` | Company-specific assistant IDs | `asst_123456789` |

## 6. File Structure

```
src_dev/
└── reply_processor/
    └── app/
        ├── requirements.txt
        └── lambda_pkg/
            ├── __init__.py
            ├── index.py                # Main handler
            ├── utils/
            │   ├── __init__.py
            │   ├── logger.py           # Logging utilities
            │   ├── parsers.py          # Message parsing utilities
            │   └── validators.py       # Validation utilities
            ├── services/
            │   ├── __init__.py
            │   ├── dynamodb_service.py # DynamoDB interactions
            │   ├── openai_service.py   # OpenAI API interactions
            │   ├── twilio_service.py   # Twilio API interactions
            │   └── secrets_service.py  # Secrets Manager interactions
            ├── models/
            │   ├── __init__.py
            │   ├── exceptions.py       # Custom exception types
            │   └── messages.py         # Message data models
            └── config/
                ├── __init__.py
                └── settings.py         # Configuration utilities
```

## 7. Dependencies

```
# AWS SDK
boto3==1.26.153
botocore==1.29.153

# API and Lambda Function
pydantic==2.0.2

# OpenAI
openai>=1.24.0,<2.0.0

# Twilio
twilio==8.5.0

# Utilities
structlog==23.1.0
```

## 8. Error Handling & Logging Strategy

### 8.1 Logging Strategy

- Use structured logging with `structlog` for easier log analysis
- Include conversation_id, thread_id, and other context in all log entries
- Log different message types at appropriate levels:
  - DEBUG: API request/response details, polling status
  - INFO: Normal operations (message received, processed, sent)
  - WARNING: Potential issues requiring attention
  - ERROR: Failed operations that trigger retries
  - CRITICAL: Failed operations after user communication has occurred

### 8.2 Error Handling Strategy

- Use custom exception types for clear error categorization
- Different handling based on when the error occurs in the process:
  - Errors before sending Twilio response: Raise exception to trigger retry via SQS
  - Errors after sending Twilio response: Log as critical but allow handler to complete
- Implement exponential backoff for OpenAI API calls
- Ensure Lambda returns appropriate SQS batch item failures when needed

## 9. Performance Considerations

### 9.1 Lambda Configuration

- Memory: 1024 MB (higher than webhook handler due to OpenAI integration)
- Timeout: 900 seconds (15 minutes to allow for long OpenAI assistant runs)
- Concurrency: Default (can be increased based on traffic patterns)

### 9.2 DynamoDB Access

- Use specific key lookups to minimize read capacity usage
- Implement conditional writes to prevent race conditions
- Consider retries with exponential backoff for DynamoDB operations

### 9.3 OpenAI API Performance

- Implement efficient polling with exponential backoff
- Set reasonable timeouts for OpenAI assistant runs
- Consider caching for frequently used configurations

## 10. Security Considerations

### 10.1 API Keys and Credentials

- Store all API keys and credentials in AWS Secrets Manager
- Fetch secrets only when needed
- Do not log sensitive information

### 10.2 Data Protection

- Sanitize user input before sending to OpenAI
- Implement appropriate retention policies for conversation data
- Consider PII/sensitive data handling in messages

### 10.3 Input Validation

- Validate all input parameters
- Implement proper error handling for malformed inputs
- Sanitize both incoming and outgoing messages

## 11. Testing Strategy

### 11.1 Unit Tests

```python
# Example unit test for OpenAI integration
def test_add_message_to_thread():
    # Mock logger
    logger = MagicMock()
    
    # Mock OpenAI client
    mock_openai_client = MagicMock()
    mock_message = MagicMock()
    mock_message.id = "msg_123"
    mock_openai_client.beta.threads.messages.create.return_value = mock_message
    
    # Mock the get_openai_client function
    with patch('lambda_pkg.services.openai_service.get_openai_client', return_value=mock_openai_client):
        # Call the function
        message_id = add_message_to_thread("thread_123", "Test message", logger)
        
        # Assert the OpenAI client was called correctly
        mock_openai_client.beta.threads.messages.create.assert_called_once_with(
            thread_id="thread_123",
            role="user",
            content="Test message"
        )
        
        # Assert the correct message ID was returned
        assert message_id == "msg_123"
```

### 11.2 Integration Tests

- Test with real DynamoDB tables (using localstack)
- Mock OpenAI and Twilio API responses
- Test end-to-end flow with simulated SQS messages

### 11.3 Validation Testing

- Test error handling for each potential failure point
- Validate behavior when OpenAI API is slow or unavailable
- Test with various message formats and edge cases

## 12. Deployment Considerations

### 12.1 IAM Permissions

Minimum required permissions:
- `dynamodb:GetItem` and `dynamodb:UpdateItem` on the ConversationsTable
- `secretsmanager:GetSecretValue` on OpenAI and Twilio credential secrets
- `sqs:DeleteMessage` and `sqs:ReceiveMessage` on the WhatsApp Replies Queue

### 12.2 Resource Naming

- Lambda Function: `${ProjectPrefix}-reply-processor-${EnvironmentName}`
- Log Group: `/aws/lambda/${ProjectPrefix}-reply-processor-${EnvironmentName}`

## 13. Happy Path Analysis

### 13.1 Preconditions

- Valid message in WhatsApp Replies Queue
- Valid OpenAI thread_id
- Valid conversation record in DynamoDB
- Working OpenAI and Twilio APIs
- All necessary credentials in Secrets Manager

### 13.2 Flow

1. SQS triggers the Lambda with the message
2. Lambda parses and validates the message
3. User's message is added to the OpenAI thread
4. OpenAI Assistant is run on the thread
5. Lambda waits for and retrieves the assistant's response
6. Response is sent back to the user via Twilio
7. Conversation record is updated in DynamoDB
8. Lambda returns successfully, SQS deletes the message

### 13.3 Expected Outcome

- User receives an AI-generated response via WhatsApp
- Conversation record is updated with the new messages
- Complete transaction takes < 15 seconds (typical case)

## 14. Unhappy Path Analysis

### 14.1 OpenAI API Failure

#### Flow
1. Lambda fails when calling OpenAI API
2. Exception is raised
3. SQS retains the message (not deleted)
4. Message becomes visible again after visibility timeout
5. Process is retried

#### Expected Outcome
- After temporary failures, message is retried
- After maxReceiveCount failures, message moves to DLQ
- No message is sent to the user until the OpenAI API succeeds

### 14.2 Twilio API Failure

#### Flow
1. OpenAI API succeeds
2. Lambda fails when calling Twilio API
3. Exception is raised
4. SQS retains the message (not deleted)
5. Message becomes visible again after visibility timeout
6. Process is retried

#### Expected Outcome
- Similar to OpenAI API failure
- No partial updates to DynamoDB occur

### 14.3 DynamoDB Failure After Message Sent

#### Flow
1. OpenAI API succeeds
2. Twilio API succeeds (message sent to user)
3. DynamoDB update fails
4. Critical error is logged
5. Lambda completes successfully (SQS message is deleted)

#### Expected Outcome
- User receives the response
- Conversation record is not updated
- Alert is triggered for operations team to investigate
- Overall process is considered complete (no retry)

## 15. Next Steps

1. Implement Lambda code according to this design
2. Create unit tests for all components
3. Set up local test environment with mocked services
4. Deploy to AWS using CLI
5. Test with real OpenAI and Twilio integrations
6. Implement monitoring and alerting
7. Document actual implementation details 