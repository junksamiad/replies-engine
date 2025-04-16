# API Gateway Webhook Endpoint - Low-Level Design

## 1. Purpose and Responsibilities

The API Gateway webhook endpoint serves as the entry point for all incoming communication replies in the replies-engine microservice. Its primary responsibilities include:

- Receiving HTTP POST requests from Twilio when users reply to WhatsApp messages
- Providing a secure, reliable endpoint that can validate the authenticity of incoming webhooks
- Routing validated requests to the appropriate Lambda function for processing
- Returning appropriate responses to Twilio to acknowledge receipt
- Supporting future expansion to other communication channels (SMS, email)

## 2. API Structure

### Resources and Methods

The API Gateway will be structured with the following resources and methods:

```
/
├── /whatsapp
│   ├── POST - Receives WhatsApp replies from Twilio
│   └── OPTIONS - Supports CORS
├── /sms (future)
│   ├── POST - Will receive SMS replies
│   └── OPTIONS - Will support CORS
└── /email (future)
    ├── POST - Will receive email replies
    └── OPTIONS - Will support CORS
```

Initially, only the `/whatsapp` endpoint will be fully implemented, but we will create placeholder resources for `/sms` and `/email` to support future expansion.

### Stage Configuration

- Stage Name: Defined by environment parameter (e.g., `dev`, `prod`)
- Stage Variables: None initially, but may be used for environment-specific configuration if needed

## 3. Request/Response Models

### WhatsApp Webhook Request (from Twilio)

Twilio sends webhooks with application/x-www-form-urlencoded content type. Key fields include:

```
{
  "SmsMessageSid": "SM...",
  "NumMedia": "0",
  "SmsSid": "SM...",
  "SmsStatus": "received",
  "Body": "User's reply message text",
  "To": "whatsapp:+1234567890",  // Our WhatsApp number
  "NumSegments": "1",
  "MessageSid": "SM...",
  "AccountSid": "AC...",
  "From": "whatsapp:+0987654321",  // User's WhatsApp number
  "ApiVersion": "2010-04-01"
}
```

Note: This is a simplified representation. The actual webhook contains additional fields that vary based on the message type and content. We will handle all Twilio fields and pass them to the Lambda function.

### Response to Twilio

A successful response to Twilio should be an HTTP 200 OK with a simple TwiML message or empty response. For our needs, we'll return:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<Response></Response>
```

This minimal TwiML response acknowledges receipt without performing any additional Twilio actions.

## 4. Integration with Backend

### Lambda Integration

- Integration Type: `AWS_PROXY` (Lambda Proxy Integration)
- Lambda Function: `IncomingWebhookHandler`
- HTTP Method: `POST`
- Content Handling: `CONVERT_TO_TEXT` (API Gateway will pass the form data as-is to Lambda)

### Request Mapping

With Lambda Proxy integration, API Gateway will automatically pass the full HTTP request to the Lambda function, including:
- Path parameters
- Query string parameters
- Headers
- Body
- Request context

No custom request mapping template is needed.

### Response Mapping

With Lambda Proxy integration, the Lambda function must return a response in the format:

```json
{
  "statusCode": 200,
  "headers": {
    "Content-Type": "text/xml"
  },
  "body": "<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response></Response>"
}
```

No custom response mapping template is needed.

## 5. Authentication & Security

### Twilio Signature Validation

The webhook endpoint will not use API Gateway's built-in authentication methods. Instead, the `IncomingWebhookHandler` Lambda function will validate the Twilio signature to ensure the request genuinely comes from Twilio:

1. Twilio includes an `X-Twilio-Signature` header in each webhook request
2. The Lambda function will:
   - Extract this header
   - Fetch the Twilio Auth Token from AWS Secrets Manager
   - Recreate the signature using the webhook URL and request parameters
   - Compare with the provided signature
   - Reject requests with invalid signatures

This approach provides strong security while maintaining flexibility.

### IP Restrictions (Optional)

As an optional additional security layer, we could restrict incoming requests to Twilio's IP ranges using WAF or resource policies. This is not a priority for the initial implementation but could be added later.

## 6. Rate Limiting & Throttling

### API Gateway Throttling

- Default account-level throttling: 10,000 requests per second (RPS)
- Default stage-level throttling: None

For the initial implementation, we will rely on API Gateway's default throttling settings, which are sufficient for our expected traffic. We can adjust these settings later if needed based on actual usage patterns.

### Custom Throttling (Future)

If needed, we can implement more granular throttling:
- Stage-level throttling: Limit total RPS to the API
- Method-level throttling: Set different limits for different endpoints

## 7. CORS Configuration

CORS configuration is required to allow web clients to interact with our API. For each resource:

- Allowed Origins: `*` for development, restricted to specific domains for production
- Allowed Methods: `POST`, `OPTIONS`
- Allowed Headers: `Content-Type`, `X-Twilio-Signature`, `Authorization`
- Allow Credentials: `false`

## 8. Logging & Monitoring

### Access Logging

Enable CloudWatch access logging with the following format:
```
$context.identity.sourceIp $context.identity.caller $context.identity.user [$context.requestTime] "$context.httpMethod $context.resourcePath $context.protocol" $context.status $context.responseLength $context.requestId
```

### Execution Logging

- Log Level: `INFO`
- Data Trace: `true` for development, `false` for production
- Metrics: Enabled

### CloudWatch Metrics

Monitor the following API Gateway metrics:
- Count
- 4XXError
- 5XXError
- Latency
- IntegrationLatency

## 9. Error Responses

The API Gateway will rely on default error responses for most error conditions. The IncomingWebhookHandler Lambda will handle specific error cases and return appropriate responses.

Common error scenarios:
- 400 Bad Request: Invalid webhook format
- 401 Unauthorized: Invalid Twilio signature
- 500 Internal Server Error: Unhandled exceptions

## 10. Deployment Strategy

The API Gateway will initially be deployed manually using the AWS CLI, with all resources created in the same operation:

```bash
# Create API
aws apigateway create-rest-api --name ai-multi-comms-api-dev

