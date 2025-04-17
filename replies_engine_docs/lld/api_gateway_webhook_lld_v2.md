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

### 2.3 CORS Configuration
To support cross-origin API calls (e.g., if you ever call these endpoints from browsers or webhooks simulators):

- **Allowed Origins**: `*` (or restrict to specific domains as needed)
- **Allowed Methods**: `POST`, `OPTIONS`
- **Allowed Headers**: `Content-Type`, `X-Twilio-Signature`, `Accept`, `User-Agent`
- **Exposed Headers**: None (unless downstream needs to read headers like `X-Request-ID`)
- **Allow Credentials**: `false` (not needed for webhooks)
- **Max Age**: `3600` seconds (preflight cache duration)

For each resource method:
1.  Enable CORS in API Gateway method settings (OPTIONS method returning the above headers).
2.  Configure Method Response to include the CORS headers in `Access-Control-Allow-*`.
3.  Map Integration Response to pass through the CORS headers on `OPTIONS` and on `POST` success.

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

### 3.2.1 Example JSON Schema Model
Below is an example of an `AWS::ApiGateway::Model` JSON schema for validating a WhatsApp/SMS webhook payload (for `application/x-www-form-urlencoded` after mapping to JSON):
```json
{
  "$schema": "http://json-schema.org/draft-04/schema#",
  "title": "WhatsApp/SMS Webhook Request",
  "type": "object",
  "properties": {
    "From":      { "type": "string" },
    "To":        { "type": "string" },
    "Body":      { "type": "string" },
    "AccountSid":{ "type": "string" },
    "MessageSid":{ "type": "string" }
  },
  "required": ["From", "To", "Body", "AccountSid", "MessageSid"]
}
```
This model is then referenced by a **Request Validator** on the `/whatsapp` and `/sms` POST methods to enforce that only well-formed requests reach the Lambda.

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

### 5.1 Success Responses

#### WhatsApp / SMS
A successful Twilio callback should return a minimal empty TwiML envelope:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<Response></Response>
```
With HTTP status `200 OK` and header:
```
Content-Type: text/xml
```

#### Email
A successful inbound email POST (future) can return a `200 OK` with an empty JSON body or confirmation JSON:
```json
{
  "status": "received"
}
```
With header:
```
Content-Type: application/json
```

### 5.2 Error Response Modeling

Unlike typical REST endpoints, Twilio webhooks expect a `200` status even on application errors, otherwise Twilio will treat it as a delivery failure. To handle errors gracefully:

#### 5.2.1 Twilio (WhatsApp/SMS) Error Mapping

- **All** `4XX` and `5XX` integration errors should be remapped to a `200` status with an empty TwiML response. This ensures Twilio will not retry or mark the webhook as failed.

API Gateway Method Integration Responses example (YAML snippet):
```yaml
  - StatusCode: 200
    SelectionPattern: '4\d{2}'       # catch any 4xx from Lambda
    ResponseParameters:
      method.response.header.Content-Type: "'text/xml'"
    ResponseTemplates:
      text/xml: "<?xml version='1.0' encoding='UTF-8'?><Response></Response>"

  - StatusCode: 200
    SelectionPattern: '5\d{2}'       # catch any 5xx from Lambda
    ResponseParameters:
      method.response.header.Content-Type: "'text/xml'"
    ResponseTemplates:
      text/xml: "<?xml version='1.0' encoding='UTF-8'?><Response></Response>"
```
Be sure the Method Response lists status `200` under `Responses` and exposes `Content-Type`.

#### 5.2.2 Email Error Mapping (Future)

For email endpoints, preserve standard HTTP error codes. For example:
- `400 Bad Request` when validation fails
- `500 Internal Server Error` for unexpected failures

Use Integration Responses without override of status code:
```yaml
  - StatusCode: 400
    SelectionPattern: '4\d{2}'
    ResponseParameters:
      method.response.header.Content-Type: "'application/json'"
    ResponseTemplates:
      application/json: |
        { "error": "Invalid email payload", "details": "$context.error.messageString" }

  - StatusCode: 500
    SelectionPattern: '5\d{2}'
    ResponseParameters:
      method.response.header.Content-Type: "'application/json'"
    ResponseTemplates:
      application/json: |
        { "error": "Internal server error" }
```

### 5.3 Gateway Responses for Validation Errors

API Gateway can automatically return structured responses for request‐validation failures (missing headers, model mismatches):
```yaml
Resources:
  BadRequestBodyGatewayResponse:
    Type: AWS::ApiGateway::GatewayResponse
    Properties:
      RestApiId: !Ref ApiGateway
      ResponseType: BAD_REQUEST_BODY
      StatusCode: 400
      ResponseTemplates:
        application/json: |
          { "message": "Invalid request body: $context.error.messageString" }
      ResponseParameters:
        gatewayresponse.header.Content-Type: "'application/json'"

  UnauthorizedGatewayResponse:
    Type: AWS::ApiGateway::GatewayResponse
    Properties:
      RestApiId: !Ref ApiGateway
      ResponseType: UNAUTHORIZED
      StatusCode: 401
      ResponseTemplates:
        application/json: |
          { "message": "Missing or invalid Twilio signature" }
      ResponseParameters:
        gatewayresponse.header.Content-Type: "'application/json'"
