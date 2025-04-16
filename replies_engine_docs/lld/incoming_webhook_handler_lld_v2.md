# IncomingWebhookHandler Lambda - Low-Level Design v2

This document extends the original LLD with enhanced concurrency control mechanisms to handle simultaneous messages, message batching, and conversation locking.

## 1. Concurrency Control Design

### 1.1 Key Concurrency Challenges

The webhook processing system must address several key concurrency challenges:

1. **Simultaneous Messages from Same Sender**:
   - Multiple messages arriving within milliseconds from the same sender
   - Risk of conversation record being updated concurrently
   - Potential message reordering if not handled properly

2. **Message Processing Overlap**:
   - Initial webhook processing completes, but AI response generation is still in progress when a new message arrives
   - Status transitions could be confusing if not properly sequenced

3. **Cross-Channel Communication** (not currently in scope):
   - Messages arriving via different channels for the same conversation
   - Potential for conversation fragmentation or conflicting updates

### 1.2 Concurrency Control Strategy

To address these challenges, we implement a two-phase approach:

1. **Batching Window**: Collect messages arriving within a short timeframe (30 seconds)
2. **Conversation Locking**: Lock the conversation record during processing with status flags

This approach allows us to handle rapid sequential messages efficiently while preventing conflicts during AI processing.

## 2. Enhanced Webhook Processing Flow

### 2.1 Initial Validation Phase

```python
def lambda_handler(event, context):
    """
    Main handler for incoming webhooks with enhanced validation.
    """
    # Initialize logger with request ID
    logger = setup_logger(context)
    
    try:
        # Parse webhook and extract key data
        channel_type = determine_channel_type(event)
        parser = WebhookParserFactory.get_parser(channel_type, event, logger)
        
        # Validate webhook authenticity
        if not parser.validate():
            logger.warning("Invalid webhook signature")
            return create_error_response(401, "Invalid signature")
        
        webhook_data = parser.parse()
        
        # Perform critical validation checks
        conversation = validate_conversation(webhook_data['sender_id'], channel_type, logger)
        
        # Check if the conversation is currently being processed
        if conversation.get('conversation_status') == 'processing_reply':
            logger.warning("Conversation is currently being processed", 
                         extra={"conversation_id": conversation['conversation_id']})
            return handle_concurrent_message(channel_type, webhook_data, conversation)
        
        # Create and queue the message
        message_context = create_message_context(webhook_data, conversation, logger)
        route_to_batch_queue(message_context, logger)
        
        # Return appropriate response based on channel
        return create_channel_response(channel_type, True)
        
    except ValidationError as e:
        logger.warning(f"Validation error: {str(e)}")
        return create_error_response(400, str(e))
    
    except Exception as e:
        logger.error(f"Unhandled exception: {str(e)}", exc_info=True)
        return create_error_response(500, "Internal server error")
```

### 2.2 Enhanced Conversation Validation

The validation function performs multiple checks beyond simple existence:

```python
def validate_conversation(sender_id, channel_type, logger):
    """
    Perform comprehensive validation of the conversation.
    
    Args:
        sender_id (str): Sender's identifier
        channel_type (str): Communication channel type
        logger: Logger instance
    
    Returns:
        dict: Validated conversation metadata
        
    Raises:
        ConversationNotFoundError: If no matching conversation is found
        ValidationError: If conversation fails validation checks
    """
    try:
        # Initialize DynamoDB client
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(os.environ['CONVERSATIONS_TABLE'])
        
        # Query by sender ID with projection
        response = table.query(
            IndexName='sender-id-index',
            KeyConditionExpression=Key('sender_id').eq(sender_id),
            ProjectionExpression='conversation_id, primary_channel, conversation_status, '
                               'company_id, project_id, project_status, '
                               'auto_queue_reply_message, auto_queue_reply_message_from_number, '
                               'auto_queue_reply_message_from_email, allowed_channels',
            ScanIndexForward=False,
            Limit=1
        )
        
        if not response.get('Items'):
            logger.warning(f"No conversation found for {sender_id}")
            raise ConversationNotFoundError(f"No conversation found for {sender_id}")
        
        conversation = response['Items'][0]
        
        # Validate project status
        if conversation.get('project_status') != 'active':
            logger.warning(f"Project not active for conversation", 
                          extra={"conversation_id": conversation['conversation_id']})
            raise ValidationError("Project not active")
        
        # Validate channel is allowed
        allowed_channels = conversation.get('allowed_channels', [channel_type])
        if channel_type not in allowed_channels:
            logger.warning(f"Channel not allowed for conversation: {channel_type}", 
                          extra={"conversation_id": conversation['conversation_id']})
            raise ValidationError(f"Channel {channel_type} not allowed for this conversation")
        
        # Check conversation_status for processing status
        if conversation.get('conversation_status') == 'processing_reply':
            logger.info(f"Conversation is currently processing a response", 
                       extra={"conversation_id": conversation['conversation_id']})
        
        return conversation
    
    except ClientError as e:
        logger.error(f"DynamoDB error: {str(e)}")
        raise
```

