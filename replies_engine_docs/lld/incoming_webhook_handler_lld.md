# IncomingWebhookHandler Lambda - Low-Level Design

## 1. Purpose and Responsibilities

The IncomingWebhookHandler Lambda function serves as the unified entry point for all incoming webhook requests across different communication channels. Its primary responsibilities include:

- Receiving and processing webhooks from multiple channels (WhatsApp/SMS via Twilio, email via SendGrid)
- Parsing channel-specific payloads using a factory pattern approach
- Looking up existing conversation records in DynamoDB using the sender's identifier
- Placing incoming messages on appropriate SQS queues with a delay for batching
- Returning appropriate responses based on the originating channel

### 1.1 Design Goals

This Lambda function is designed with several key goals in mind:

1. **Channel Consolidation**: Centralize the initial processing of all communication channels into a single Lambda function to reduce complexity and maintenance overhead.

2. **Separation of Concerns**: While handling all channels initially, the function maintains clear boundaries between channel-specific logic through the parser factory design pattern.

3. **Message Batching**: Enable batching of rapid sequential messages by delaying processing and allowing multiple messages to accumulate.

4. **Standardization**: Convert different webhook formats into a common context object structure that can be consistently processed by downstream components.

5. **Fail-Safe Operation**: Include appropriate error handling and fallback mechanisms to ensure graceful behavior even in error conditions.

### 1.2 Relationship to Other Components

The IncomingWebhookHandler is positioned at the beginning of a two-stage webhook processing pipeline:

1. **Upstream**: Receives events directly from API Gateway, which performs initial request validation and throttling.

2. **Downstream**: 
   - Places messages onto SQS queues with a 30-second delay
   - Triggers BatchProcessorLambda functions after the delay period
   - BatchProcessor Lambdas then update DynamoDB and interact with AI services

3. **Dependencies**:
   - Relies on DynamoDB for conversation lookups (but not updates in this stage)
   - Uses SQS for message queuing and batching
   - BatchProcessor Lambdas handle DynamoDB updates after batching

## 2. Multi-Channel Processing Architecture

The Lambda implements a comprehensive architecture for handling webhooks from multiple channels using a common processing flow but with channel-specific adaptations.

### 2.1 Channel Identification and Routing

The Lambda function determines the channel type from the API Gateway event path. This approach simplifies API Gateway configuration while providing clear separation between different communication channels.

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

This function extracts the channel type directly from the API path, which offers several advantages:

1. **Configuration Simplicity**: API Gateway routes are naturally separated by path
2. **Clear Separation**: Each channel is distinctly identified
3. **Easy Extensibility**: Adding new channels requires only defining new paths and parser implementations
4. **Route-Based Authorization**: Allows for route-specific security policies at the API Gateway level

### 2.2 Parser Factory Pattern

To handle the diverse formats of different webhook providers, the Lambda implements a parser factory pattern. This pattern allows for:

1. **Encapsulation**: Channel-specific parsing details are isolated in their own classes
2. **Extensibility**: New channels can be added without modifying existing code
3. **Standardization**: All parsers expose a common interface regardless of the underlying webhook format
4. **Testability**: Each parser can be independently tested with mock webhook payloads

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

The factory method selects the appropriate parser implementation based on the channel type. This approach offers several benefits:

1. **Centralized Creation Logic**: The factory determines which parser to instantiate based on the channel type
2. **Code Organization**: The construction logic is separated from the parsing logic itself
3. **Consistent Interface**: Clients interact with all parsers through the same abstract interface
4. **Sharing Common Logic**: Twilio webhooks for both WhatsApp and SMS use the same parser since their format is identical

### 2.3 Channel-Specific Parsers

