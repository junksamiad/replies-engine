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
- **Lambda Integration**: All routes will integrate with a single Lambda function with internal routing logic
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

## 4. Lambda Integration

### 4.1 Integration Type

- **Type**: `AWS_PROXY` (Lambda Proxy Integration)
- **Lambda Function**: One single `IncomingWebhookHandler` function for all channels
- **Content Handling**: `CONVERT_TO_TEXT` (API Gateway passes form data as-is to Lambda)

### 4.2 Request/Response Flow

1. API Gateway receives the webhook request and validates headers/format
2. The request is passed to the Lambda function with channel information
3. Lambda processes the request based on channel type and content (see `IncomingWebhookHandler` LLD for details)
4. Lambda returns an appropriate response for the channel
5. API Gateway forwards the response back to the sender

### 4.3 Execution Role

The API Gateway requires permissions to invoke the Lambda function:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "lambda:InvokeFunction",
      "Resource": "arn:aws:lambda:${region}:${account-id}:function:${function-name}"
    }
  ]
}
```

## 5. Response Handling

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

## 6. Monitoring and Logging

### CloudWatch Logging

- **Log Level**: INFO for normal operations, ERROR for failures
- **Access Logging**: Enable API Gateway access logging
- **Execution Logging**: Configure logging for all API stages

### CloudWatch Metrics

- **Standard Metrics**:
  - IntegrationLatency - Time between when API Gateway relays a request to the backend and when it receives a response
  - Latency - Time between when API Gateway receives a request from a client and when it returns a response
  - CacheHitCount/CacheMissCount - Not applicable (no caching enabled)
  - Count - Total number of API requests in a given period

### Alerting

- Set up alarms for:
  - High 4XX or 5XX error rates
  - Elevated latency
  - Throttling events
  - Quota limit approaching

## 7. Deployment Strategy

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

## 8. Testing Strategy

### API Gateway Testing

- Use API Gateway Test feature to send test requests
- Verify request validation and models
- Test throttling behavior
- Validate CORS configuration

### End-to-End Testing

- Send webhook requests from Twilio test accounts
- Verify integration with Lambda and backend systems
- Test error handling and response formatting

## 9. Next Steps

1. Implement the API Gateway with WhatsApp endpoint and security measures
2. Create request validators and models
3. Configure usage plans and throttling
4. Deploy and test with sample webhooks
5. Extend to support SMS and email endpoints 