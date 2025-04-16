# SQS Queues - Low-Level Design

## 1. Purpose and Responsibilities

The SQS queues in the replies-engine microservice serve as message brokers between different processing stages. They provide:

- Decoupling of the webhook reception from message processing
- Buffering of messages to handle traffic spikes
- Delayed processing to enable message batching
- Dead-letter handling for fault tolerance
- Separate paths for human handoff vs. AI processing

This LLD focuses on two primary queues:
1. **WhatsApp Replies Queue** - For messages to be processed by the AI
2. **Human Handoff Queue** - For messages to be handled by human operators

## 2. Queue Configurations

### 2.1 WhatsApp Replies Queue

```yaml
WhatsAppRepliesQueue:
  Type: AWS::SQS::Queue
  Properties:
    QueueName: !Sub '${ProjectPrefix}-whatsapp-replies-queue-${EnvironmentName}'
    DelaySeconds: 30  # 30-second delay for message batching
    VisibilityTimeout: 905  # Slightly higher than Lambda timeout (900s)
    MessageRetentionPeriod: 345600  # 4 days (in seconds)
    RedrivePolicy:
      deadLetterTargetArn: !GetAtt WhatsAppRepliesDLQ.Arn
      maxReceiveCount: 3  # Retry failed processing 3 times before moving to DLQ
```

#### Configuration Rationale

- **DelaySeconds (30s)**: Provides a delay window to allow multiple messages from the same user to arrive before processing begins. This enables more contextual AI responses when a user sends multiple messages in quick succession.
- **VisibilityTimeout (905s)**: Set higher than the Lambda function timeout (900s) to prevent duplicate processing if the Lambda takes the maximum allowed time.
- **MessageRetentionPeriod (4 days)**: Provides enough time for operational recovery if there are issues with message processing, without keeping messages indefinitely.
- **maxReceiveCount (3)**: Allows multiple processing attempts before considering a message unprocessable.

### 2.2 WhatsApp Replies Dead-Letter Queue (DLQ)

```yaml
WhatsAppRepliesDLQ:
  Type: AWS::SQS::Queue
  Properties:
    QueueName: !Sub '${ProjectPrefix}-whatsapp-replies-dlq-${EnvironmentName}'
    MessageRetentionPeriod: 1209600  # 14 days (in seconds)
```

#### Configuration Rationale

- **MessageRetentionPeriod (14 days)**: Longer retention period for failed messages to allow sufficient time for investigation and manual reprocessing.

### 2.3 Human Handoff Queue

```yaml
HumanHandoffQueue:
  Type: AWS::SQS::Queue
  Properties:
    QueueName: !Sub '${ProjectPrefix}-human-handoff-queue-${EnvironmentName}'
    DelaySeconds: 0  # No delay for human intervention
    VisibilityTimeout: 300  # 5 minutes (in seconds)
    MessageRetentionPeriod: 345600  # 4 days (in seconds)
    RedrivePolicy:
      deadLetterTargetArn: !GetAtt HumanHandoffDLQ.Arn
      maxReceiveCount: 3
```

#### Configuration Rationale

- **DelaySeconds (0)**: No delay for human handoff messages to ensure immediate attention.
- **VisibilityTimeout (300s)**: Shorter timeout as human handoff processing is typically quicker than AI processing.
- **MessageRetentionPeriod (4 days)**: Consistent with the WhatsApp Replies Queue.

### 2.4 Human Handoff Dead-Letter Queue (DLQ)

```yaml
HumanHandoffDLQ:
  Type: AWS::SQS::Queue
  Properties:
    QueueName: !Sub '${ProjectPrefix}-human-handoff-dlq-${EnvironmentName}'
    MessageRetentionPeriod: 1209600  # 14 days (in seconds)
```

## 3. Message Format and Schema

### 3.1 Message Schema for WhatsApp Replies Queue