Each parser implements a common interface defined by the abstract `WebhookParser` class but handles channel-specific details internally. This allows the main Lambda handler to interact with all parsers in a uniform way while still accommodating the unique requirements of each channel.

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
```

The abstract base class defines two key methods that all parsers must implement:

1. **parse()**: Extracts and normalizes the webhook data into a standard format
2. **validate()**: Verifies the authenticity of the webhook using channel-specific validation

#### Twilio Webhook Parser

For Twilio-based channels (WhatsApp and SMS), the parser handles the form-encoded payload format and signature validation:

```python
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
```

The Twilio parser handles several important aspects:

1. **Payload Parsing**: Twilio sends data as URL-encoded form parameters that must be parsed
2. **Channel Detection**: Distinguishes between WhatsApp and SMS based on the format of the "From" field
3. **Number Formatting**: Extracts the pure phone number from prefixed formats like "whatsapp:+1234567890"
4. **Signature Validation**: Verifies the X-Twilio-Signature header to ensure the webhook is genuine
5. **Standardization**: Returns a normalized dictionary with consistent field names regardless of channel

#### SendGrid Webhook Parser

For email webhooks from SendGrid, a separate parser implementation handles their JSON payload structure and signature verification method:

```python
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

The specific implementation details for SendGrid webhooks would include:

1. **JSON Parsing**: SendGrid typically sends JSON-formatted payloads
2. **Signature Verification**: Using SendGrid-specific headers and validation algorithms
3. **Email Field Extraction**: Mapping email-specific fields to our standardized format
4. **Attachment Handling**: Optional logic for processing email attachments

## 3. Conversation Processing Flow

The conversation processing flow follows a two-stage Lambda architecture to enable message batching and efficient processing.

### 3.1 Lambda Handler Function

The main Lambda handler focuses on receiving, validating, and queueing messages rather than performing database updates.

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
        
        # Look up conversation in DynamoDB (only verify existence)
        conversation = lookup_conversation_exists(webhook_data['sender_id'], channel_type, logger)
        
        # Create a simplified message context for queueing
        message_context = create_message_context(webhook_data, conversation, logger)
        
        # Route to appropriate queue with delay for batching
        route_to_batch_queue(message_context, logger)
        
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

The key changes in this handler function are:

1. **Simplified Conversation Lookup**: Only verifies the conversation exists without retrieving the full record
2. **No Conversation Update**: The function does not update the conversation record
3. **Lighter Context Object**: Creates a simplified context with just the essential information
4. **Message Queueing**: Places the message on a queue with a delay for batching

### 3.2 Simplified Conversation Lookup

The lookup function is simplified to only verify existence without retrieving the full record:

```python
def lookup_conversation_exists(sender_id, channel_type, logger):
    """
    Verify that a conversation exists for this sender.
    
    Args:
        sender_id (str): Sender's identifier (phone number, email)
        channel_type (str): Communication channel type
        logger: Logger instance
    
    Returns:
        dict: Basic conversation metadata (ID, primary channel)
        
    Raises:
        ConversationNotFoundError: If no matching conversation is found
    """
    try:
        # Initialize DynamoDB client
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(os.environ['CONVERSATIONS_TABLE'])
        
        # Query by sender ID (phone number, email)
        response = table.query(
            IndexName='sender-id-index',
            KeyConditionExpression=Key('sender_id').eq(sender_id),
            ProjectionExpression='conversation_id, primary_channel, company_id, project_id',
            ScanIndexForward=False,
            Limit=1
        )
        
        if not response.get('Items'):
            logger.warning(f"No conversation found for {sender_id}")
            raise ConversationNotFoundError(f"No conversation found for {sender_id}")
        
        # Return minimal conversation metadata
        return response['Items'][0]
    
    except ClientError as e:
        logger.error(f"DynamoDB error: {str(e)}")
        raise
```

This function only retrieves the minimal information needed to create a message context and performs a more efficient query with projection expression.

### 3.3 Message Context Creation

Instead of creating a comprehensive context object, this function creates a lightweight message context:

