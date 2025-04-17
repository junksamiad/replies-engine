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