```json
{
  "twilio_message": {
    "Body": ["User message text"],
    "From": ["whatsapp:+1234567890"],
    "To": ["whatsapp:+0987654321"],
    "SmsMessageSid": ["SM..."],
    "MessageSid": ["SM..."],
    "AccountSid": ["AC..."],
    "NumMedia": ["0"],
    "NumSegments": ["1"],
    "SmsStatus": ["received"],
    "ApiVersion": ["2010-04-01"]
    // ... other Twilio webhook parameters
  },
  "conversation_id": "abc123def456",
  "thread_id": "thread_abc123def456",
  "company_id": "company_123",
  "project_id": "project_456",
  "timestamp": "2023-07-21T14:30:45.123Z",
  "channel": "whatsapp"
}
```

### 3.2 Message Schema for Human Handoff Queue

```json
{
  "twilio_message": {
    // Same format as WhatsApp Replies Queue
  },
  "conversation_id": "abc123def456",
  "thread_id": "thread_abc123def456",
  "company_id": "company_123",
  "project_id": "project_456",
  "timestamp": "2023-07-21T14:30:45.123Z",
  "channel": "whatsapp",
  "handoff_reason": "user_requested" // Optional field indicating why handoff occurred
}
```

## 4. Queue Access Patterns

### 4.1 Send Operations

| Component | Operation | Queue |
|-----------|-----------|-------|
| IncomingWebhookHandler Lambda | SendMessage | WhatsApp Replies Queue or Human Handoff Queue (based on handoff_to_human flag) |
| Manual Reprocessing Tool (future) | SendMessage | WhatsApp Replies Queue (for retrying failed messages) |

### 4.2 Receive Operations

| Component | Operation | Queue |
|-----------|-----------|-------|
| ReplyProcessorLambda | ReceiveMessage | WhatsApp Replies Queue |
| Human Interface (future) | ReceiveMessage | Human Handoff Queue |

## 5. Message Lifecycle

### 5.1 WhatsApp Replies Queue Lifecycle

1. **Creation**: Message created by IncomingWebhookHandler Lambda
2. **Delay Period**: Message becomes invisible for 30 seconds (DelaySeconds)
3. **Available**: Message becomes available for processing
4. **In Flight**: Message is retrieved by ReplyProcessorLambda and becomes invisible for VisibilityTimeout duration
5. **Processing Outcomes**:
   - **Success**: Message is deleted from the queue after successful processing
   - **Failure**: Message returns to the queue after VisibilityTimeout expires
   - **Repeated Failure**: After maxReceiveCount failures, message moves to DLQ

### 5.2 Human Handoff Queue Lifecycle

1. **Creation**: Message created by IncomingWebhookHandler Lambda
2. **Available**: Message immediately available for processing (no delay)
3. **In Flight**: Message is retrieved by Human Interface System and becomes invisible for VisibilityTimeout duration
4. **Processing Outcomes**:
   - **Success**: Message is deleted from the queue after successful processing
   - **Failure**: Similar to WhatsApp Replies Queue

## 6. Error Handling & Dead Letter Queues

### 6.1 Dead Letter Queue Strategy

- Messages are sent to the DLQ after 3 failed processing attempts
- DLQs retain messages for 14 days to allow time for investigation
- DLQ monitoring with CloudWatch alarms will trigger notifications

### 6.2 DLQ Handling Process

1. Operations team receives alert about messages in DLQ
2. Messages are examined to determine cause of failure
3. After issue resolution, messages can be manually moved back to the primary queue for reprocessing
4. Metrics are collected for failure analysis and system improvement

## 7. Performance Considerations

### 7.1 Throughput

- Default SQS throughput (unlimited messages per second) is sufficient for expected load
- No need for throughput provisioning initially

### 7.2 Latency

- Standard queue (non-FIFO) used as exact ordering is not critical
- 30-second delay introduces intentional latency for message batching benefits
- Expected end-to-end latency for happy path: ~35-40 seconds (30s delay + ~5-10s processing)

### 7.3 Batch Processing

- SQS batching features (SendMessageBatch, ReceiveMessage with MaxNumberOfMessages) should be used for efficiency
- ReplyProcessorLambda will be configured with batch size of 1 initially (can be increased if needed)

## 8. Monitoring & Alerting

### 8.1 Key Metrics to Monitor

