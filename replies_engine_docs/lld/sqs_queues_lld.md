# SQS Queues - Low-Level Design

## 1. Purpose and Responsibilities

The SQS queues in the replies-engine microservice serve as message brokers between different processing stages. They provide:

- Decoupling of the initial webhook reception (`StagingLambda`) from delayed batch processing (`MessagingLambda`).
- Buffering of trigger messages and processed payloads.
- Delayed processing initiation via a dedicated Trigger Delay Queue to enable message batching.
- Dead-letter handling for fault tolerance on trigger and channel-specific queues.
- Separate paths for human handoff vs. automated/AI processing.

This LLD outlines the configurations for the following queues:
1.  **Trigger Delay Queue:** Initiates batch processing after a delay.
2.  **Channel-Specific Queues (WhatsApp, SMS, Email):** Receive merged payloads for downstream processing (e.g., AI).
3.  **Human Handoff Queue:** Receives merged payloads flagged for manual review.

## 2. Queue Configurations

*Note: `${ProjectPrefix}` (e.g., `ai-multi-comms`) and `${EnvironmentName}` (e.g., `dev`, `prod`) will be substituted during deployment.*

### 2.1 Trigger Delay Queue

This queue receives simple trigger messages from the `StagingLambda` and invokes the `MessagingLambda` after a defined delay.

```yaml
TriggerDelayQueue:
  Type: AWS::SQS::Queue
  Properties:
    QueueName: !Sub '${ProjectPrefix}-trigger-delay-queue-${EnvironmentName}'
    DelaySeconds: 0 # Default delay is 0; Delay is set per-message by StagingLambda
    VisibilityTimeout: 905 # Slightly higher than MessagingLambda timeout (900s assumed)
    MessageRetentionPeriod: 345600 # 4 days (in seconds)
    RedrivePolicy:
      deadLetterTargetArn: !GetAtt TriggerDelayDLQ.Arn
      maxReceiveCount: 3 # Retry trigger 3 times before moving to DLQ
```

#### Configuration Rationale

-   **DelaySeconds (0 default):** The actual batching delay (`W`, e.g., 10s) is set dynamically via the `DelaySeconds` parameter in the `SendMessage` call made by the `StagingLambda`.
-   **VisibilityTimeout (905s):** Set higher than the `MessagingLambda` function timeout (assumed 900s) to prevent duplicate processing of the *same trigger* if the Lambda takes the maximum allowed time.
-   **MessageRetentionPeriod (4 days):** Standard retention for operational recovery.
-   **maxReceiveCount (3):** Allows multiple trigger attempts before considering it unprocessable.

### 2.2 Trigger Delay Dead-Letter Queue (DLQ)

```yaml
TriggerDelayDLQ:
  Type: AWS::SQS::Queue
  Properties:
    QueueName: !Sub '${ProjectPrefix}-trigger-delay-dlq-${EnvironmentName}'
    MessageRetentionPeriod: 1209600 # 14 days (in seconds)
```

#### Configuration Rationale

-   **MessageRetentionPeriod (14 days):** Longer retention for failed trigger messages to allow sufficient time for investigation.

### 2.3 WhatsApp Target Queue

Receives merged payloads processed by `MessagingLambda` destined for WhatsApp channel handling (e.g., AI interaction).

```yaml
WhatsAppTargetQueue:
  Type: AWS::SQS::Queue
  Properties:
    QueueName: !Sub '${ProjectPrefix}-whatsapp-target-queue-${EnvironmentName}'
    DelaySeconds: 0 # No delay needed; batching handled by TriggerDelayQueue
    VisibilityTimeout: 300 # Example: Depends on consumer processing time (e.g., AI Lambda)
    MessageRetentionPeriod: 345600 # 4 days (in seconds)
    RedrivePolicy:
      deadLetterTargetArn: !GetAtt WhatsAppTargetDLQ.Arn
      maxReceiveCount: 3
```

### 2.4 WhatsApp Target Dead-Letter Queue (DLQ)

