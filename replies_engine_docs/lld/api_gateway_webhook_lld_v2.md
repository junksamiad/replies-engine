# API Gateway Webhook Endpoint - Low-Level Design (v2)

## 1. Purpose and Responsibilities

The API Gateway webhook endpoint serves as the entry point for all incoming communication replies in the replies-engine microservice. Its primary responsibilities include:

- Receiving HTTP POST requests from various communication channels (Twilio for WhatsApp/SMS, email providers)
- Providing a secure, reliable endpoint that validates the authenticity of incoming webhooks
- Routing validated requests to the appropriate Lambda function for processing
- Returning appropriate responses to acknowledge receipt
- Supporting multiple communication channels (WhatsApp, SMS, email) through separate routes

## 2. API Structure and Routing

### Resources and Methods

The API Gateway will be structured with the following resources and methods:

```
/
├── /whatsapp
│   ├── POST - Receives WhatsApp replies from Twilio
│   └── OPTIONS - Supports CORS
├── /sms
│   ├── POST - Receives SMS replies from Twilio
│   └── OPTIONS - Supports CORS
└── /email
    ├── POST - Receives email replies
    └── OPTIONS - Supports CORS
```

Each endpoint will have its own specific request validation model and integration with the appropriate Lambda function.

### Channel-Specific Routing

- **Path Parameters**: The channel type is determined by the URL path (/whatsapp, /sms, /email)
- **Lambda Integration**: Each channel may be integrated with its own Lambda function or a single function with internal routing
- **Initial Release**: Only the WhatsApp endpoint will be fully implemented, with placeholder resources for SMS and email

## 3. Security Implementation

### 3.1 Resource Policy

A resource policy will be attached to the API Gateway to perform basic filtering of requests before they reach any Lambda function.