```python
def create_message_context(webhook_data, conversation_metadata, logger):
    """
    Create a lightweight message context for queueing.
    
    Args:
        webhook_data (dict): Parsed webhook data
        conversation_metadata (dict): Basic conversation metadata
        logger: Logger instance
    
    Returns:
        dict: Message context
    """
    # Generate a unique message ID
    message_id = str(uuid.uuid4())
    
    # Build the message context
    context = {
        "meta": {
            "message_id": message_id,
            "channel_type": webhook_data['channel_type'],
            "timestamp": datetime.utcnow().isoformat(),
            "batch_id": None  # Will be set by batch processor
        },
        "conversation": {
            "conversation_id": conversation_metadata['conversation_id'],
            "primary_channel": conversation_metadata['primary_channel']
        },
        "message": {
            "from": webhook_data['sender_id'],
            "to": webhook_data['recipient_id'],
            "body": webhook_data['message_content'],
            "provider_message_id": webhook_data['message_id'],
            "timestamp": webhook_data['timestamp']
        },
        "company": {
            "company_id": conversation_metadata.get('company_id'),
            "project_id": conversation_metadata.get('project_id')
        }
    }
    
    logger.info("Created message context", 
               extra={"message_id": message_id, 
                     "conversation_id": conversation_metadata['conversation_id']})
    
    return context
```

This context contains only the essential information needed for batching and subsequent processing.

### 3.4 Queue Routing with Delay

The queue routing function now focuses on adding a delay for batching:

```python
def route_to_batch_queue(message_context, logger):
    """
    Route the message context to the appropriate batch queue with delay.
    
    Args:
        message_context (dict): The message context
        logger: Logger instance
    """
    # Get relevant data from context
    conversation_id = message_context['conversation']['conversation_id']
    channel_type = message_context['meta']['channel_type']
    
    # Initialize SQS client
    sqs = boto3.client('sqs')
    
    # Get environment variables and stage name
    stage = os.environ.get('STAGE', 'dev')
    
    # Route to appropriate batch queue
    queue_url = os.environ.get(f'{channel_type.upper()}_BATCH_QUEUE_URL')
    delay_seconds = 30  # 30-second delay for message batching
    
    # Send to the batch queue
    response = sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps(message_context),
        DelaySeconds=delay_seconds,
        MessageAttributes={
            'ConversationId': {
                'DataType': 'String',
                'StringValue': conversation_id
            }
        }
    )
    
    logger.info("Message sent to batch queue", 
               extra={"queue": queue_url, 
                      "message_id": message_context['meta']['message_id'],
                      "delay_seconds": delay_seconds,
                      "conversation_id": conversation_id})
```

This function adds message attributes to support filtering and correlation during batch processing.

## 3.5 Batch Processing (Second Lambda)

After the delay period, a second Lambda function processes messages in batches:

```python
def batch_processor_handler(event, context):
    """
    Handler for processing batched messages after delay.
    
    Args:
        event (dict): SQS event containing messages
        context (LambdaContext): AWS Lambda context
    """
    # Initialize logger
    logger = setup_logger(context)
    
    # Extract messages from event
    messages = [json.loads(record['body']) for record in event['Records']]
    logger.info(f"Processing {len(messages)} messages")
    
    # Group messages by conversation ID
    conversation_groups = {}
    for message in messages:
        conv_id = message['conversation']['conversation_id']
        if conv_id not in conversation_groups:
            conversation_groups[conv_id] = []
        conversation_groups[conv_id].append(message)
    
    # Process each conversation's message batch
    for conv_id, message_group in conversation_groups.items():
        # Sort messages by timestamp
        message_group.sort(key=lambda m: m['message']['timestamp'])
        
        # Generate batch ID
        batch_id = str(uuid.uuid4())
        
        # Assign batch ID to all messages in group
        for message in message_group:
            message['meta']['batch_id'] = batch_id
        
        try:
            # Update conversation in DynamoDB with all messages
            conversation = update_conversation_with_batch(conv_id, message_group, logger)
            
            # Create comprehensive context for processing
            context_object = create_processing_context(message_group, conversation, logger)
            
            # Route to appropriate processing queue
            route_to_processing_queue(context_object, logger)
            
        except Exception as e:
            logger.error(f"Failed to process batch for conversation {conv_id}: {str(e)}", 
                        extra={"batch_id": batch_id, "message_count": len(message_group)},
                        exc_info=True)
```