```yaml
WhatsAppTargetDLQ:
  Type: AWS::SQS::Queue
  Properties:
    QueueName: !Sub '${ProjectPrefix}-whatsapp-target-dlq-${EnvironmentName}'
    MessageRetentionPeriod: 1209600 # 14 days (in seconds)
```

### 2.5 SMS Target Queue & DLQ

Similar configuration to WhatsApp Target Queue & DLQ, adjusted for SMS consumers.

```yaml
# Placeholder - Define SMSTargetQueue similar to WhatsAppTargetQueue
# Placeholder - Define SMSTargetDLQ similar to WhatsAppTargetDLQ
```

### 2.6 Email Target Queue & DLQ

Similar configuration to WhatsApp Target Queue & DLQ, adjusted for Email consumers.

```yaml
# Placeholder - Define EmailTargetQueue similar to WhatsAppTargetQueue
# Placeholder - Define EmailTargetDLQ similar to WhatsAppTargetDLQ
```

### 2.7 Human Handoff Queue

Receives merged payloads processed by `MessagingLambda` flagged for human intervention.

```yaml
HumanHandoffQueue:
  Type: AWS::SQS::Queue
  Properties:
    QueueName: !Sub '${ProjectPrefix}-human-handoff-queue-${EnvironmentName}'
    DelaySeconds: 0 # No delay for human intervention
    VisibilityTimeout: 3600 # Example: 1 hour visibility for manual action
    MessageRetentionPeriod: 604800 # 7 days (in seconds) - Longer retention suitable for manual queues
    # No RedrivePolicy - This queue acts as its own holding area for manual action.
```

#### Configuration Rationale

-   **DelaySeconds (0):** Ensure immediate availability for human operators.
-   **VisibilityTimeout (e.g., 3600s):** Longer timeout appropriate for potential manual review and action window.
-   **MessageRetentionPeriod (e.g., 7 days):** Longer retention suitable for items awaiting manual action.
-   **No RedrivePolicy:** Messages remain here until explicitly processed and deleted by the human interface/operator. If processing fails persistently at the *human* step, it's an operational issue, not a message routing issue suitable for a DLQ. Monitoring queue depth is crucial.

## 3. Message Format and Schema

### 3.1 Message Schema for Trigger Delay Queue

A simple JSON object containing only the `conversation_id`.

```json
{
  "conversation_id": "abc123def456"
}
```

### 3.2 Message Schema for Target Queues (WhatsApp, SMS, Email, Human Handoff)

A JSON object representing the *merged* payload produced by the `MessagingLambda`. The exact structure depends on downstream needs, but a likely format is:

```json
{
  "conversation_id": "abc123def456",
  "merged_body": "User message part 1.\nUser message part 2.",
  "channel_type": "whatsapp", // or sms, email
  "sender_id": "whatsapp:+1234567890", // e.g., from first message context
  "recipient_id": "whatsapp:+0987654321", // e.g., from first message context
  "first_message_received_at": "2023-07-21T14:30:45.123Z", // Timestamp of first msg in batch
  "last_message_received_at": "2023-07-21T14:30:52.456Z", // Timestamp of last msg in batch
  "message_sids": ["SM...", "SM..."], // List of original SIDs in the batch
  "handoff_reason": null, // or "user_requested", "auto_queued", etc. for Handoff Queue
  "full_conversation_context": {
      // Relevant fields retrieved from ConversationsTable by StagingLambda
      // and included in the context_object stored in conversations-stage
      "project_id": "project_456",
      "company_id": "company_123",
      "thread_id": "thread_abc123def456",
      "customer_name": "...",
      "current_step": "...",
      // etc.
  }
}
```

## 4. Queue Access Patterns

### 4.1 Send Operations

| Component | Operation | Queue | Notes |
|-----------|-----------|-------|-------|
| `StagingLambda` | SendMessage | Trigger Delay Queue | Sends trigger message (`{ "conversation_id": "..." }`) with dynamic `DelaySeconds=W` if lock acquired. |
| `MessagingLambda` | SendMessage | WhatsApp/SMS/Email Target Queue OR Human Handoff Queue | Sends merged payload after successful batch processing. |
| Manual Reprocessing Tool (future) | SendMessage | Trigger Delay Queue (for failed triggers) or Target Queues (for failed processing) | For retrying failed messages/batches. |