| Metric | Description | Threshold | Alert |
|--------|-------------|-----------|-------|
| ApproximateNumberOfMessagesVisible | Number of messages available for retrieval | > 100 for > 5 minutes | Warning |
| ApproximateNumberOfMessagesNotVisible | Number of messages in flight | > 50 for > 10 minutes | Warning |
| ApproximateAgeOfOldestMessage | Age of the oldest message in the queue | > 1 hour | Warning |
| NumberOfMessagesReceived | Message arrival rate | N/A | No alert (monitoring only) |
| NumberOfMessagesSent | Message dispatch rate | N/A | No alert (monitoring only) |
| NumberOfMessagesDeleted | Message completion rate | N/A | No alert (monitoring only) |
| ApproximateNumberOfMessagesVisible (DLQ) | Messages in DLQ | > 0 | Critical |

### 8.2 CloudWatch Alarms

```yaml
WhatsAppRepliesDLQNotEmptyAlarm:
  Type: AWS::CloudWatch::Alarm
  Properties:
    AlarmName: !Sub '${ProjectPrefix}-WhatsAppRepliesDLQ-NotEmpty-${EnvironmentName}'
    AlarmDescription: 'Alarm when any messages appear in the WhatsApp Replies Dead Letter Queue'
    Namespace: 'AWS/SQS'
    MetricName: 'ApproximateNumberOfMessagesVisible'
    Dimensions:
      - Name: QueueName
        Value: !GetAtt WhatsAppRepliesDLQ.QueueName
    Statistic: Sum
    Period: 60
    EvaluationPeriods: 1
    Threshold: 0
    ComparisonOperator: GreaterThan
    TreatMissingData: notBreaching
    AlarmActions:
      - !Ref AlertsNotificationTopic
```

## 9. Security Considerations

### 9.1 Encryption

- Server-side encryption (SSE) will be enabled for all queues
- AWS managed KMS keys will be used for encryption

```yaml
WhatsAppRepliesQueue:
  Type: AWS::SQS::Queue
  Properties:
    # ...existing properties
    SqsManagedSseEnabled: true
```

### 9.2 Access Control

- IAM roles with least privilege principle
- SQS access policies restricting queue operations to specific IAM roles
- No public access to queues

### 9.3 Data Protection

- Messages containing sensitive information should be minimal
- Personal identifiable information (PII) should be handled according to data protection policies
- Consider message content masking in logs

## 10. Implementation and Testing Strategy

### 10.1 Manual Implementation Steps

```bash
# Create WhatsApp Replies DLQ
aws sqs create-queue \
  --queue-name ai-multi-comms-whatsapp-replies-dlq-dev \
  --attributes '{"MessageRetentionPeriod":"1209600"}'

# Get the DLQ ARN
dlq_arn=$(aws sqs get-queue-attributes \
  --queue-url https://sqs.us-east-1.amazonaws.com/{account-id}/ai-multi-comms-whatsapp-replies-dlq-dev \
  --attribute-names QueueArn \
  --query 'Attributes.QueueArn' \
  --output text)

# Create WhatsApp Replies Queue with DLQ
aws sqs create-queue \
  --queue-name ai-multi-comms-whatsapp-replies-queue-dev \
  --attributes '{"DelaySeconds":"30","VisibilityTimeout":"905","MessageRetentionPeriod":"345600","RedrivePolicy":"{\"deadLetterTargetArn\":\"'$dlq_arn'\",\"maxReceiveCount\":\"3\"}"}'

# Similar commands for Human Handoff Queue and DLQ
```

### 10.2 Testing Approach

#### Unit Testing

- Test SQS service wrapper functions with mocked SQS client
- Verify correct message formatting and queue URL selection

#### Integration Testing

- Use localstack for local SQS emulation
- Test message flow from IncomingWebhookHandler to SQS to ReplyProcessorLambda
- Verify message format and content
- Test error handling and DLQ functionality

#### Performance Testing

- Simulate high message volume to validate throughput
- Verify batching behavior with multiple messages

### 10.3 Validation Checks

- Verify message delivery to correct queue based on handoff flag
- Confirm DelaySeconds behavior for message batching
- Test message visibility and processing timeout
- Validate DLQ redrive policy by forcing failures