### 2.3 Handling Concurrent Messages

When a message arrives while another is being processed:

```python
def handle_concurrent_message(channel_type, webhook_data, conversation):
    """
    Handle a message that arrived while the conversation is being processed.
    
    Args:
        channel_type (str): Communication channel type
        webhook_data (dict): Parsed webhook data
        conversation (dict): The conversation record
        
    Returns:
        dict: API Gateway response
    """
    sender_id = webhook_data['sender_id']
    
    # Send appropriate fallback message
    fallback_message = (
        "I'm sorry, but as I am an AI assistant, I can only process one message at a time. "
        "I am currently processing your first reply, so please allow me to respond before sending "
        "further messages. For best results, please reply with single responses during our chat."
    )
    
    # Send channel-specific fallback
    if channel_type == 'whatsapp':
        send_whatsapp_fallback(sender_id, fallback_message)
    elif channel_type == 'sms':
        send_sms_fallback(sender_id, fallback_message)
    elif channel_type == 'email':
        send_email_fallback(sender_id, fallback_message)
    
    # Log the concurrent message
    logger.info(f"Sent concurrent message fallback", 
               extra={
                   "conversation_id": conversation['conversation_id'],
                   "sender_id": sender_id,
                   "message_content": webhook_data['message_content'][:100] + '...' 
                                    if len(webhook_data['message_content']) > 100 else webhook_data['message_content']
               })
    
    # Return success response to the channel
    return create_channel_response(channel_type, True)
```

## 3. Batch Processing Implementation

### 3.1 Updated Batch Processor Handler

The batch processor now includes conversation locking before processing:

```python
def batch_processor_handler(event, context):
    """
    Handler for processing batched messages with conversation locking.
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
            # Lock conversation for processing
            conversation = lock_conversation_for_processing(conv_id, logger)
            
            # Update conversation with all messages
            conversation = update_conversation_with_batch(conversation, message_group, logger)
            
            # Create comprehensive context for processing
            context_object = create_processing_context(message_group, conversation, logger)
            
            # Route to appropriate processing queue
            route_to_processing_queue(context_object, logger)
            
        except ConversationLockError as e:
            logger.warning(f"Could not lock conversation {conv_id}: {str(e)}")
            # Re-queue the messages with delay for retry
            requeue_messages_with_delay(message_group, logger)
            
        except Exception as e:
            logger.error(f"Failed to process batch for conversation {conv_id}: {str(e)}", 
                        extra={"batch_id": batch_id, "message_count": len(message_group)},
                        exc_info=True)
            # If needed, unlock the conversation if we got past the locking phase
            try_unlock_conversation(conv_id, logger)
```

### 3.2 Conversation Locking Implementation

The locking mechanism ensures exclusive access during processing:

```python
def lock_conversation_for_processing(conversation_id, logger):
    """
    Lock the conversation by setting processing_reply status with conditional update.
    
    Args:
        conversation_id (str): Conversation identifier
        logger: Logger instance
        
    Returns:
        dict: Updated conversation record
        
    Raises:
        ConversationLockError: If the conversation cannot be locked
    """
    try:
        # Initialize DynamoDB client
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(os.environ['CONVERSATIONS_TABLE'])
        
        # Get the current timestamp for locking
        lock_timestamp = datetime.utcnow().isoformat()
        
        # Try to update with a condition that ensures the conversation is not already processing
        try:
            response = table.update_item(
                Key={'conversation_id': conversation_id},
                UpdateExpression="SET conversation_status = :processing_status, "
                                "processing_started_at = :timestamp",
                ConditionExpression="conversation_status <> :processing_status",
                ExpressionAttributeValues={
                    ':processing_status': 'processing_reply',
                    ':timestamp': lock_timestamp
                },
                ReturnValues="ALL_NEW"
            )
            
            logger.info(f"Conversation locked for processing", 
                       extra={"conversation_id": conversation_id, "timestamp": lock_timestamp})
            
            return response['Attributes']
            
        except ClientError as e:
            if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                logger.warning(f"Conversation already being processed", 
                              extra={"conversation_id": conversation_id})
                raise ConversationLockError(f"Conversation {conversation_id} is already being processed")
            raise
        
    except ClientError as e:
        logger.error(f"DynamoDB error: {str(e)}")
        raise
```

