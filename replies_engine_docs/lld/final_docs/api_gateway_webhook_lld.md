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

Security for webhook validation (specifically Twilio signature verification) is now primarily handled within the backend Lambda function (`staging-lambda-test`) integration.

### 3.1 Resource Policy

A minimal Resource Policy is applied to API Gateway, primarily to allow invocations from any principal. It does **not** perform specific header checks like `X-Twilio-Signature`.

**Implementation Details (Current):**
```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": "*",
            "Action": "execute-api:Invoke",
            "Resource": "arn:aws:execute-api:<REGION>:<ACCOUNT_ID>:<API_ID>/*" 
        }
    ]
}
```
*(Replace placeholders with actual values)*

**Purpose:**
- Allows API Gateway stage to invoke backend resources.
- **Note:** Initial attempts to use `Condition` clauses with `aws:RequestHeader/X-Twilio-Signature` (using `Bool` or `Null` checks) failed due to this condition key not being supported for API Gateway resource policies, resulting in persistent 403 errors.

### 3.2 Request Validation

API Gateway Request Validators are **disabled** for the `/whatsapp` and `/sms` POST methods.

**Reasoning:**
- The critical validation (Twilio signature verification) must be performed within the Lambda function *after* retrieving tenant-specific credentials from DynamoDB/Secrets Manager.
- Attempting header or body validation at the API Gateway level proved problematic and unreliable due to the dynamic nature of the required Auth Token and issues with resource policy conditions.
- Payload format validation (e.g., ensuring required fields like `From`, `To`, `Body` exist) is now handled within the Lambda function's initial parsing step.

**Implementation Method:**
- Request Validator association on the `/whatsapp` and `/sms` POST methods is set to **NONE**.
- The method requirement for the `X-Twilio-Signature` header is set to **false**.

### 3.2.1 Example JSON Schema Model
*(This section can be removed or commented out, as the model is no longer enforced by API Gateway for these paths)*
<!--
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
-->

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

**Note on Retry Behavior:**
- API Gateway returns `429 TooManyRequests` when rate limits are exceeded; these are **not** remapped and will be passed through to Twilio as a 429 so that Twilio will automatically retry the webhook.
- Integration-level 4XX errors (schema/validation failures) errors returned by the Lambda are remapped to a `200 OK` empty TwiML response (see Section 5.2.1) to prevent Twilio from retrying on those application errors.

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

- **Goal:** Ensure Twilio retries only for transient infrastructure/server errors (typically 5xx) and *not* for non-transient validation/application errors (typically 4xx, including **INVALID_SIGNATURE**) or unexpected code bugs.

- **Implementation via Lambda:** The primary logic resides within the `staging-lambda-test` function:
    - **Non-Transient Errors (including INVALID_SIGNATURE):** When the Lambda detects a non-transient error (e.g., parsing failure, `CONVERSATION_NOT_FOUND`, `INVALID_SIGNATURE`, `PROJECT_INACTIVE`), it **directly returns a 200 OK response with empty TwiML** to API Gateway. This immediately signals success to Twilio and prevents retries.
    - **Known Transient Errors:** When the Lambda detects a known transient error (e.g., `DB_TRANSIENT_ERROR`, `SECRET_FETCH_TRANSIENT_ERROR`), it **intentionally raises an Exception**.

- API Gateway Method Integration Responses example (YAML snippet):
```yaml
  IntegrationResponses:
    # Default response for successful Lambda execution (passes through 200 OK TwiML)
    - StatusCode: 200
      # No SelectionPattern needed for default pass-through

    # Map Lambda Execution Errors matching our transient pattern to 503
    - StatusCode: 503
      # Regex pattern to match the exception message raised by the Lambda
      SelectionPattern: '.*Transient server error:.*'
      # No ResponseTemplates needed - API Gateway generates default body for 5xx

    # Optional: Catch-all for other Lambda Execution Errors (map to 500)
    - StatusCode: 500
      # SelectionPattern without specific regex acts as a default catch-all for errors
      # Ensure this comes AFTER more specific patterns like the 503 mapping.
      SelectionPattern: '.*'
```

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

API Gateway automatic Gateway Responses for `BAD_REQUEST_BODY` or `UNAUTHORIZED` (due to missing/invalid signature) are **no longer directly applicable** to the `/whatsapp` and `/sms` POST methods in the current configuration, as the Request Validator requiring these elements is disabled. These responses might become relevant if validators are re-enabled or other authorization mechanisms are added.

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

*   **REST API ID:** `fjvxpbzh6b` (`ai-multi-comms-webhook-dev`)
*   **Resource ID (`/whatsapp`):** `gyaxx2`

**Status:**
*   [x] Create REST API (`ai-multi-comms-webhook-dev`)
*   [x] Create Resources (`/whatsapp`, `/sms`, `/email`)
*   [x] Create Request Model (`TwilioWebhookPayloadModel` - ID: `fw4toh`) - *Model exists but not actively used on POST* 
*   [x] Create/Use Request Validator (`ValidateHeadersAndBody` - ID: `75r6is`) - *Validator exists but DISABLED on POST methods*
*   [x] Configure `/whatsapp` OPTIONS Method (CORS)
*   [x] Configure `/whatsapp` POST Method (Structure Only)
*   [~] Associate Validator & Model with `/whatsapp` POST (`application/x-www-form-urlencoded`) - *Association removed / Validator set to NONE*
*   [x] Create IAM Role for Logging (`apigateway-logs-role-ai-multi-comms-webhook-dev`)
*   [x] Ensure CloudWatch Log Group (`/aws/apigateway/ai-multi-comms-webhook-dev-access-logs`)
*   [x] Associate Logging Role with API Gateway Account Settings (`update-account`)
*   [x] Apply Resource Policy (Simplified - Allow Invoke, no header check) - *Updated from original design*
*   [x] Configure `/whatsapp` POST Integration (Lambda Proxy to `staging-lambda-test`)
    *   Integration URI: `arn:aws:apigateway:eu-north-1:lambda:path/2015-03-31/functions/arn:aws:lambda:eu-north-1:337909745089:function:staging-lambda-test/invocations`
*   [x] Configure `/whatsapp` POST Integration Responses (Error Mapping)
    *   Mapped `503` for `.*Transient server error:.*` pattern.
    *   Mapped `500` for `.*` pattern (catch-all).
    *   Mapped `200` for default success (passing through body).
*   [x] Create Deployment (`<LATEST_DEPLOYMENT_ID>`) and associate with `test` Stage
*   [x] Configure `test` Stage (Access Logging, Tracing) - *Enabled via subsequent updates*
*   [ ] Create and Associate Usage Plan / API Key - *Skipped (Not Required for current webhook)*