# Create resources and methods
# Set up integrations
# Deploy API to stage
```

Later, this process will be automated using an AWS SAM template and deployed via GitHub Actions.

## 11. Happy Path Analysis

### Preconditions
- API Gateway is deployed and accessible
- IncomingWebhookHandler Lambda function is deployed and properly configured
- Twilio is configured to send webhooks to our API endpoint

### Flow
1. User replies to a WhatsApp message sent by our system
2. Twilio receives the reply and sends a webhook POST request to our `/whatsapp` endpoint
3. API Gateway receives the request and forwards it to the IncomingWebhookHandler Lambda
4. Lambda processes the request and returns a 200 OK response with a TwiML body
5. API Gateway forwards the response back to Twilio
6. Twilio acknowledges the receipt and completes the webhook process

### Expected Outcome
- The incoming message is successfully received and processed
- Twilio receives a valid acknowledgment
- The message proceeds through the rest of the system for AI processing

### Performance Characteristics
- Expected latency: < 100ms for API Gateway processing
- Total processing time: < 1000ms for the entire webhook handling process

## 12. Unhappy Path Analysis

### Invalid Twilio Signature
1. Request arrives with an invalid or missing X-Twilio-Signature header
2. Lambda validates the signature and rejects the request
3. A 401 Unauthorized response is returned
4. The event is logged for security monitoring

### Rate Limiting/Throttling
1. Too many requests arrive simultaneously
2. API Gateway throttles excess requests
3. Twilio receives 429 Too Many Requests responses
4. Twilio's retry mechanism will attempt to resend the webhook later

### Lambda Failure
1. IncomingWebhookHandler Lambda encounters an unhandled exception
2. API Gateway receives a 500 Internal Server Error response
3. This error is returned to Twilio
4. The error is logged and alerts are triggered
5. Twilio's retry mechanism will attempt to resend the webhook later

## 13. Testing Strategy

### Unit Testing
- Test Lambda function's signature validation logic
- Test request parsing and error handling

### Integration Testing
- Test API Gateway configuration with mock requests
- Verify correct routing to Lambda function
- Verify proper handling of various request formats

### Manual Testing
- Use Twilio's developer tools to simulate webhook requests
- Verify end-to-end flow with actual WhatsApp messages

### Automated Testing in CI/CD
- Include API Gateway configuration in infrastructure tests
- Test endpoint availability and response codes

## 14. Implementation Considerations

### Resource Naming
- API Name: `${ProjectPrefix}-api-${EnvironmentName}`
- Stage Name: `${EnvironmentName}`
- Resource Path: `/whatsapp`

### Future Expansion
- Design API structure to easily accommodate additional channels
- Keep channel-specific logic in separate Lambda functions
- Use shared utilities for common webhook processing functions

## 15. Documentation Requirements

- API reference documentation
- Webhook format documentation
- Sequence diagram showing the flow of requests
- Integration guides for configuring Twilio

## 16. Next Steps

1. Create the API Gateway manually using AWS CLI
2. Configure the WhatsApp resource and POST method
3. Set up Lambda proxy integration
4. Configure logging and monitoring
5. Test the endpoint with mock Twilio requests
6. Document the final configuration for future reference 