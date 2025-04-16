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

### 4.1 Signature Validation

All webhook requests must be validated to ensure they originated from the expected service provider.

**Twilio Signature Validation Process:**
1. Lambda function extracts `X-Twilio-Signature` header from the request
2. Extract sender's phone number (`From` parameter) from the body
3. Query DynamoDB to find the conversation record associated with this number
4. If found, retrieve the credential reference (`channel_config.whatsapp.whatsapp_credentials_id`)
5. Call AWS Secrets Manager to get the Twilio auth token using this reference
6. Reconstruct the expected signature using:
   - The Twilio auth token
   - The full webhook URL
   - All POST parameters in alphabetical order
7. Compare calculated signature with the received `X-Twilio-Signature`
8. Proceed only if signatures match

**Email Validation:** (TBD based on email provider)
- Similar process using appropriate signature validation for the email provider

### 4.2 Conversation Record Processing

After signature validation, the Lambda will process the message based on conversation status.

**For Existing Conversations:**
1. Update the conversation record with the new message
2. Process the message according to business logic
3. Trigger appropriate response workflows

**For Unknown Numbers/Emails (Fallback Handling):**
1. Implement rate limiting (max 1 response per number per 24 hours)
2. Send a templated response informing the sender that the conversation is no longer active
3. Provide alternative contact methods (website, phone)
4. Log the occurrence for monitoring

**Fallback Implementation:**
```python
def handle_unknown_sender(channel_type, sender_id):
    # Check if we've already sent a fallback to this sender recently
    if has_received_fallback_recently(sender_id):
        log.info(f"Ignoring repeat unknown sender: {sender_id}")
        return
        
    # Send appropriate channel-specific fallback
    if channel_type == "whatsapp":
        send_whatsapp_fallback_template(sender_id)
    elif channel_type == "sms":
        send_sms_fallback(sender_id)
    elif channel_type == "email":
        send_email_fallback(sender_id)
        
    # Record fallback timestamp with TTL
    record_fallback_sent(sender_id)
```

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

- Test signature validation logic in isolation
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