## 11. Deployment Strategy

### 11.1 Initial Deployment

- Deploy queues via AWS CLI commands as outlined above
- Document queue URLs and ARNs for Lambda environment variables
- Configure IAM permissions for Lambda functions

### 11.2 Future SAM Template

In the future SAM template, the following resources will be defined:

```yaml
Resources:
  # WhatsApp Replies Queue & DLQ
  WhatsAppRepliesDLQ:
    Type: AWS::SQS::Queue
    Properties:
      QueueName: !Sub '${ProjectPrefix}-whatsapp-replies-dlq-${EnvironmentName}'
      MessageRetentionPeriod: 1209600
      # Additional properties as defined in this LLD

  WhatsAppRepliesQueue:
    Type: AWS::SQS::Queue
    Properties:
      QueueName: !Sub '${ProjectPrefix}-whatsapp-replies-queue-${EnvironmentName}'
      DelaySeconds: 30
      VisibilityTimeout: 905
      MessageRetentionPeriod: 345600
      RedrivePolicy:
        deadLetterTargetArn: !GetAtt WhatsAppRepliesDLQ.Arn
        maxReceiveCount: 3
      # Additional properties as defined in this LLD

  # Human Handoff Queue & DLQ
  # Similar definitions
```

## 12. Happy Path Analysis

### 12.1 WhatsApp Replies Queue

#### Preconditions
- IncomingWebhookHandler Lambda has processed a valid webhook
- Conversation found in DynamoDB with handoff_to_human = false

#### Flow
1. IncomingWebhookHandler constructs message payload
2. Message is sent to WhatsApp Replies Queue
3. Message remains invisible for 30 seconds (DelaySeconds)
4. After delay, message becomes available for processing
5. ReplyProcessorLambda is triggered and receives the message
6. Message becomes invisible for duration of VisibilityTimeout
7. ReplyProcessorLambda successfully processes message
8. Message is deleted from the queue

#### Expected Outcome
- Message is successfully processed by ReplyProcessorLambda
- Queue metrics show normal message flow
- No messages accumulate in the queue

### 12.2 Human Handoff Queue

#### Preconditions
- IncomingWebhookHandler Lambda has processed a valid webhook
- Conversation found in DynamoDB with handoff_to_human = true

#### Flow
1. IncomingWebhookHandler constructs message payload
2. Message is sent to Human Handoff Queue
3. Message is immediately available for processing (no delay)
4. Human interface system receives the message
5. Message becomes invisible for duration of VisibilityTimeout
6. Human interface system successfully processes message
7. Message is deleted from the queue

#### Expected Outcome
- Message is successfully delivered to human operators
- Queue metrics show normal message flow
- No messages accumulate in the queue

## 13. Unhappy Path Analysis

### 13.1 Processing Failure

#### Flow
1. Message is retrieved by ReplyProcessorLambda
2. Processing fails (e.g., OpenAI API error)
3. Lambda function exits without deleting the message
4. After VisibilityTimeout expires, message becomes visible again
5. Process repeats for maxReceiveCount attempts
6. After maxReceiveCount failures, message is moved to DLQ
7. CloudWatch alarm triggers notification

#### Expected Outcome
- After temporary failures, message is reprocessed
- After persistent failures, message moves to DLQ
- Operations team is notified of DLQ messages

### 13.2 Queue Service Disruption

#### Flow
1. SQS service experiences disruption
2. Messages cannot be sent or received
3. IncomingWebhookHandler Lambda fails with SQS service exception
4. CloudWatch logs show SQS errors
5. SQS service recovers
6. Normal operation resumes

#### Expected Outcome
- Temporary service disruption is logged
- Lost messages (if any) are identified through monitoring
- System recovers automatically when SQS service is restored

## 14. Next Steps

1. Create SQS queues via AWS CLI
2. Document queue URLs and ARNs
3. Configure CloudWatch metrics and alarms
4. Set up DLQ monitoring
5. Develop and test SQS integration in Lambda functions
6. Create queue management utilities for redriving DLQ messages if needed 