### 4.2 Receive Operations

| Component | Operation | Queue | Notes |
|-----------|-----------|-------|-------|
| `MessagingLambda` | ReceiveMessage | Trigger Delay Queue | Receives single trigger message to initiate batch processing. |
| AI Processor Lambda (future) | ReceiveMessage | WhatsApp/SMS/Email Target Queues | Consumes merged payloads for AI interaction. |
| Human Interface (future) | ReceiveMessage | Human Handoff Queue | Consumes merged payloads requiring manual action. |

## 5. Message Lifecycle

### 5.1 Trigger Delay Queue Lifecycle

1.  **Creation**: Simple trigger message created by `StagingLambda`, sent with `DelaySeconds=W`.
2.  **Delay Period**: Message becomes invisible for `W` seconds.
3.  **Available**: Message becomes available for processing.
4.  **In Flight**: Message is retrieved by `MessagingLambda` and becomes invisible for `VisibilityTimeout`.
5.  **Processing Outcomes**:
    *   **Success**: `MessagingLambda` completes successfully, message is deleted from the queue.
    *   **Failure (MessagingLambda crash/timeout)**: Message returns to the queue after `VisibilityTimeout` expires.
    *   **Failure (Lock Contention in MessagingLambda)**: `MessagingLambda` exits successfully (returns success to SQS), message is deleted (preventing trigger retry for *this specific* SQS message delivery).
    *   **Repeated Failure (Non-Contention)**: After `maxReceiveCount` failures, message moves to `TriggerDelayDLQ`.

### 5.2 Target Queue Lifecycle (WhatsApp, SMS, Email)

1.  **Creation**: Merged payload message created by `MessagingLambda`.
2.  **Available**: Message immediately available for processing (no delay).
3.  **In Flight**: Message is retrieved by consumer (e.g., AI Lambda) and becomes invisible for `VisibilityTimeout`.
4.  **Processing Outcomes**:
    *   **Success**: Message is deleted from the queue after successful processing by consumer.
    *   **Failure**: Message returns to the queue after `VisibilityTimeout` expires.
    *   **Repeated Failure**: After `maxReceiveCount` failures, message moves to the respective Target DLQ.

### 5.3 Human Handoff Queue Lifecycle

1.  **Creation**: Merged payload message created by `MessagingLambda`.
2.  **Available**: Message immediately available for processing (no delay).
3.  **In Flight**: Message is retrieved by Human Interface System and becomes invisible for `VisibilityTimeout`.
4.  **Processing Outcomes**:
    *   **Success**: Message is deleted from the queue after successful processing/action by human/system.
    *   **Failure**: Message returns to the queue after `VisibilityTimeout` expires. It remains in the queue until successfully processed or it expires after `MessageRetentionPeriod`. Monitoring queue depth is essential.

## 6. Error Handling & Dead Letter Queues

### 6.1 Dead Letter Queue Strategy

-   Messages are sent to the appropriate DLQ (Trigger or Target) after 3 failed processing attempts.
-   DLQs retain messages for 14 days.
-   The Human Handoff queue does not have a DLQ; monitoring its depth (`ApproximateNumberOfMessagesVisible`) is key.
-   DLQ monitoring with CloudWatch alarms will trigger notifications.

### 6.2 DLQ Handling Process

1.  Operations team receives alert about messages in a DLQ.
2.  Messages are examined to determine the cause of failure.
3.  After issue resolution, messages can be manually moved back to the primary queue (Trigger or Target) for reprocessing.
4.  Metrics are collected for failure analysis and system improvement.

## 7. Performance Considerations

### 7.1 Throughput

-   Default SQS throughput (unlimited messages per second) is sufficient for expected load.

### 7.2 Latency