**Implementation Details:**
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": "*",
      "Action": "execute-api:Invoke",
      "Resource": "execute-api:/*",
      "Condition": {
        "StringEquals": {
          "aws:HeaderExists": "X-Twilio-Signature"
        }
      }
    },
    {
      "Effect": "Deny",
      "Principal": "*",
      "Action": "execute-api:Invoke",
      "Resource": "execute-api:/*",
      "Condition": {
        "Bool": {
          "aws:HeaderExists": "false"  
        }
      }
    }
  ]
}
```

**Purpose:**
- Immediately reject requests without the required `X-Twilio-Signature` header
- Provide first-line defense against non-Twilio traffic
- Reduce Lambda invocations for obviously invalid requests

### 3.2 Request Validation

API Gateway request validators will be configured to ensure incoming webhooks match the expected format before reaching the Lambda function.

**WhatsApp/SMS Required Parameters:**
- Headers:
  - `X-Twilio-Signature` (string)
- Body parameters (application/x-www-form-urlencoded):
  - `From` (string) - The sender's phone number
  - `To` (string) - The recipient's phone number
  - `Body` (string) - The message content
  - `AccountSid` (string) - Twilio account identifier
  - `MessageSid` (string) - Message identifier

**Email Required Parameters:** (TBD based on email provider)
- Appropriate signature headers
- Sender and recipient information
- Message content references

**Implementation Method:**
- Define JSON schema for each channel's expected request format
- Configure validators in API Gateway for each endpoint
- Requests not meeting the schema will be rejected with 400 Bad Request

### 3.3 Throttling and Quotas

Usage plans will be implemented to protect against denial-of-service attacks and excessive usage.

**Configuration Details:**
- **Rate Limit**: 10 requests per second (steady state)
- **Burst Limit**: 20 requests (to handle legitimate traffic spikes)
- **Quota**: 50,000 requests per month (adjustable based on expected volume)

**Implementation:**
- Create a usage plan in API Gateway and apply to all stages
- Configure CloudWatch alarms to alert on high throttling events
- Review and adjust limits based on actual usage patterns

## 4. Webhook Processing Flow

### 4.1 Conversation Record Processing

The `IncomingWebhookHandler` Lambda function performs initial processing for all incoming webhooks regardless of channel type, following a standardized flow:

**Conversation Lookup and Update Flow:**
1. Extract sender's contact information from the webhook payload based on channel type:
   - WhatsApp/SMS: `From` parameter from Twilio payload
   - Email: Sender address from email headers
2. Query DynamoDB to find the corresponding conversation record using the sender identifier
3. If record exists:
   - Update the conversation record with the incoming message:
     ```python
     # Simplified pseudo-code
     update_expression = "SET messages = list_append(messages, :new_message), 
                          conversation_status = :status,
                          last_user_message_at = :timestamp,
                          last_activity_at = :timestamp"
     
     expression_values = {
         ":new_message": [{
             "message_id": webhook_data.message_id,
             "direction": "INBOUND",
             "content": webhook_data.body,
             "timestamp": current_timestamp,
             "channel_type": channel_type,
             "metadata": {...}  # Channel-specific metadata
         }],
         ":status": "user_reply_received",
         ":timestamp": current_timestamp
     }
     ```
   - This update is performed as an atomic operation to ensure the message is recorded even if subsequent processing fails
4. If record does not exist:
   - Log the unknown sender attempt
   - Implement rate-limited fallback messaging (max 1 response per sender per 24h)
   - Return early without further processing

**Context Object Creation:**
1. After the record update, retrieve the complete conversation record with all associated data
2. Construct a comprehensive context object containing:
   - Message details from the webhook
   - Conversation data (ID, status, thread_id, etc.)
   - Company and project information from the record
   - Channel-specific configuration (credentials reference, templates, etc.)
   - Processing metadata (timestamps, validation status, request IDs)

**Channel and Queue Routing:**
1. Determine the appropriate processing path based on multiple factors:
   - Check `handoff_to_human` flag in the conversation record
   - Examine `channel_method` to identify channel-specific processing needs (whatsapp, sms, email)
   - Verify conversation status is valid for further processing
2. Based on these factors, route to the appropriate SQS queue:
   ```python
   if handoff_to_human:
       # Route to human agent queue
       sqs.send_message(
           QueueUrl=f"ai-multi-comms-{channel_method}-handoff-queue-{stage}",
           MessageBody=json.dumps(context_object)
       )
   else:
       # Route to AI processing queue
       sqs.send_message(
           QueueUrl=f"ai-multi-comms-{channel_method}-replies-queue-{stage}",
           MessageBody=json.dumps(context_object),
           DelaySeconds=30  # Allow message batching
       )
   ```

**Single Lambda for Initial Processing:**
- The `IncomingWebhookHandler` serves as a unified entry point for all channels
- Channel-specific logic is isolated within well-defined sections of code
- Configuration is externalized in environment variables and DynamoDB records
- A factory pattern is used to select the appropriate message parser based on channel type
- This approach reduces duplication while maintaining channel-specific handling when needed

**Post-Queue Processing:**
- After the SQS queue, channel-specific Lambdas handle the actual processing:
  - `WhatsappReplyProcessorLambda`
  - `SmsReplyProcessorLambda`
  - `EmailReplyProcessorLambda`
- This architecture mirrors the template-sender-engine design pattern
- Each processor Lambda is optimized for channel-specific requirements while sharing common core logic through shared libraries

### 4.2 Message Routing and Context Object

A standardized context object will be created to track the message through the processing pipeline:

```json
{
  "meta": {
    "request_id": "uuid-here",
    "channel_type": "whatsapp",
    "timestamp": "2023-06-01T12:34:56.789Z",
    "version": "1.0"
  },
  "conversation": {
    "conversation_id": "conversation-uuid",
    "primary_channel": "+1234567890",
    "conversation_status": "active",
    "hand_off_to_human": false
  },
  "message": {
    "from": "+1234567890",
    "to": "+0987654321",
    "body": "Hello there",
    "message_id": "twilio-message-id",
    "timestamp": "2023-06-01T12:34:50.123Z"
  },
  "company": {
    "company_id": "company-123",
    "project_id": "project-456",
    "company_name": "Cucumber Recruitment",
    "credentials_reference": "whatsapp-credentials/cucumber-recruitment/cv-analysis/twilio"
  },
  "processing": {
    "validation_status": "valid",
    "ai_response": null,
    "sent_response": null,
    "processing_timestamps": {
      "received": "2023-06-01T12:34:56.789Z",
      "validated": "2023-06-01T12:34:57.123Z",
      "queued": "2023-06-01T12:34:57.456Z"
    }
  }
}
```

This context object will be placed on the appropriate SQS queue for further processing.

## 5. Integration with Backend Systems

### 5.1 Lambda Integration

- **Integration Type**: `AWS_PROXY` (Lambda Proxy Integration)
- **Lambda Function**: `IncomingWebhookHandler`
- **Content Handling**: `CONVERT_TO_TEXT` (API Gateway passes form data as-is to Lambda)

### 5.2 Database Integration

- **Primary Table**: Messages will be stored in a DynamoDB table
- **Schema Design**: 
  - Partition Key: `primary_channel` (phone number or email)
  - Sort Key: `conversation_id`
  - GSI: `conversation_status` for efficient querying
- **Access Pattern**: Query by sender identifier to retrieve conversation context

### 5.3 Secrets Manager Integration

- **Access Method**: Lambda retrieves credentials based on stored reference
- **Credential Reference Format**: `{channel}-credentials/{company_name}/{project_name}/{provider}`
- **Example**: `whatsapp-credentials/cucumber-recruitment/cv-analysis/twilio`

### 5.4 SQS Integration

- **Queues**:
  - `WhatsAppRepliesQueue`: For messages to be processed by AI
  - `HandoffQueue`: For messages requiring human intervention
- **Configuration**:
  - Standard queues with message delay (30 seconds)
  - Visibility timeout: 2 minutes
  - Dead-letter queue for failed processing
  - Retention period: 14 days

## 6. Response Handling

### WhatsApp/SMS Response

A successful response to Twilio should be a minimal TwiML response:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<Response></Response>
```

