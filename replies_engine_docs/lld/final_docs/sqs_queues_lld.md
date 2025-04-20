# SQS Queues - Low-Level Design

## 1. Purpose and Responsibilities

The SQS queues in the replies-engine microservice serve as message brokers between different processing stages. They provide:

-   Decoupling of the initial webhook reception (`StagingLambda`) from delayed batch processing (`MessagingLambda`).
-   Buffering of messages/payloads.
-   Delayed processing initiation for batching, handled by setting `DelaySeconds` on messages sent to channel-specific queues.
-   Dead-letter handling for fault tolerance on channel-specific queues.
-   Separate paths for human handoff vs. automated/AI processing.

This LLD outlines the configurations for the following queues:
1.  **Channel-Specific Queues (WhatsApp, SMS, Email):** Receive initial message context from `StagingLambda` with a delay (`W`), triggering `MessagingLambda` for batch processing. They have associated Dead-Letter Queues (DLQs).
2.  **Human Handoff Queue:** Receives initial message context from `StagingLambda` flagged for manual review (no delay, no DLQ).

## 2. Queue Configurations

*Note: `${ProjectPrefix}` (e.g., `ai-multi-comms`) and `${EnvironmentName}` (e.g., `dev`, `prod`) will be substituted during deployment.*

### 2.1 WhatsApp Queue (Handles Delay)

This queue receives initial message context from the `StagingLambda` for WhatsApp messages requiring batch processing. It invokes the `MessagingLambda` after the message-specific delay (`W`).

```yaml
WhatsAppQueue:
  Type: AWS::SQS::Queue
  Properties:
    QueueName: !Sub '${ProjectPrefix}-whatsapp-queue-${EnvironmentName}'
    SqsManagedSseEnabled: true
    DelaySeconds: 0 # Default queue delay is 0; Batching delay (W) is set per-message by StagingLambda
    VisibilityTimeout: 905 # Slightly higher than MessagingLambda timeout (900s assumed)
    MessageRetentionPeriod: 345600 # 4 days (in seconds)
    RedrivePolicy:
      deadLetterTargetArn: !GetAtt WhatsAppDLQ.Arn
      maxReceiveCount: 3 # Retry trigger 3 times before moving to DLQ
```

#### Configuration Rationale

-   **DelaySeconds (0 default):** The actual batching delay (`W`, e.g., 10s) is set dynamically via the `DelaySeconds` parameter in the `SendMessage` call made by the `StagingLambda`.
-   **VisibilityTimeout (905s):** Set higher than the `MessagingLambda` function timeout (assumed 900s) to prevent duplicate processing if the Lambda takes the maximum allowed time to process a batch triggered by a message from this queue.
-   **MessageRetentionPeriod (4 days):** Standard retention for operational recovery.
-   **maxReceiveCount (3):** Allows multiple processing attempts by `MessagingLambda` before considering the message (and potentially the batch it represents) unprocessable.

### 2.2 WhatsApp Dead-Letter Queue (DLQ)

```yaml
WhatsAppDLQ:
  Type: AWS::SQS::Queue
  Properties:
    QueueName: !Sub '${ProjectPrefix}-whatsapp-dlq-${EnvironmentName}'
    SqsManagedSseEnabled: true
    MessageRetentionPeriod: 1209600 # 14 days (in seconds)
```

#### Configuration Rationale

-   **MessageRetentionPeriod (14 days):** Longer retention for failed messages/triggers to allow sufficient time for investigation.

### 2.3 SMS Queue (Handles Delay) & DLQ

Similar configuration to WhatsApp Queue & DLQ, adjusted for SMS.

```yaml
# Placeholder - Define SMSQueue similar to WhatsAppQueue (triggers MessagingLambda)
# Placeholder - Define SMSDLQ similar to WhatsAppDLQ
```

### 2.4 Email Queue (Handles Delay) & DLQ