This batch processor groups messages by conversation ID, sorts them by timestamp, and updates the conversation record with all messages at once.

### 3.6 Batch Update to Conversation

The batch update function handles updating the conversation with multiple messages in a single operation:

```python
def update_conversation_with_batch(conversation_id, message_group, logger):
    """
    Update conversation with multiple messages in a single operation.
    
    Args:
        conversation_id (str): Conversation identifier
        message_group (list): Group of related messages
        logger: Logger instance
        
    Returns:
        dict: Updated conversation record
    """
    try:
        # Initialize DynamoDB client
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(os.environ['CONVERSATIONS_TABLE'])
        
        # Get full conversation record
        response = table.get_item(
            Key={'conversation_id': conversation_id}
        )
        
        if 'Item' not in response:
            raise ConversationNotFoundError(f"Conversation {conversation_id} not found")
        
        conversation = response['Item']
        
        # Format messages for database
        new_messages = []
        for message in message_group:
            new_messages.append({
                "message_id": message['meta']['message_id'],
                "direction": "INBOUND",
                "content": message['message']['body'],
                "timestamp": message['message']['timestamp'],
                "channel_type": message['meta']['channel_type'],
                "batch_id": message['meta']['batch_id'],
                "metadata": {
                    "sender": message['message']['from'],
                    "recipient": message['message']['to'],
                    "provider_message_id": message['message']['provider_message_id']
                }
            })
        
        # Update conversation with all messages
        update_response = table.update_item(
            Key={'conversation_id': conversation_id},
            UpdateExpression="SET messages = list_append(if_not_exists(messages, :empty_list), :new_messages), "
                            "conversation_status = :status, "
                            "last_user_message_at = :timestamp, "
                            "last_activity_at = :timestamp, "
                            "last_batch_id = :batch_id",
            ExpressionAttributeValues={
                ':new_messages': new_messages,
                ':empty_list': [],
                ':status': 'user_reply_received',
                ':timestamp': datetime.utcnow().isoformat(),
                ':batch_id': message_group[0]['meta']['batch_id']
            },
            ReturnValues="ALL_NEW"
        )
        
        logger.info(f"Conversation updated with {len(new_messages)} messages", 
                   extra={"conversation_id": conversation_id, 
                          "batch_id": message_group[0]['meta']['batch_id']})
        
        # Return updated conversation
        return update_response.get('Attributes', conversation)
        
    except ClientError as e:
        logger.error(f"Failed to update conversation: {str(e)}")
        raise
```

This function performs a single DynamoDB update to add all related messages to the conversation record.

## 4. Lambda Configuration and Resources

Properly sizing and configuring the Lambda function is crucial for reliable operation, especially given the critical role this function plays in the message processing pipeline.

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

The Lambda configuration includes carefully selected parameters based on the function's requirements:

1. **Memory Allocation (512MB)**:
   - Provides sufficient memory for processing webhook payloads of all expected sizes
   - Balances cost with performance needs
   - Higher memory also correlates with increased CPU allocation in Lambda
   - Based on profiling of typical webhook processing requirements

2. **Timeout Configuration (15 seconds)**:
   - Set well above the expected execution time to account for occasional latency spikes
   - Typical execution time is under 1 second but allows for:
     - DynamoDB retries during high-load periods
     - SQS queue backpressure scenarios
     - Secret Manager occasional higher latency
   - Still below API Gateway's 29-second timeout to ensure proper response handling

3. **Environment Variables**:
   - Extensive use of environment variables for configuration flexibility
   - Follows convention-based naming for queue URLs to support multiple channels
   - Includes stage information to support multi-environment deployments
   - Configurable logging level for different environments (verbose in dev, minimal in prod)

### 4.2 Performance Optimization

The Lambda function implements several performance optimizations to ensure reliable operation:

