# IncomingWebhookHandler Lambda - Low-Level Design

## 1. Purpose and Responsibilities

The IncomingWebhookHandler Lambda function serves as the unified entry point for all incoming webhook requests across different communication channels. Its primary responsibilities include:

- Receiving and processing webhooks from multiple channels (WhatsApp/SMS via Twilio, email via SendGrid)
- Parsing channel-specific payloads using a factory pattern approach
- Looking up existing conversation records in DynamoDB using the sender's identifier
- Updating conversation records with incoming message content and metadata
- Creating a standardized context object for consistent processing
- Routing messages to the appropriate SQS queue based on conversation state and channel type
- Returning appropriate responses based on the originating channel

## 2. Multi-Channel Processing Architecture

### 2.1 Channel Identification and Routing

The Lambda function determines the channel type from the API Gateway event path:

```python
def determine_channel_type(event):
    """
    Determine the channel type based on the API Gateway path.
    
    Args:
        event (dict): API Gateway event
        
    Returns:
        str: Channel type (whatsapp, sms, email)
    """
    path = event.get('path', '').lower()
    
    if '/whatsapp' in path:
        return 'whatsapp'
    elif '/sms' in path:
        return 'sms'
    elif '/email' in path:
        return 'email'
    else:
        raise UnsupportedChannelError(f"Unsupported channel path: {path}")
```

### 2.2 Parser Factory Pattern

The Lambda implements a parser factory pattern to handle channel-specific webhook formats:

```python
class WebhookParserFactory:
    @staticmethod
    def get_parser(channel_type, event, logger):
        """
        Return the appropriate parser for the given channel type.
        
        Args:
            channel_type (str): The communication channel type
            event (dict): The API Gateway event
            logger: Logger instance
            
        Returns:
            WebhookParser: A parser instance for the specified channel
            
        Raises:
            UnsupportedChannelError: If the channel type is not supported
        """
        if channel_type == 'whatsapp' or channel_type == 'sms':
            return TwilioWebhookParser(event, logger)
        elif channel_type == 'email':
            return SendgridWebhookParser(event, logger)
        else:
            raise UnsupportedChannelError(f"Unsupported channel type: {channel_type}")
```

### 2.3 Channel-Specific Parsers

Each parser implements a common interface but handles channel-specific details:

```python
class WebhookParser:
    """Abstract base class for webhook parsers."""
    
    def __init__(self, event, logger):
        self.event = event
        self.logger = logger
    
    def parse(self):
        """
        Parse the webhook payload.
        
        Returns:
            dict: Standardized webhook data
        """
        raise NotImplementedError("Subclasses must implement parse()")
    
    def validate(self):
        """
        Validate the webhook authenticity.
        
        Returns:
            bool: True if valid, False otherwise
        """
        raise NotImplementedError("Subclasses must implement validate()")

class TwilioWebhookParser(WebhookParser):
    """Parser for Twilio webhooks (WhatsApp and SMS)."""
    
    def parse(self):
        """Parse the Twilio webhook payload."""
        body = self.event.get('body', '')
        # Parse application/x-www-form-urlencoded format
        parsed_body = parse_qs(body)
        
        # Determine if this is WhatsApp or SMS based on the From field
        from_field = parsed_body.get('From', [''])[0]
        is_whatsapp = from_field.startswith('whatsapp:')
        
        # Extract the sender ID (phone number)
        sender_id = from_field.split('whatsapp:')[1] if is_whatsapp else from_field
        
        return {
            'channel_type': 'whatsapp' if is_whatsapp else 'sms',
            'sender_id': sender_id,
            'recipient_id': parsed_body.get('To', [''])[0],
            'message_content': parsed_body.get('Body', [''])[0],
            'message_id': parsed_body.get('MessageSid', [''])[0],
            'timestamp': datetime.utcnow().isoformat(),
            'raw_payload': parsed_body
        }
    
    def validate(self):
        """Validate the Twilio signature."""
        # Extract Twilio signature from headers
        twilio_signature = self.event.get('headers', {}).get('X-Twilio-Signature')
        if not twilio_signature:
            self.logger.warning("Missing X-Twilio-Signature header")
            return False
        
        # Implementation of Twilio signature validation
        # (Detailed implementation omitted for brevity)
        return True

class SendgridWebhookParser(WebhookParser):
    """Parser for SendGrid email webhooks."""
    
    def parse(self):
        """Parse the SendGrid webhook payload."""
        # Implementation for email parsing
        # (Detailed implementation omitted for brevity)
        pass
    
    def validate(self):
        """Validate the SendGrid webhook signature."""
        # Implementation for email validation
        # (Detailed implementation omitted for brevity)
        pass
```