Similar configuration to WhatsApp Queue & DLQ, adjusted for Email.

```yaml
# Placeholder - Define EmailQueue similar to WhatsAppQueue (triggers MessagingLambda)
# Placeholder - Define EmailDLQ similar to WhatsAppDLQ
```

### 2.5 Human Handoff Queue

Receives initial message context from `StagingLambda` flagged for human intervention. Does not trigger `MessagingLambda`.

```yaml
HumanHandoffQueue:
  Type: AWS::SQS::Queue
  Properties:
    QueueName: !Sub '${ProjectPrefix}-human-handoff-queue-${EnvironmentName}'
    SqsManagedSseEnabled: true
    DelaySeconds: 0 # No delay for human intervention
    VisibilityTimeout: 3600 # Example: 1 hour visibility for manual action
    MessageRetentionPeriod: 604800 # 7 days (in seconds) - Longer retention suitable for manual queues
    # No RedrivePolicy - This queue acts as its own holding area for manual action.
```

#### Configuration Rationale

-   **DelaySeconds (0):** Ensure immediate availability for human operators.
-   **VisibilityTimeout (e.g., 3600s):** Longer timeout appropriate for potential manual review and action window by the consuming Human Interface system.
-   **MessageRetentionPeriod (e.g., 7 days):** Longer retention suitable for items awaiting manual action.
-   **No RedrivePolicy:** Messages remain here until explicitly processed and deleted by the human interface/operator. Monitoring queue depth is crucial.

## 3. Message Format and Schema

### 3.1 Message Schema for Channel Queues (WhatsApp, SMS, Email - Input to MessagingLambda)

The message sent by `StagingLambda` to these queues contains the initial context required to trigger batch processing. It should at least contain the `conversation_id`. Sending the full `context_object` as saved in `conversations-stage` is also an option. Let's assume the minimal `conversation_id` for simplicity, as `MessagingLambda` will query the staging table anyway.

```json
{
  "conversation_id": "abc123def456"
  // Optionally include other lightweight hints (e.g., primary_channel) but **do not** include the full context; the MessagingLambda will hydrate from DynamoDB.
}
```

### 3.2 Message Schema for Human Handoff Queue (Input to Human Interface)

This queue receives the full initial context for messages routed for manual review.

```json
{
    // This is essentially the 'context_object' stored in conversations-stage
    "conversation_id": "abc123def456",
    "channel_type": "whatsapp",
    "message_sid": "SM...",
    "received_at": "2023-07-21T14:30:52.456Z",
    "sender_id": "whatsapp:+1234567890",
    "recipient_id": "whatsapp:+0987654321",
    "body": "User message needing handoff",
    "handoff_reason": "user_requested", // or "auto_queued", etc.
    // ... potentially other fields from the original context_object ...
    "project_id": "project_456",
    "company_id": "company_123",
    "thread_id": "thread_abc123def456",
    // ... etc ...
}
```

*(Note: The schema for the *output* of `MessagingLambda` sent to further downstream queues/processes is not defined in this LLD but would resemble the merged payload described previously).*

## 4. Queue Access Patterns

### 4.1 Send Operations

| Component         | Operation   | Queue                                    | Notes                                                                 |
| :---------------- | :---------- | :--------------------------------------- | :-------------------------------------------------------------------- |
| `StagingLambda`   | SendMessage | WhatsApp/SMS/Email Queue                 | Sends trigger message (e.g., `{ "conversation_id": "..." }`) with dynamic `DelaySeconds=W`. |
| `StagingLambda`   | SendMessage | Human Handoff Queue                      | Sends full `context_object` with `DelaySeconds=0` (or omitted).         |
| `MessagingLambda` | SendMessage | *Downstream Queues/Services* (Not in LLD) | Sends *merged payload* after successful batch processing.               |
| Manual Reprocessing | SendMessage | WhatsApp/SMS/Email Queue or Handoff Queue | For retrying failed messages (needs careful consideration of state). |