1. **Efficient Resource Utilization**:
   - Lambda memory (512MB) provides sufficient resources for webhook processing
   - Selectively imports only necessary modules to reduce cold start time
   - Initializes AWS clients outside the handler function for reuse across invocations

2. **DynamoDB Optimization**:
   - Uses single-table design with appropriate indices for fast lookups
   - Implements conditional updates to avoid race conditions
   - Sets request timeout to 2 seconds with retries to handle transient failures
   - Uses DynamoDB's atomic operations for consistent list updates

3. **SQS Configuration**:
   - Carefully sized visibility timeout (2 minutes) based on expected processing time
   - Pre-validates message size to ensure it's below SQS limits (256KB)
   - Implements backoff strategy for queue throttling scenarios

4. **Response Time Focus**:
   - Prioritizes early acknowledgment of webhooks to the sending service
   - Defers heavy processing to downstream Lambdas via SQS
   - Implements asynchronous patterns for non-critical operations

### 4.3 Cold Start Mitigation

Cold starts are a particular concern for webhook handlers as they can affect response time. Several strategies are implemented to minimize their impact:

1. **Code Optimization**:
   - Keeps dependencies minimal and carefully selected
   - Uses lightweight packages where possible
   - Implements lazy loading for infrequently used components

2. **AWS Client Initialization**:
   - Initializes AWS clients outside the handler function for reuse
   - Uses service-specific configurations for optimal performance

3. **Provisioned Concurrency**:
   - Recommended for production environments to maintain warm instances
   - Configuration can be adjusted based on traffic patterns
   - Cost-benefit analysis supports this for core messaging infrastructure

4. **Import Optimization**:
   - Organizes imports to minimize startup time
   - Prioritizes essential modules during initialization
   - Uses conditional imports for less-common execution paths

By implementing these strategies, the Lambda function can maintain consistent performance characteristics even under varying load conditions and minimize the impact of cold starts on webhook processing time.

## 5. Channel-Specific Response Handling

Each channel expects a specific response format when receiving webhooks. The Lambda function must return the appropriate response based on the channel type to ensure proper acknowledgment.

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

The response handling function addresses the unique requirements of each channel:

1. **Twilio (WhatsApp/SMS)**:
   - Returns a TwiML response in XML format
   - Sets Content-Type to 'text/xml' as required by Twilio
   - Uses an empty `<Response>` element to acknowledge receipt without additional actions
   - Always returns HTTP 200 even in some error cases to prevent Twilio retries

2. **Email Webhooks**:
   - Returns a JSON response with a success indicator
   - Sets Content-Type to 'application/json'
   - Format aligned with common email webhook provider expectations
   - Could be customized further based on specific provider requirements

3. **Default Fallback**:
   - Implements a sensible default for any new channels
   - Uses JSON format as the most universally accepted
   - Includes a success flag to indicate processing status

This approach ensures that each webhook provider receives an appropriate acknowledgment that follows their expected format and conventions.

## 6. Error Handling and Fallbacks

Robust error handling is essential for a webhook processing system. The Lambda implements comprehensive error management and fallback mechanisms to ensure graceful degradation.

### 6.1 Unknown Sender Handling

One common error scenario is receiving messages from unknown senders - numbers or email addresses not associated with any active conversation. The Lambda implements a graceful fallback:

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

The unknown sender handler implements several important strategies:

1. **Rate Limiting**:
   - Checks if a fallback has been sent recently to this sender
   - Uses DynamoDB with TTL (Time To Live) to automatically expire records
   - Prevents flooding senders with repeated responses
   - Default rate limit is one response per sender per 24 hours

2. **Channel-Specific Responses**:
   - Sends appropriate templated responses based on the channel type
   - WhatsApp responses leverage templates for notification allowance
   - SMS responses are concise to minimize costs
   - Email responses include appropriate subject lines and formatting

3. **Response Recording**:
   - Records when a fallback was sent to implement rate limiting
   - Sets appropriate TTL for automatic cleanup of records
   - Includes sender ID and timestamp for auditing