## 3. Conversation Processing Flow

### 3.1 Updated Handler Function

```python
def lambda_handler(event, context):
    """
    Main handler for incoming webhooks from all channels.
    
    Args:
        event (dict): API Gateway event containing webhook data
        context (LambdaContext): AWS Lambda context
        
    Returns:
        dict: Response object for API Gateway
    """
    # Initialize logger with request ID
    logger = setup_logger(context)
    logger.info("Received webhook request", extra={"event_path": event.get('path')})
    
    try:
        # Determine channel type from API Gateway path
        channel_type = determine_channel_type(event)
        logger.info(f"Processing {channel_type} webhook")
        
        # Use factory to get appropriate parser
        parser = WebhookParserFactory.get_parser(channel_type, event, logger)
        
        # Validate webhook authenticity
        if not parser.validate():
            logger.warning("Invalid webhook signature")
            return create_error_response(401, "Invalid signature")
        
        # Parse webhook data
        webhook_data = parser.parse()
        logger.info("Webhook parsed successfully", 
                   extra={"sender_id": webhook_data['sender_id'], 
                          "message_id": webhook_data['message_id']})
        
        # Look up conversation in DynamoDB
        conversation = lookup_conversation(webhook_data['sender_id'], channel_type, logger)
        
        # Update conversation with the incoming message
        update_conversation_with_message(conversation, webhook_data, logger)
        
        # Create the context object
        context_object = create_context_object(webhook_data, conversation, logger)
        
        # Route to appropriate queue based on conversation state and channel
        route_to_queue(context_object, logger)
        
        # Return appropriate response based on channel
        return create_channel_response(channel_type, True)
        
    except ConversationNotFoundError:
        logger.warning(f"No conversation found for sender", 
                      extra={"sender_id": webhook_data.get('sender_id', 'unknown')})
        # Handle unknown sender (implement rate-limited fallback)
        return handle_unknown_sender(channel_type, webhook_data)
    
    except Exception as e:
        logger.error(f"Unhandled exception: {str(e)}", exc_info=True)
        return create_error_response(500, "Internal server error")
```

### 3.2 Conversation Record Update

```python
def update_conversation_with_message(conversation, webhook_data, logger):
    """
    Update the conversation record in DynamoDB with the incoming message.
    
    Args:
        conversation (dict): Existing conversation record
        webhook_data (dict): Parsed webhook data
        logger: Logger instance
    
    Returns:
        dict: Updated conversation record
    """
    try:
        # Initialize DynamoDB client
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(os.environ['CONVERSATIONS_TABLE'])
        
        # Format the new message
        new_message = {
            "message_id": webhook_data['message_id'],
            "direction": "INBOUND",
            "content": webhook_data['message_content'],
            "timestamp": webhook_data['timestamp'],
            "channel_type": webhook_data['channel_type'],
            "metadata": {
                # Channel-specific metadata
                "sender": webhook_data['sender_id'],
                "recipient": webhook_data['recipient_id']
            }
        }
        
        # Update the conversation with the new message
        response = table.update_item(
            Key={
                'conversation_id': conversation['conversation_id'],
                'primary_channel': conversation['primary_channel']
            },
            UpdateExpression="SET messages = list_append(if_not_exists(messages, :empty_list), :new_message), "
                            "conversation_status = :status, "
                            "last_user_message_at = :timestamp, "
                            "last_activity_at = :timestamp",
            ExpressionAttributeValues={
                ':new_message': [new_message],
                ':empty_list': [],
                ':status': 'user_reply_received',
                ':timestamp': webhook_data['timestamp']
            },
            ReturnValues="ALL_NEW"
        )
        
        logger.info("Conversation updated with new message", 
                  extra={"conversation_id": conversation['conversation_id']})
        
        # Return the updated conversation
        return response.get('Attributes', conversation)
    
    except ClientError as e:
        logger.error(f"Failed to update conversation: {str(e)}")
        raise
```