### 4.2 Receive Operations

| Component                | Operation      | Queue                        | Notes                                                        |
| :----------------------- | :------------- | :--------------------------- | :----------------------------------------------------------- |
| `MessagingLambda`        | ReceiveMessage | WhatsApp/SMS/Email Queue     | Receives trigger message(s) to initiate batch processing.    |
| AI Processor (future)  | ReceiveMessage | *Downstream Queues* (Not LLD)  | Consumes merged payloads for AI interaction.                 |
| Human Interface (future) | ReceiveMessage | Human Handoff Queue          | Consumes full `context_object` requiring manual action.      |

## 5. Message Lifecycle

### 5.1 Channel Queue Lifecycle (WhatsApp, SMS, Email)

1.  **Creation**: Trigger message created by `StagingLambda`, sent with `DelaySeconds=W`.
2.  **Delay Period**: Message becomes invisible for `W` seconds.
3.  **Available**: Message becomes available for processing.
4.  **In Flight**: Message is retrieved by `MessagingLambda` and becomes invisible for `VisibilityTimeout`.
5.  **Processing Outcomes**:
    *   **Success**: `MessagingLambda` successfully processes the batch triggered by this message, message is deleted from the queue.
    *   **Failure (MessagingLambda crash/timeout)**: Message returns to the queue after `VisibilityTimeout` expires.
    *   **Failure (Lock Contention in MessagingLambda)**: `MessagingLambda` exits successfully (returns success to SQS), message is deleted (preventing trigger retry for *this specific* SQS message delivery). The lock likely prevents processing anyway.
    *   **Repeated Failure (Non-Contention)**: After `maxReceiveCount` failures, message moves to the respective channel DLQ.

### 5.2 Human Handoff Queue Lifecycle

1.  **Creation**: Full `context_object` message created by `StagingLambda` (no delay).
2.  **Available**: Message immediately available for processing.
3.  **In Flight**: Message is retrieved by Human Interface System and becomes invisible for `VisibilityTimeout`.
4.  **Processing Outcomes**:
    *   **Success**: Message is deleted from the queue after successful processing/action by human/system.
    *   **Failure**: Message returns to the queue after `VisibilityTimeout` expires. It remains in the queue until successfully processed or it expires after `MessageRetentionPeriod`. Monitoring queue depth is essential.

## 6. Error Handling & Dead Letter Queues

### 6.1 Dead Letter Queue Strategy

-   Messages failing processing repeatedly in the Channel Queues (WhatsApp, SMS, Email) are sent to their respective DLQs after `maxReceiveCount` attempts.
-   DLQs retain messages for 14 days.
-   The Human Handoff queue does not have a DLQ; monitoring its depth (`ApproximateNumberOfMessagesVisible`) and age (`ApproximateAgeOfOldestMessage`) is key.
-   DLQ monitoring with CloudWatch alarms will trigger notifications.

### 6.2 DLQ Handling Process

1.  Operations team receives alert about messages in a channel DLQ.
2.  Messages are examined (containing e.g., `{ "conversation_id": "..." }`) to determine the cause of failure (likely requires checking `MessagingLambda` logs for that `conversation_id`).
3.  After issue resolution, messages can be manually moved back to the primary Channel Queue for reprocessing.
4.  Metrics are collected for failure analysis.

## 7. Performance Considerations

### 7.1 Throughput

-   Default SQS throughput is sufficient for expected load.

### 7.2 Latency

-   Standard queues (non-FIFO) used.
-   Channel Queues introduce an intentional message delivery latency of `W` seconds (e.g., 10s) for batching initiation.
-   Expected end-to-end latency for happy path *triggering MessagingLambda*: `W` seconds. Latency to final reply depends on `MessagingLambda` processing time and downstream consumers.

### 7.3 Batch Processing