4. **Transparent Logging**:
   - Logs all unknown sender attempts for monitoring
   - Tracks both successful and rate-limited fallback attempts
   - Provides data for security monitoring (potential scanning attempts)

This approach balances security (not confirming invalid numbers) with service quality (informing genuine users who might be using expired conversation links).

### 6.2 Enhanced Exception Types

To facilitate clear error categorization and handling, the Lambda defines a hierarchy of custom exception types:

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

This exception hierarchy provides several benefits:

1. **Error Categorization**:
   - Creates a clear taxonomy of possible error conditions
   - Enables specific exception catching and handling
   - Supports precise error reporting and metrics

2. **Centralized Error Definition**:
   - All errors inherit from a common base class
   - Simplifies broad exception handling when needed
   - Clearly separates application errors from system errors

3. **Semantic Clarity**:
   - Exception names clearly communicate the error nature
   - Improves code readability and maintainability
   - Supports better debugging and troubleshooting

4. **Enhanced Logging**:
   - Enables detailed error logging with specific error types
   - Facilitates error aggregation in monitoring systems
   - Supports pattern recognition for recurring issues

These custom exception types are used throughout the Lambda to provide consistent error handling and reporting, improving both reliability and observability.

## 7. Deployment and Testing Considerations

### 7.1 Testing Strategy

A comprehensive testing strategy is essential for ensuring the reliability of the webhook handler:

- **Unit Tests**:
  - Each parser implementation should have dedicated unit tests
  - Test both success and failure paths for all main functions
  - Mock AWS services for isolated testing
  - Verify channel-specific processing logic

- **Integration Tests**:
  - Create tests with mock webhook payloads for each channel
  - Test end-to-end flow with local DynamoDB and SQS
  - Verify database updates and queue routing
  - Test error handling and fallback scenarios

- **Load Testing**:
  - Verify behavior under high concurrency
  - Test with varying payload sizes
  - Ensure memory and timeout settings are appropriate
  - Verify resource utilization under load

### 7.2 Monitoring and Metrics

Effective monitoring is crucial for operational awareness:

- **CloudWatch Metrics**:
  - Track invocation count by channel type
  - Monitor error rates by error type
  - Measure processing duration for performance analysis
  - Count queue routing success/failure rates
  - Track unknown sender occurrences for security monitoring

- **Alarms**:
  - Set up alarms for elevated error rates
  - Monitor for sudden changes in traffic patterns
  - Alert on repeated processing failures
  - Track DynamoDB and SQS throttling events

- **Dashboards**:
  - Create operational dashboards with key metrics
  - Include error breakdown by type
  - Show channel distribution of incoming webhooks
  - Display processing time trends

### 7.3 Logging Strategy

A structured logging approach provides valuable insights:

- **JSON-Structured Logs**:
  - Use consistent field names for all log entries
  - Include context fields like conversation_id and request_id
  - Add channel information for easy filtering
  - Include operation outcomes (success/failure)

- **Consistent Logging**:
  - Apply uniform logging patterns across components
  - Use appropriate log levels for different events
  - Include both technical and business context
  - Timestamp all operations for sequence reconstruction

- **Sensitive Information Handling**:
  - Redact all PII (Personally Identifiable Information)
  - Mask sensitive values in logs
  - Follow compliance requirements for data protection
  - Implement log retention policies as required

- **Level Appropriate Logging**:
  - Use DEBUG for detailed execution flow (dev/test only)
  - Use INFO for normal operations and state transitions
  - Use WARNING for unusual but non-critical conditions
  - Use ERROR for failures that affect message processing

This comprehensive logging strategy ensures that both operational and security teams have the visibility they need into system behavior.

## 8. Next Steps

1. Implement the IncomingWebhookHandler Lambda with multi-channel support
2. Create unit and integration tests for all parsers and workflows
3. Set up monitoring and alarming for production use
4. Implement additional channel parsers as needed
5. Document operational procedures for troubleshooting 