### 3.3 Context Object Creation

```python
def create_context_object(webhook_data, conversation, logger):
    """
    Create a standardized context object for processing.
    
    Args:
        webhook_data (dict): Parsed webhook data
        conversation (dict): Conversation record
        logger: Logger instance
    
    Returns:
        dict: Context object
    """
    # Generate a unique request ID
    request_id = str(uuid.uuid4())
    
    # Build the context object
    context = {
        "meta": {
            "request_id": request_id,
            "channel_type": webhook_data['channel_type'],
            "timestamp": datetime.utcnow().isoformat(),
            "version": "1.0"
        },
        "conversation": {
            "conversation_id": conversation['conversation_id'],
            "primary_channel": conversation['primary_channel'],
            "conversation_status": conversation['conversation_status'],
            "hand_off_to_human": conversation.get('hand_off_to_human', False),
            "thread_id": conversation.get('thread_id')
        },
        "message": {
            "from": webhook_data['sender_id'],
            "to": webhook_data['recipient_id'],
            "body": webhook_data['message_content'],
            "message_id": webhook_data['message_id'],
            "timestamp": webhook_data['timestamp']
        },
        "company": {
            "company_id": conversation.get('company_id'),
            "project_id": conversation.get('project_id'),
            "company_name": conversation.get('company_name'),
            "credentials_reference": conversation.get('credentials_reference')
        },
        "processing": {
            "validation_status": "valid",
            "ai_response": None,
            "sent_response": None,
            "processing_timestamps": {
                "received": webhook_data['timestamp'],
                "validated": datetime.utcnow().isoformat(),
                "queued": None  # Will be set when queued
            }
        }
    }
    
    logger.info("Created context object", 
               extra={"request_id": request_id, "conversation_id": conversation['conversation_id']})
    
    return context
```

### 3.4 Queue Routing

```python
def route_to_queue(context_object, logger):
    """
    Route the context object to the appropriate SQS queue.
    
    Args:
        context_object (dict): The context object
        logger: Logger instance
    """
    # Get relevant data from context
    conversation = context_object['conversation']
    channel_type = context_object['meta']['channel_type']
    
    # Initialize SQS client
    sqs = boto3.client('sqs')
    
    # Get environment variables and stage name
    stage = os.environ.get('STAGE', 'dev')
    
    # Determine which queue to use based on conversation state
    if conversation.get('hand_off_to_human', False):
        # Route to human handoff queue
        queue_url = os.environ.get(f'{channel_type.upper()}_HANDOFF_QUEUE_URL')
        delay_seconds = 0  # No delay for human handoff
    else:
        # Route to AI processing queue
        queue_url = os.environ.get(f'{channel_type.upper()}_REPLIES_QUEUE_URL')
        delay_seconds = 30  # 30-second delay for message batching
    
    # Update the queued timestamp
    context_object['processing']['processing_timestamps']['queued'] = datetime.utcnow().isoformat()
    
    # Send to the queue
    response = sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps(context_object),
        DelaySeconds=delay_seconds
    )
    
    logger.info("Message sent to queue", 
               extra={"queue": queue_url, 
                      "message_id": response['MessageId'],
                      "conversation_id": conversation['conversation_id']})
```

## 4. Lambda Configuration and Resources

### 4.1 Memory and Timeout Settings

```yaml
Resources:
  IncomingWebhookHandlerFunction:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: ./src/webhook_handler
      Handler: app.lambda_handler
      Runtime: python3.9
      MemorySize: 512  # Sufficient for webhook processing
      Timeout: 15      # 15-second timeout
      Environment:
        Variables:
          CONVERSATIONS_TABLE: !Ref ConversationsTable
          WHATSAPP_REPLIES_QUEUE_URL: !Ref WhatsAppRepliesQueue
          SMS_REPLIES_QUEUE_URL: !Ref SmsRepliesQueue
          EMAIL_REPLIES_QUEUE_URL: !Ref EmailRepliesQueue
          WHATSAPP_HANDOFF_QUEUE_URL: !Ref WhatsAppHandoffQueue
          SMS_HANDOFF_QUEUE_URL: !Ref SmsHandoffQueue
          EMAIL_HANDOFF_QUEUE_URL: !Ref EmailHandoffQueue
          STAGE: !Ref Stage
          LOG_LEVEL: INFO
```