-   Batching of incoming messages is achieved by delaying the trigger message sent to the Channel Queues.
-   The `MessagingLambda` is triggered by a single message from a Channel Queue but processes a batch of items queried from the `conversations-stage` DynamoDB table.
-   `MessagingLambda` *can* be configured to receive batches of trigger messages (up to 10) from SQS if needed (set `BatchSize` > 1 on the Lambda event source mapping), but each message still triggers a separate lock attempt and batch processing cycle for its respective `conversation_id`.

## 8. Monitoring & Alerting

### 8.1 Key Metrics to Monitor

*(Monitor these for each Channel Queue, its DLQ, and the Human Handoff Queue)*

| Metric                                   | Description                               | Threshold Example                          | Alert Example |
| :--------------------------------------- | :---------------------------------------- | :----------------------------------------- | :------------ |
| ApproximateNumberOfMessagesVisible       | Messages available for retrieval          | > 100 for > 5 minutes                      | Warning       |
| ApproximateNumberOfMessagesNotVisible    | Messages in flight (being processed)    | > 50 for > 10 minutes                      | Warning       |
| ApproximateAgeOfOldestMessage            | Age of the oldest message in the queue  | > 1 hour (Channel/Handoff)                 | Warning       |
| ApproximateNumberOfMessagesVisible (DLQ) | Messages in any Channel DLQ             | > 0                                        | Critical      |
| ApproximateNumberOfMessagesVisible (Handoff) | Messages waiting in Human Handoff Queue | > 50 (adjust based on team capacity)     | Warning       |

### 8.2 CloudWatch Alarms

Define CloudWatch Alarms for DLQNotEmpty conditions (for Channel DLQs) and high queue depth/age for the Human Handoff Queue. Example for WhatsApp DLQ:

```yaml
WhatsAppDLQNotEmptyAlarm:
  Type: AWS::CloudWatch::Alarm
  Properties:
    AlarmName: !Sub '${ProjectPrefix}-WhatsAppDLQ-NotEmpty-${EnvironmentName}'
    AlarmDescription: 'Alarm when any messages appear in the WhatsApp Dead Letter Queue'
    Namespace: 'AWS/SQS'
    MetricName: 'ApproximateNumberOfMessagesVisible'
    Dimensions:
      - Name: QueueName
        Value: !GetAtt WhatsAppDLQ.QueueName
    Statistic: Sum
    Period: 60
    EvaluationPeriods: 1
    Threshold: 0
    ComparisonOperator: GreaterThan
    TreatMissingData: notBreaching
    AlarmActions:
      - !Ref AlertsNotificationTopic # Assumes an SNS topic parameter/resource
```

*(Define similar alarms for SMS/Email DLQs and the Human Handoff queue depth/age)*

## 9. Security Considerations

*(Content remains valid - apply SSE, least privilege IAM, data protection policies)*

```yaml
# Example SSE enablement
WhatsAppQueue: # Apply to ALL queues including DLQs
  Type: AWS::SQS::Queue
  Properties:
    # ...existing properties
    SqsManagedSseEnabled: true
```

## 10. Implementation and Testing Strategy

*(General approach remains valid, update commands/tests for revised queue structure)*

### 10.1 Manual Implementation Steps (Example for WhatsApp Queue)