### 3.3 Conversation Update with Batched Messages

The update function now handles multiple messages in a single operation:

```python
def update_conversation_with_batch(conversation, message_group, logger):
    """
    Update conversation with multiple messages in a single operation.
    
    Args:
        conversation (dict): Current conversation record (already locked)
        message_group (list): Group of related messages
        logger: Logger instance
        
    Returns:
        dict: Updated conversation record
    """
    try:
        # Initialize DynamoDB client
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(os.environ['CONVERSATIONS_TABLE'])
        
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
        
        # Update conversation with all messages (maintaining the processing_reply status)
        update_response = table.update_item(
            Key={'conversation_id': conversation['conversation_id']},
            UpdateExpression="SET messages = list_append(if_not_exists(messages, :empty_list), :new_messages), "
                            "last_user_message_at = :timestamp, "
                            "last_activity_at = :timestamp, "
                            "last_batch_id = :batch_id",
            ExpressionAttributeValues={
                ':new_messages': new_messages,
                ':empty_list': [],
                ':timestamp': datetime.utcnow().isoformat(),
                ':batch_id': message_group[0]['meta']['batch_id']
            },
            ReturnValues="ALL_NEW"
        )
        
        logger.info(f"Conversation updated with {len(new_messages)} messages", 
                   extra={"conversation_id": conversation['conversation_id'], 
                          "batch_id": message_group[0]['meta']['batch_id']})
        
        # Return updated conversation
        return update_response.get('Attributes', conversation)
        
    except ClientError as e:
        logger.error(f"Failed to update conversation: {str(e)}")
        # Try to unlock the conversation on failure
        try_unlock_conversation(conversation['conversation_id'], logger)
        raise
```

### 3.4 Final Status Update After Processing

After AI processing and response sending, the conversation is unlocked:

```python
def unlock_conversation_after_processing(conversation_id, response_sent, logger):
    """
    Unlock the conversation after processing by setting the appropriate status.
    
    Args:
        conversation_id (str): Conversation identifier
        response_sent (bool): Whether the AI response was successfully sent
        logger: Logger instance
    """
    try:
        # Initialize DynamoDB client
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(os.environ['CONVERSATIONS_TABLE'])
        
        # Set the appropriate final status
        final_status = 'ai_response_sent' if response_sent else 'error_sending_response'
        
        # Update the conversation status
        response = table.update_item(
            Key={'conversation_id': conversation_id},
            UpdateExpression="SET conversation_status = :status, "
                            "processing_completed_at = :timestamp",
            ExpressionAttributeValues={
                ':status': final_status,
                ':timestamp': datetime.utcnow().isoformat()
            },
            ReturnValues="NONE"
        )
        
        logger.info(f"Conversation unlocked with status: {final_status}", 
                   extra={"conversation_id": conversation_id})
        
    except ClientError as e:
        logger.error(f"Failed to unlock conversation: {str(e)}", 
                    extra={"conversation_id": conversation_id})
        # This is a best-effort operation; we've already sent the response
        # Consider implementing an automated recovery mechanism for stuck conversations
```

## 4. Error Handling and Edge Cases

### 4.1 Message Requeuing for Contention