### 4.2 Performance Optimization

- Size the Lambda appropriately to handle all channel types
- The memory allocation of 512MB provides sufficient resources for webhook processing
- The timeout value of 15 seconds accommodates all possible execution paths
- DynamoDB request timeouts are set to 2 seconds with retries
- SQS message size limits are respected (max 256KB)

### 4.3 Cold Start Mitigation

- Keep dependencies minimal
- Initialize AWS clients outside the handler function
- Consider using Provisioned Concurrency for production
- Optimize import statements for faster initialization

## 5. Channel-Specific Response Handling

```python
def create_channel_response(channel_type, success=True):
    """
    Create an API Gateway response suitable for the specific channel.
    
    Args:
        channel_type (str): The communication channel type
        success (bool): Whether to return a success response
        
    Returns:
        dict: API Gateway response object
    """
    if channel_type in ['whatsapp', 'sms']:
        # Twilio expects a TwiML response
        response_body = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
        content_type = 'text/xml'
    elif channel_type == 'email':
        # Email webhook response
        response_body = json.dumps({'success': success})
        content_type = 'application/json'
    else:
        # Default JSON response
        response_body = json.dumps({'success': success})
        content_type = 'application/json'
    
    return {
        'statusCode': 200,
        'headers': {
            'Content-Type': content_type
        },
        'body': response_body
    }
```

## 6. Error Handling and Fallbacks

### 6.1 Unknown Sender Handling

```python
def handle_unknown_sender(channel_type, webhook_data):
    """
    Handle messages from unknown senders with rate-limited fallbacks.
    
    Args:
        channel_type (str): The communication channel type
        webhook_data (dict): Parsed webhook data
        
    Returns:
        dict: API Gateway response
    """
    sender_id = webhook_data['sender_id']
    
    # Check if we've already sent a fallback recently using DynamoDB with TTL
    if has_received_fallback_recently(sender_id):
        logger.info(f"Ignoring repeat unknown sender", extra={"sender_id": sender_id})
        return create_channel_response(channel_type, True)
    
    # Send appropriate channel-specific fallback
    if channel_type == 'whatsapp':
        send_whatsapp_fallback(sender_id)
    elif channel_type == 'sms':
        send_sms_fallback(sender_id)
    elif channel_type == 'email':
        send_email_fallback(sender_id)
    
    # Record fallback timestamp with TTL
    record_fallback_sent(sender_id)
    
    # Return success response to the channel
    return create_channel_response(channel_type, True)
```

### 6.2 Enhanced Exception Types

```python
class WebhookHandlerError(Exception):
    """Base exception class for webhook handler errors."""
    pass

class UnsupportedChannelError(WebhookHandlerError):
    """Exception raised when the channel type is not supported."""
    pass

class ValidationError(WebhookHandlerError):
    """Exception raised when webhook validation fails."""
    pass

class ConversationNotFoundError(WebhookHandlerError):
    """Exception raised when no matching conversation is found."""
    pass

class QueueRoutingError(WebhookHandlerError):
    """Exception raised when message cannot be routed to a queue."""
    pass
```

## 7. Deployment and Testing Considerations

### 7.1 Testing Strategy

- Unit tests for each parser implementation
- Integration tests with mock webhook payloads
- End-to-end tests with real API Gateway events
- Load testing to verify memory and timeout settings

### 7.2 Monitoring and Metrics

- CloudWatch metrics for:
  - Invocation count by channel type
  - Error rates by error type
  - Processing duration
  - Queue routing success/failure
  - Unknown sender count

### 7.3 Logging Strategy

- JSON-structured logs with context fields
- Consistent logging across all components
- Redaction of sensitive information
- Log levels appropriate to the operation

## 8. Next Steps

1. Implement the IncomingWebhookHandler Lambda with multi-channel support
2. Create unit and integration tests for all parsers and workflows
3. Set up monitoring and alarming for production use
4. Implement additional channel parsers as needed
5. Document operational procedures for troubleshooting 