```bash
# Create WhatsApp DLQ
aws sqs create-queue --queue-name ${ProjectPrefix}-whatsapp-dlq-${EnvironmentName} --attributes '{"MessageRetentionPeriod":"1209600","SqsManagedSseEnabled":"true"}'

# Get the DLQ ARN
dlq_arn=$(aws sqs get-queue-attributes --queue-url .../${ProjectPrefix}-whatsapp-dlq-${EnvironmentName} --attribute-names QueueArn --query 'Attributes.QueueArn' --output text)

# Create WhatsApp Queue with DLQ
aws sqs create-queue --queue-name ${ProjectPrefix}-whatsapp-queue-${EnvironmentName} --attributes '{"DelaySeconds":"0","VisibilityTimeout":"905","MessageRetentionPeriod":"345600","RedrivePolicy":"{\"deadLetterTargetArn\":\"'$dlq_arn'\",\"maxReceiveCount\":\"3\"}","SqsManagedSseEnabled":"true"}'

# Create Human Handoff Queue (No DLQ)
aws sqs create-queue --queue-name ${ProjectPrefix}-human-handoff-queue-${EnvironmentName} --attributes '{"DelaySeconds":"0","VisibilityTimeout":"3600","MessageRetentionPeriod":"604800","SqsManagedSseEnabled":"true"}'

# Similar commands for SMS/Email Queues & DLQs
```

### 10.2 Testing Approach

#### Unit Testing

-   Test SQS service wrapper functions in `StagingLambda` with mocked SQS client.
-   Verify correct trigger message format (`conversation_id`), dynamic delay setting (`W` vs `0`), and correct queue URL selection (Channel vs Handoff).
-   Test `MessagingLambda` SQS message parsing.

#### Integration Testing

-   Use localstack for local SQS/DynamoDB/Lambda emulation.
-   Test the flow: `StagingLambda` -> Channel Queue (with delay `W`) -> `MessagingLambda`.
-   Test the flow: `StagingLambda` -> Handoff Queue (no delay) -> Mock Human Interface consumer.
-   Verify message content arrives correctly at `MessagingLambda` and Mock Human Interface.
-   Test error handling (lock contention, processing errors) and DLQ/Handoff queue behavior.

## 11. Deployment Strategy

### 11.1 Future SAM Template Snippet

```yaml
Resources:
  # WhatsApp Queue & DLQ
  WhatsAppDLQ:
    Type: AWS::SQS::Queue
    Properties:
      QueueName: !Sub '${ProjectPrefix}-whatsapp-dlq-${EnvironmentName}'
      MessageRetentionPeriod: 1209600
      SqsManagedSseEnabled: true
  WhatsAppQueue:
    Type: AWS::SQS::Queue
    Properties:
      QueueName: !Sub '${ProjectPrefix}-whatsapp-queue-${EnvironmentName}'
      VisibilityTimeout: 905 # Match MessagingLambda timeout + buffer
      MessageRetentionPeriod: 345600
      RedrivePolicy:
        deadLetterTargetArn: !GetAtt WhatsAppDLQ.Arn
        maxReceiveCount: 3
      SqsManagedSseEnabled: true # DelaySeconds is 0 here, set per message

  # SMS Queue & DLQ (Define similarly)
  # Email Queue & DLQ (Define similarly)

  # Human Handoff Queue
  HumanHandoffQueue:
    Type: AWS::SQS::Queue
    Properties:
      QueueName: !Sub '${ProjectPrefix}-human-handoff-queue-${EnvironmentName}'
      VisibilityTimeout: 3600 # Example - Adjust for manual processing window
      MessageRetentionPeriod: 604800 # Longer retention (7 days)
      SqsManagedSseEnabled: true
      # No RedrivePolicy

  # --- Lambda Event Source Mappings ---
  MessagingLambdaSQSTriggerWhatsApp:
    Type: AWS::Lambda::EventSourceMapping
    Properties:
      BatchSize: 1 # Process one trigger message at a time
      Enabled: True
      EventSourceArn: !GetAtt WhatsAppQueue.Arn
      FunctionName: !GetAtt MessagingLambda.Arn # Assumes MessagingLambda resource defined elsewhere
  # Define similar EventSourceMappings for SMSQueue and EmailQueue triggering MessagingLambda

  # Human Handoff consumer trigger would be defined elsewhere (not MessagingLambda)

```

## 12. Happy Path Analysis

### 12.1 Channel Queue -> MessagingLambda Path