```python
def requeue_messages_with_delay(message_group, logger):
    """
    Requeue messages with a delay when conversation lock fails.
    
    Args:
        message_group (list): Group of messages to requeue
        logger: Logger instance
    """
    # Initialize SQS client
    sqs = boto3.client('sqs')
    
    # Get the queue URL based on the channel of the first message
    channel_type = message_group[0]['meta']['channel_type']
    queue_url = os.environ.get(f'{channel_type.upper()}_BATCH_QUEUE_URL')
    
    # Use a slightly longer delay for the retry
    delay_seconds = 45  # 45-second delay for retry
    
    for message in message_group:
        try:
            response = sqs.send_message(
                QueueUrl=queue_url,
                MessageBody=json.dumps(message),
                DelaySeconds=delay_seconds,
                MessageAttributes={
                    'ConversationId': {
                        'DataType': 'String',
                        'StringValue': message['conversation']['conversation_id']
                    },
                    'RetryAttempt': {
                        'DataType': 'Number',
                        'StringValue': '1'  # Increment this if implementing multiple retries
                    }
                }
            )
            
            logger.info("Message requeued due to lock contention", 
                       extra={"message_id": message['meta']['message_id'],
                              "delay_seconds": delay_seconds,
                              "conversation_id": message['conversation']['conversation_id']})
                              
        except Exception as e:
            logger.error(f"Failed to requeue message: {str(e)}", 
                        extra={"message_id": message['meta']['message_id']})
```

### 4.2 Deadlock Prevention

```python
def try_unlock_conversation(conversation_id, logger):
    """
    Attempt to unlock a conversation in an error scenario.
    This prevents conversations from getting stuck in processing state.
    
    Args:
        conversation_id (str): Conversation identifier
        logger: Logger instance
    """
    try:
        # Initialize DynamoDB client
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(os.environ['CONVERSATIONS_TABLE'])
        
        # Update the conversation status to an error state
        response = table.update_item(
            Key={'conversation_id': conversation_id},
            UpdateExpression="SET conversation_status = :status, "
                            "processing_error_at = :timestamp",
            ConditionExpression="conversation_status = :processing_status",
            ExpressionAttributeValues={
                ':status': 'processing_error',
                ':timestamp': datetime.utcnow().isoformat(),
                ':processing_status': 'processing_reply'
            }
        )
        
        logger.info(f"Emergency conversation unlock performed", 
                   extra={"conversation_id": conversation_id})
        
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            # Conversation was already unlocked or in a different state
            logger.info(f"Conversation already unlocked or in different state", 
                       extra={"conversation_id": conversation_id})
        else:
            logger.error(f"Failed emergency unlock: {str(e)}", 
                        extra={"conversation_id": conversation_id})
```

### 4.3 Lock Timeout Mechanism

To prevent permanently locked conversations:

```python
def check_and_reset_stalled_conversations():
    """
    Scheduled function to check for and reset stalled conversations.
    This should run as a separate scheduled Lambda function.
    """
    logger = setup_logger(None)
    logger.info("Checking for stalled conversations")
    
    try:
        # Initialize DynamoDB client
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(os.environ['CONVERSATIONS_TABLE'])
        
        # Calculate cutoff time (e.g., 5 minutes ago)
        cutoff_time = (datetime.utcnow() - timedelta(minutes=5)).isoformat()
        
        # Scan for conversations stuck in processing state
        response = table.scan(
            FilterExpression=Attr('conversation_status').eq('processing_reply') & 
                             Attr('processing_started_at').lt(cutoff_time),
            ProjectionExpression='conversation_id, processing_started_at'
        )
        
        stalled_count = 0
        for item in response.get('Items', []):
            conversation_id = item['conversation_id']
            started_at = item['processing_started_at']
            
            # Reset the conversation status
            table.update_item(
                Key={'conversation_id': conversation_id},
                UpdateExpression="SET conversation_status = :status, "
                                "processing_timeout_at = :current_time, "
                                "processing_started_at = :started_time",
                ExpressionAttributeValues={
                    ':status': 'processing_timeout',
                    ':current_time': datetime.utcnow().isoformat(),
                    ':started_time': started_at
                }
            )
            
            logger.warning(f"Reset stalled conversation", 
                          extra={"conversation_id": conversation_id, 
                                 "started_at": started_at})
            stalled_count += 1
        
        logger.info(f"Completed stalled conversation check", 
                   extra={"stalled_count": stalled_count})
        
    except Exception as e:
        logger.error(f"Failed to check for stalled conversations: {str(e)}")
```

## 5. Concurrency Considerations and Recommendations

### 5.1 Key Implementation Considerations