-   Standard queues (non-FIFO) used as exact ordering across different conversations is not critical (within-conversation order is handled by `MessagingLambda` sorting).
-   The Trigger Delay Queue introduces an intentional latency of `W` seconds (e.g., 10s) for batching.
-   Expected end-to-end latency for happy path to *target queue*: `W` seconds + `MessagingLambda` processing time (~10-15 seconds typical). Latency to final reply depends on downstream consumers.

### 7.3 Batch Processing

-   Batching of *incoming* messages is achieved via the Trigger Delay mechanism.
-   The `MessagingLambda` is triggered by a *single* SQS message but processes a *batch* of items queried from the `conversations-stage` DynamoDB table.
-   Consumers of the *Target* Queues (WhatsApp, SMS, Email) *can* use SQS batching features (`ReceiveMessage` with `MaxNumberOfMessages` > 1) if beneficial for their processing logic.

## 8. Monitoring & Alerting

### 8.1 Key Metrics to Monitor

*(Monitor these for Trigger Queue, each Target Queue, and Human Handoff Queue)*

| Metric                            | Description                               | Threshold Example                      | Alert Example |
| :-------------------------------- | :---------------------------------------- | :------------------------------------- | :------------ |
| ApproximateNumberOfMessagesVisible | Messages available for retrieval          | > 100 for > 5 minutes                  | Warning       |
| ApproximateNumberOfMessagesNotVisible | Messages in flight (being processed)    | > 50 for > 10 minutes                  | Warning       |
| ApproximateAgeOfOldestMessage     | Age of the oldest message in the queue  | > 1 hour (Target/Handoff), >10m (Trigger) | Warning       |
| ApproximateNumberOfMessagesVisible (DLQ) | Messages in any DLQ                    | > 0                                    | Critical      |
| ApproximateNumberOfMessagesVisible (Handoff) | Messages in Human Handoff Queue     | > 50 (adjust based on team capacity) | Warning       |

### 8.2 CloudWatch Alarms

Define CloudWatch Alarms for DLQNotEmpty conditions (for Trigger and Target DLQs) and high queue depth/age for the Human Handoff Queue. Example for Trigger DLQ:

```yaml
TriggerDelayDLQNotEmptyAlarm:
  Type: AWS::CloudWatch::Alarm
  Properties:
    AlarmName: !Sub '${ProjectPrefix}-TriggerDelayDLQ-NotEmpty-${EnvironmentName}'
    AlarmDescription: 'Alarm when any messages appear in the Trigger Delay Dead Letter Queue'
    Namespace: 'AWS/SQS'
    MetricName: 'ApproximateNumberOfMessagesVisible'
    Dimensions:
      - Name: QueueName
        Value: !GetAtt TriggerDelayDLQ.QueueName
    Statistic: Sum
    Period: 60
    EvaluationPeriods: 1
    Threshold: 0
    ComparisonOperator: GreaterThan
    TreatMissingData: notBreaching
    AlarmActions:
      - !Ref AlertsNotificationTopic # Assumes an SNS topic parameter/resource
```

*(Define similar alarms for Target DLQs and the Human Handoff queue depth/age)*

## 9. Security Considerations

*(Content from original section 9 generally remains valid - apply SSE, least privilege IAM, data protection policies)*

```yaml
# Example SSE enablement
TriggerDelayQueue:
  Type: AWS::SQS::Queue
  Properties:
    # ...existing properties
    SqsManagedSseEnabled: true
# Apply SqsManagedSseEnabled: true to ALL queues including DLQs
```

## 10. Implementation and Testing Strategy

*(General approach remains valid, update commands/tests for new queues)*

### 10.1 Manual Implementation Steps (Example for Trigger Queue)

```bash
# Create Trigger Delay DLQ
aws sqs create-queue --queue-name ${ProjectPrefix}-trigger-delay-dlq-${EnvironmentName} --attributes '{"MessageRetentionPeriod":"1209600","SqsManagedSseEnabled":"true"}'

# Get the DLQ ARN
dlq_arn=$(aws sqs get-queue-attributes --queue-url .../${ProjectPrefix}-trigger-delay-dlq-${EnvironmentName} --attribute-names QueueArn --query 'Attributes.QueueArn' --output text)

# Create Trigger Delay Queue with DLQ
aws sqs create-queue --queue-name ${ProjectPrefix}-trigger-delay-queue-${EnvironmentName} --attributes '{"DelaySeconds":"0","VisibilityTimeout":"905","MessageRetentionPeriod":"345600","RedrivePolicy":"{\"deadLetterTargetArn\":\"'$dlq_arn'\",\"maxReceiveCount\":\"3\"}","SqsManagedSseEnabled":"true"}'

# Similar commands for Target Queues/DLQs and Handoff Queue
```