#### Preconditions
-   `StagingLambda` processes a valid webhook.
-   Conversation found, rules valid, routing determines a channel-specific queue (e.g., WhatsAppQueue).
-   Trigger lock acquired, trigger message sent to `WhatsAppQueue` with `DelaySeconds=W`.

#### Flow
1.  Trigger message waits in `WhatsAppQueue` for `W` seconds.
2.  Message becomes visible, triggering `MessagingLambda` (via Event Source Mapping).
3.  `MessagingLambda` receives trigger message (`{ "conversation_id": "..." }`).
4.  `MessagingLambda` acquires processing lock, queries `conversations-stage`, merges messages.
5.  `MessagingLambda` sends merged payload downstream (e.g., to AI service/queue - not defined here).
6.  `MessagingLambda` cleans up stage/lock tables, releases processing lock, returns success.
7.  Trigger message is deleted from `WhatsAppQueue`.

#### Expected Outcome
-   Merged payload is successfully processed/sent downstream by `MessagingLambda`.
-   Queue metrics show normal flow through the specific Channel Queue.

### 12.2 Human Handoff Path

#### Preconditions
-   `StagingLambda` processes a valid webhook.
-   Conversation found, rules valid, routing determines `HumanHandoffQueue`.
-   Full context message sent to `HumanHandoffQueue` with no delay.

#### Flow
1.  Message is immediately available in `HumanHandoffQueue`.
2.  Human Interface system retrieves the message.
3.  Human Interface system processes/displays it.
4.  Message is deleted from `HumanHandoffQueue` upon successful action.

#### Expected Outcome
-   Full context message delivered promptly to human operators/interface.
-   Metrics show messages accumulating/being processed in Handoff Queue.

## 13. Unhappy Path Analysis

### 13.1 Channel Queue Processing Failure (`MessagingLambda`)

#### Flow
1.  `MessagingLambda` retrieves trigger message from a Channel Queue (e.g., WhatsAppQueue).
2.  Processing fails (e.g., error querying DynamoDB, internal bug) before completion.
3.  Lambda function exits/crashes *without* returning success to SQS.
4.  After `VisibilityTimeout` expires, trigger message becomes visible again in the Channel Queue.
5.  Process repeats for `maxReceiveCount` attempts.
6.  After `maxReceiveCount` failures, trigger message moves to the respective Channel DLQ (e.g., `WhatsAppDLQ`).
7.  CloudWatch alarm triggers notification.

#### Expected Outcome
-   After temporary failures, the batch processing triggered by the message is retried.
-   After persistent failures, trigger message moves to the Channel DLQ. Operations team notified.

### 13.2 Human Handoff Processing Issue

*(Same as previous version)*

#### Flow
1.  Human Interface retrieves message from `HumanHandoffQueue`.
2.  Operator takes action, but system fails to delete message from SQS before `VisibilityTimeout`.
3.  Message reappears in `HumanHandoffQueue`.

#### Expected Outcome
-   Message might be presented to operators multiple times if not deleted correctly. Requires robust handling in the Human Interface system and monitoring of queue age/depth. Messages remain until deleted or retention period expires.

## 14. Next Steps

1.  Define final queue names and parameters (VisibilityTimeouts, Retention Periods).
2.  Create/Update SQS queues and DLQs via AWS CLI or SAM template.
3.  Document queue URLs/ARNs for Lambda environment variables/parameters.
4.  Configure CloudWatch metrics and alarms for all queues/DLQs.
5.  Update `StagingLambda` to send messages to correct queues with appropriate `DelaySeconds`.
6.  Configure `MessagingLambda` event source mapping to trigger from Channel Queues.
7.  Develop/update and test SQS integration in `StagingLambda` and `MessagingLambda`.
8.  Develop consumers for Handoff Queue and downstream consumers of `MessagingLambda`'s output.
9.  Create queue management utilities (e.g., for DLQ redrive) if needed. 