With HTTP headers:
```
Content-Type: text/xml
Status: 200 OK
```

### Email Response (TBD)

Appropriate response format based on email provider requirements.

## 7. Monitoring and Logging

### CloudWatch Logging

- **Log Level**: INFO for normal operations, ERROR for failures
- **Structured Logging**: JSON format for easy querying and analysis
- **Sensitive Data**: Mask sensitive information in logs

### CloudWatch Metrics

- **Custom Metrics**:
  - `WebhookValidationFailure` - Count of signature validation failures
  - `UnknownSenderCount` - Count of messages from unknown senders
  - `FallbackMessageSent` - Count of fallback messages sent
  - `ProcessingTime` - Time taken to process each webhook

### Alerting

- Set up alarms for:
  - High rate of validation failures (potential attack)
  - Excessive unknown sender messages
  - Lambda errors or timeouts
  - High API throttling events

## 8. Deployment Strategy

### CloudFormation/SAM Template

The API Gateway and associated resources will be defined in infrastructure-as-code:

```yaml
# Simplified example
Resources:
  ApiGateway:
    Type: AWS::ApiGateway::RestApi
    Properties:
      Name: replies-engine-webhook-api
      Description: "Webhook endpoints for replies-engine"
  
  UsagePlan:
    Type: AWS::ApiGateway::UsagePlan
    Properties:
      ApiStages:
        - ApiId: !Ref ApiGateway
          Stage: !Ref ApiStage
      Throttle:
        RateLimit: 10
        BurstLimit: 20
      Quota:
        Limit: 50000
        Period: MONTH

  WhatsAppResource:
    Type: AWS::ApiGateway::Resource
    Properties:
      RestApiId: !Ref ApiGateway
      ParentId: !GetAtt ApiGateway.RootResourceId
      PathPart: "whatsapp"
      
  # Additional resources for SMS and Email routes
  # Request validators, models, methods, etc.
```

### Multi-Environment Support

- Use CloudFormation parameters to customize for different environments
- Incorporate environment-specific naming conventions
- Deploy separate stacks for dev, staging, and production

## 9. Testing Strategy

### Unit Testing

- Test message parsing logic in isolation
- Test DynamoDB interaction patterns
- Mock Secrets Manager for credential retrieval testing

### Integration Testing

- Use API Gateway Test feature to send test webhooks
- Validate end-to-end flow with Twilio test credentials
- Test rate limiting and throttling behavior

### Load Testing

- Simulate high volume of incoming webhooks
- Verify throttling behavior works as expected
- Measure performance under load

## 10. Next Steps

1. Implement the API Gateway with WhatsApp endpoint and security measures
2. Develop and deploy the IncomingWebhookHandler Lambda
3. Set up monitoring and alerting
4. Test with Twilio sandbox environment
5. Plan for SMS and email endpoint implementation 