```

These GatewayResponses avoid invoking the Lambda for invalid or unauthorized requests and return clear JSON errors to clients.

## 6. Monitoring and Logging

### 6.1 Lambda Execution Logs (INFO / ERROR)
- Log at **INFO** for key steps: webhook receipt, validation success/failure, queue sends, etc.
- Log at **ERROR** for unhandled exceptions or downstream failures.
- Use structured logging (JSON or key/value) to ease parsing in CloudWatch.
- Configure CloudWatch Logs retention (e.g., 30 days) via LogGroup resource.

### 6.2 API Gateway Access Logging
- Enable **Access Logging** on the API stage to capture request/response metadata.
- Example AccessLogSetting (SAM snippet):
  ```yaml
  AccessLogSetting:
    DestinationArn: !GetAtt ApiGatewayAccessLogGroup.Arn
    Format: '{"requestId":"$context.requestId","ip":"$context.identity.sourceIp","userAgent":"$context.identity.userAgent","requestTime":"$context.requestTime","method":"$context.httpMethod","path":"$context.resourcePath","status":"$context.status","latency":"$context.integrationLatency"}'
  ```
- Create a dedicated LogGroup and grant `logs:PutLogEvents` to API Gateway.

### 6.3 X-Ray Tracing
- Enable **Active Tracing** on both API Gateway and the Lambda function.
- In SAM/CloudFormation:
  ```yaml
  ApiGatewayStage:
    Properties:
      TracingEnabled: true
  IncomingWebhookHandler:
    Properties:
      TracingConfig:
        Mode: Active
  ```
- (Optional) Instrument your Lambda code with the X-Ray SDK to capture subsegment traces (DynamoDB, SQS).

### 6.4 CloudWatch Metrics & Alarms
- Use built-in metrics:
  - **API Gateway**: 4XX, 5XX, Latency, IntegrationLatency, CacheHitCount.
  - **Lambda**: Errors, Throttles, Duration, IteratorAge (for stream-based triggers).
- Define **Metric Filters** on access logs and Lambda logs for patterns like missing signature or high retry counts.
- Create **Alarms**:
  - 5XX > 1% over 5-minute window
  - Throttles > 0
  - High IntegrationLatency (>500ms)
  - Stalled Conversation Count (from custom metric)

## 7. Deployment Strategy

To provision API Gateway (and related Lambda/IAM resources) in each environment using AWS SAM:

### 7.0 Prerequisites
- AWS CLI installed & configured with access to the target AWS account
- AWS SAM CLI installed (`sam --version`)
- Docker running (for `--use-container` builds)
- Git checkout on the correct branch:
  - `develop` for deploying **dev**
  - `main` for deploying **prod**

### 7.1 CloudFormation/SAM Template
All API Gateway resources (APIs, Resources, Methods, Models, Validators, GatewayResponses) and Lambda functions are defined in the `template.yaml` file at the project root. The template is parameterized with at least:

- `EnvironmentName` (e.g. `dev` or `prod`) for resource naming and configuration
- `LogLevel` to control runtime verbosity

### 7.2 Build Process
Build your application and dependencies in a Lambda-like container to produce deployment artifacts:

```bash
# From the project root
sam build --use-container
```
- Installs dependencies inside Docker to match the Lambda runtime environment
- Packages code and a processed `template.yaml` into the `.aws-sam/build/` directory

### 7.3 Deploying with SAM CLI

#### 7.3.1 Deploy to DEV
```bash
git checkout develop && git pull origin develop
sam deploy \
  --template-file .aws-sam/build/template.yaml \
  --stack-name replies-engine-dev \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --resolve-s3 \
  --parameter-overrides EnvironmentName=dev LogLevel=DEBUG
```

#### 7.3.2 Deploy to PROD
```bash
git checkout main && git pull origin main
sam deploy \
  --template-file .aws-sam/build/template.yaml \
  --stack-name replies-engine-prod \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --resolve-s3 \
  --parameter-overrides EnvironmentName=prod LogLevel=INFO
```

> **Note:** `--resolve-s3` automatically uploads built artifacts to the SAM-managed S3 deployment bucket.

### 7.4 Post-Deployment Manual Steps
Certain resources still require manual setup after stack deployment:
- **API Keys & Usage Plans:** Create or attach API keys to the appropriate Usage Plan and Stage.
- **Secrets Manager:** Populate environment-specific secrets (e.g. Twilio auth tokens) into AWS Secrets Manager (`/replies-engine/${EnvironmentName}/twilio`).
- **DynamoDB Seed Data:** Insert initial company and project configuration into the `ConversationsTable` for each environment.
- **SNS Subscriptions:** Subscribe operational endpoints to the `critical-alerts-${EnvironmentName}` SNS topic and confirm subscriptions.

## 8. Testing Strategy

// ... existing code ...

## 8. Manual Deployment Status (AWS CLI)

This section tracks the progress of manually deploying the API Gateway components described in this document using the AWS CLI.

- [x] Create REST API (`ai-multi-comms-webhook-dev`)
- [x] Create Resources (`/whatsapp`, `/sms`, `/email`)
- [x] Create Request Model (`WhatsAppSMSWebhookModel`)
- [x] Create Request Validator (`ValidateHeadersAndBody`)
- [x] Configure `/whatsapp` OPTIONS Method (CORS)
- [x] Configure `/whatsapp` POST Method (Structure Only)
- [x] Create IAM Role for Logging (`apigateway-logs-role-ai-multi-comms-webhook-dev`)
- [x] Ensure CloudWatch Log Group (`/aws/apigateway/ai-multi-comms-webhook-dev-access-logs`)
- [x] Associate Logging Role with API Gateway Account Settings (`update-account`)
- [x] Apply Resource Policy (Require `X-Twilio-Signature`)
- [ ] Configure `/whatsapp` POST Integration (Lambda Proxy)
- [ ] Configure `/whatsapp` POST Integration Responses (Error Mapping)
- [ ] Create Deployment and `dev` Stage
- [ ] Configure `dev` Stage (Access Logging, Tracing)
- [ ] Create and Associate Usage Plan / API Key