1. **Batch Window Duration**:
   - Current batch window is set to 30 seconds
   - Consider reducing to 10-20 seconds based on response time requirements
   - Monitor user interaction patterns to optimize this value

2. **Lock Timeout Handling**:
   - Implement automated recovery for stalled conversations
   - Consider a heart-beating mechanism for long-running processing
   - Alert operations on stuck conversations

3. **Message Requeuing Strategy**:
   - Current implementation retries once with a 45-second delay
   - Consider exponential backoff for multiple retries
   - Implement a dead-letter queue for messages that cannot be processed after multiple attempts

4. **Batching Implementation**:
   - Group messages by conversation ID in the batch processor
   - Sort by timestamp to maintain message order
   - Process each group in a single database transaction
   - Consider message size limits if batch could exceed 256KB (SQS limit)

### 5.2 Monitoring Recommendations

1. **Critical Metrics to Track**:
   - Concurrent message rejection rate
   - Average batch size (messages per conversation)
   - Conversation lock failures
   - Lock timeout occurrences
   - End-to-end processing time

2. **Alerting Thresholds**:
   - High concurrent message rejection rate (>10%)
   - Lock timeout occurrences (any is concerning)
   - Processing time exceeding expected thresholds

### 5.3 Future Enhancements

1. **Optimized Batch Processing**:
   - Consider intelligent batching that adjusts delay based on conversation activity
   - Implement priority queues for certain conversation types

2. **Enhanced Locking Mechanisms**:
   - Explore distributed locking alternatives like DynamoDB's transactions or AWS Step Functions
   - Implement finer-grained locks that prevent only specific operations

3. **User Experience Improvements**:
   - Provide real-time feedback on message status using WebSockets
   - Implement typing indicators in Twilio for better user feedback

## 6. Sample Sequence Diagram

```
┌─────┐          ┌────────────┐          ┌────────┐          ┌──────────────┐          ┌─────────┐
│Twilio│          │API Gateway │          │Lambda 1│          │SQS Batch Queue│          │Lambda 2 │
└──┬───┘          └─────┬──────┘          └───┬────┘          └───────┬──────┘          └────┬────┘
   │   Webhook (Msg1)    │                    │                       │                      │
   │──────────────────────>                   │                       │                      │
   │                     │    Trigger         │                       │                      │
   │                     │───────────────────>│                       │                      │
   │                     │                    │                       │                      │
   │                     │                    │ Validate Conversation │                      │
   │                     │                    │ & Check Lock Status   │                      │
   │                     │                    │───────────────────────>                      │
   │                     │                    │                       │                      │
   │  TwiML Response     │<───────────────────┤                       │                      │
   │<───────────────────│                    │                       │                      │
   │                     │                    │                       │                      │
   │   Webhook (Msg2)    │                    │                       │                      │
   │──────────────────────>                   │                       │                      │
   │                     │    Trigger         │                       │                      │
   │                     │───────────────────>│                       │                      │
   │                     │                    │                       │                      │
   │                     │                    │ Validate & Queue Msg2 │                      │
   │                     │                    │───────────────────────>                      │
   │                     │                    │                       │                      │
   │  TwiML Response     │<───────────────────┤                       │                      │
   │<───────────────────│                    │                       │                      │
   │                     │                    │                       │                      │
   │                     │                    │              After 30s delay                 │
   │                     │                    │                       │      Trigger         │
   │                     │                    │                       │─────────────────────>│
   │                     │                    │                       │                      │
   │                     │                    │                       │      Lock Conversation &
   │                     │                    │                       │      Group Messages  │
   │                     │                    │                       │                      │
   │                     │                    │                       │   Process Msg1+Msg2  │
   │                     │                    │                       │      as Batch        │
   │                     │                    │                       │                      │
   │                     │                    │                       │       AI Processing  │
   │                     │                    │                       │                      │
   │                     │                    │                       │                      │
   │   AI Response       │                    │                       │                      │
   │<──────────────────────────────────────────────────────────────────────────────────────┘
   │                     │                    │                       │
```

## 7. Next Steps

1. Implement the enhanced IncomingWebhookHandler Lambda with concurrency controls
2. Create the BatchProcessorLambda with batch grouping and conversation locking
3. Implement the lock timeout detection and recovery mechanism
4. Set up monitoring for key concurrency metrics
5. Develop and run load tests to validate the system under concurrent traffic 