### 10.2 Testing Approach

#### Unit Testing

-   Test SQS service wrapper functions in `StagingLambda` and `MessagingLambda` with mocked SQS client.
-   Verify correct trigger message format, dynamic delay setting, merged payload format, and target queue URL selection.

#### Integration Testing

-   Use localstack for local SQS/DynamoDB/Lambda emulation.
-   Test the full flow: `StagingLambda` -> Trigger Queue (with delay) -> `MessagingLambda` -> Target/Handoff Queue.
-   Verify message content at each stage.
-   Test error handling (lock contention, processing errors) and DLQ/Handoff queue behavior.

## 11. Deployment Strategy

### 11.1 Future SAM Template Snippet

```yaml
Resources:
  # Trigger Queue & DLQ
  TriggerDelayDLQ:
    Type: AWS::SQS::Queue
    Properties:
      QueueName: !Sub '${ProjectPrefix}-trigger-delay-dlq-${EnvironmentName}'
      MessageRetentionPeriod: 1209600
      SqsManagedSseEnabled: true
  TriggerDelayQueue:
    Type: AWS::SQS::Queue
    Properties:
      QueueName: !Sub '${ProjectPrefix}-trigger-delay-queue-${EnvironmentName}'
      VisibilityTimeout: 905 # Match MessagingLambda timeout + buffer
      MessageRetentionPeriod: 345600
      RedrivePolicy:
        deadLetterTargetArn: !GetAtt TriggerDelayDLQ.Arn
        maxReceiveCount: 3
      SqsManagedSseEnabled: true

  # WhatsApp Target Queue & DLQ
  WhatsAppTargetDLQ:
    Type: AWS::SQS::Queue
    Properties:
      QueueName: !Sub '${ProjectPrefix}-whatsapp-target-dlq-${EnvironmentName}'
      MessageRetentionPeriod: 1209600
      SqsManagedSseEnabled: true
  WhatsAppTargetQueue:
    Type: AWS::SQS::Queue
    Properties:
      QueueName: !Sub '${ProjectPrefix}-whatsapp-target-queue-${EnvironmentName}'
      VisibilityTimeout: 300 # Example - Adjust for consumer
      MessageRetentionPeriod: 345600
      RedrivePolicy:
        deadLetterTargetArn: !GetAtt WhatsAppTargetDLQ.Arn
        maxReceiveCount: 3
      SqsManagedSseEnabled: true

  # SMS Target Queue & DLQ (Define similarly)
  # Email Target Queue & DLQ (Define similarly)

  # Human Handoff Queue
  HumanHandoffQueue:
    Type: AWS::SQS::Queue
    Properties:
      QueueName: !Sub '${ProjectPrefix}-human-handoff-queue-${EnvironmentName}'
      VisibilityTimeout: 3600 # Example - Adjust for manual processing window
      MessageRetentionPeriod: 604800 # Longer retention (7 days)
      SqsManagedSseEnabled: true
      # No RedrivePolicy

```

## 12. Happy Path Analysis

### 12.1 Trigger -> Target Queue Path

#### Preconditions
-   `StagingLambda` processes a valid webhook.
-   Conversation found, rules valid, routing determines a channel-specific target (e.g., WhatsApp).
-   Trigger lock acquired, trigger message sent to `TriggerDelayQueue` with `DelaySeconds=W`.

#### Flow
1.  Trigger message waits in `TriggerDelayQueue` for `W` seconds.
2.  `MessagingLambda` is triggered by the message.
3.  `MessagingLambda` acquires processing lock, queries `conversations-stage`, merges messages.
4.  `MessagingLambda` sends merged payload to `WhatsAppTargetQueue`.
5.  `MessagingLambda` cleans up stage/lock tables, releases processing lock, returns success.
6.  Trigger message is deleted from `TriggerDelayQueue`.
7.  Downstream consumer (e.g., AI Lambda) retrieves merged payload from `WhatsAppTargetQueue`, processes it, and deletes the message.

#### Expected Outcome
-   Merged payload is successfully processed by the downstream consumer.
-   Queue metrics show normal flow through Trigger and Target queues.

### 12.2 Trigger -> Human Handoff Path

#### Preconditions
-   `StagingLambda` processes a valid webhook.
-   Conversation found, rules valid, routing determines `HumanHandoffQueue`.
-   Trigger lock acquired, trigger message sent to `TriggerDelayQueue` with `DelaySeconds=W`.

#### Flow
1.  Trigger message waits in `TriggerDelayQueue` for `W` seconds.
2.  `MessagingLambda` is triggered.
3.  `MessagingLambda` acquires lock, queries stage, merges messages.
4.  `MessagingLambda` sends merged payload to `HumanHandoffQueue`.
5.  `MessagingLambda` cleans up, releases lock, returns success.
6.  Trigger message deleted from `TriggerDelayQueue`.
7.  Human Interface system retrieves merged payload from `HumanHandoffQueue`, processes/displays it, and deletes the message.

#### Expected Outcome
-   Merged payload delivered to human operators/interface.
-   Metrics show flow through Trigger Queue and messages accumulating/being processed in Handoff Queue.

## 13. Unhappy Path Analysis

### 13.1 Trigger Processing Failure (`MessagingLambda`)

#### Flow
1.  `MessagingLambda` retrieves trigger message.
2.  Processing fails (e.g., error querying DynamoDB, internal bug) before completion.
3.  Lambda function exits/crashes *without* returning success to SQS.
4.  After `VisibilityTimeout` expires, trigger message becomes visible again in `TriggerDelayQueue`.
5.  Process repeats for `maxReceiveCount` attempts.
6.  After `maxReceiveCount` failures, trigger message moves to `TriggerDelayDLQ`.
7.  CloudWatch alarm triggers notification.

#### Expected Outcome
-   After temporary failures, trigger is reprocessed.
-   After persistent failures, trigger message moves to `TriggerDelayDLQ`. Operations team notified.

### 13.2 Target Queue Consumer Failure

#### Flow
1.  Consumer Lambda/System retrieves merged payload from a Target Queue (e.g., WhatsApp).
2.  Processing fails. Lambda exits without deleting message.
3.  After `VisibilityTimeout`, message becomes visible again in Target Queue.
4.  Repeats `maxReceiveCount` times.
5.  Message moves to the respective Target DLQ (e.g., `WhatsAppTargetDLQ`).
6.  CloudWatch alarm triggers.

#### Expected Outcome
-   Persistent consumer failures result in messages in the Target DLQ. Operations team notified.

### 13.3 Human Handoff Processing Issue

#### Flow
1.  Human Interface retrieves message from `HumanHandoffQueue`.
2.  Operator takes action, but system fails to delete message from SQS before `VisibilityTimeout`.
3.  Message reappears in `HumanHandoffQueue`.

#### Expected Outcome
-   Message might be presented to operators multiple times if not deleted correctly. Requires robust handling in the Human Interface system and monitoring of `ApproximateAgeOfOldestMessage` and `ApproximateNumberOfMessagesVisible` for the Handoff Queue. Messages remain until deleted or retention period expires.

## 14. Next Steps

1.  Define final queue names and parameters.
2.  Create/Update SQS queues via AWS CLI or SAM template.
3.  Document queue URLs/ARNs for Lambda environment variables/parameters.
4.  Configure CloudWatch metrics and alarms for all queues/DLQs.
5.  Develop/update and test SQS integration in `StagingLambda` and `MessagingLambda`.
6.  Develop consumers for Target Queues and Human Handoff Queue.
7.  Create queue management utilities (e.g., for DLQ redrive) if needed. 