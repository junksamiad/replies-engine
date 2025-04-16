# AI Multi-Comms Engine - Incoming Replies Handling - High-Level Design (HLD) - v1.0

This document outlines the high-level design for handling incoming user replies, starting with WhatsApp messages received via Twilio webhooks, as an extension to the AI Multi-Communications Engine.

## 1. Process Flow

1.  **Webhook Reception:**
    *   Twilio receives a reply message from the end-user via WhatsApp.
    *   Twilio sends an HTTP POST request (webhook) containing the message details (sender number, message body, etc.) to a predefined endpoint in the AWS infrastructure.
    *   API Gateway receives this webhook and validates it through:
        * Resource policies requiring specific headers
        * Request validators ensuring correct format
        * Throttling and quota limits protecting against abuse

2.  **Initial Processing & DB Lookup (Lambda: `IncomingWebhookHandler`):**
    *   API Gateway triggers the `IncomingWebhookHandler` Lambda function.
    *   This Lambda parses the incoming Twilio payload to extract essential information (sender's phone number `recipient_tel`, message content).
    *   It queries the `ConversationsTable` (DynamoDB) using `recipient_tel` (or a suitable secondary index) to find the existing conversation record.
    *   **Conversation Record Update:**
        *   If a matching record is found, the Lambda immediately updates the record with:
            *   The incoming user message content
            *   Updated timestamp
            *   Change conversation status to `user_reply_received`
            *   Any other relevant metadata from the message
        *   This update ensures the message is recorded even if later processing fails.
    *   **Context Object Creation:**
        *   The Lambda creates a comprehensive context object containing:
            *   Message metadata (sender, receiver, content, timestamps)
            *   Conversation details (ID, status, thread_id)
            *   Company and project information
            *   Credential references for downstream processing
            *   Processing metadata (request_id, channel type)
    *   **Error Handling:** If no matching record is found, the Lambda sends a templated fallback message (rate-limited) informing the user that the conversation is no longer active.

3.  **Routing Logic:**
    *   The `IncomingWebhookHandler` checks the `handoff_to_human` flag in the conversation record.
    *   **If `handoff_to_human` is `true`:**
        *   Send the context object to the **SQS queue for human intervention** (e.g., `ai-multi-comms-handoff-queue-dev`).
        *   Automated AI processing stops here for this message.
    *   **If `handoff_to_human` is `false`:**
        *   Send the context object to the **SQS queue for AI-handled replies** (e.g., `ai-multi-comms-whatsapp-replies-queue-dev`).
    *   The Lambda returns a successful response to Twilio immediately after queueing, not waiting for processing to complete.

4.  **Message Delay/Batching (SQS Feature):**
    *   Messages sent to `ai-multi-comms-whatsapp-replies-queue-dev` utilize SQS's `DelaySeconds` feature (30 seconds).
    *   This delay allows subsequent messages in a user's burst to arrive before processing begins.
    *   Standard queue configuration includes:
        *   Visibility timeout: 2 minutes
        *   Message retention: 14 days
        *   Dead-letter queue for failed processing attempts

5.  **AI Processing (Lambda: `ReplyProcessorLambda`):**
    *   After the SQS delay, the `ReplyProcessorLambda` function, triggered by the replies queue, receives the context object.
    *   This Lambda retrieves the necessary credentials from AWS Secrets Manager using the references in the context.
    *   It then interacts with the **OpenAI Assistants API**:
        *   Add user message(s) to the existing OpenAI `thread_id` found in the context.
        *   Run the appropriate OpenAI Assistant on the thread.
        *   Monitor the run for completion or function calls.
        *   Retrieve the AI-generated response once complete.
    *   The Lambda enriches the context object with the AI response and processing metadata.
    *   **Error Handling:** If the OpenAI interaction fails, the Lambda records the error, updates the conversation status, and optionally routes to human intervention.

6.  **Sending Reply via Twilio (Lambda: `TwilioSenderLambda`):**
    *   The `ReplyProcessorLambda` sends the enriched context to another SQS queue for sending.
    *   The `TwilioSenderLambda` retrieves the context and handles the external communication:
        *   Retrieves Twilio credentials from AWS Secrets Manager using the reference in the context.
        *   Formats the AI-generated response for the channel (WhatsApp).
        *   Calls the **Twilio API** to send the message to the original sender's number.
        *   Records the sending results in the context.
    *   **Error Handling:** Implements retries for transient failures and records permanent failures.

7.  **Final Conversation Update:**
    *   The `TwilioSenderLambda` performs the final update to the conversation record in DynamoDB:
        *   Records the AI response content
        *   Updates the conversation status to `ai_response_sent`
        *   Stores message IDs and delivery metadata
        *   Updates relevant timestamps

## 2. Key Components Involved

*   **API Gateway:** 
    *   Secure endpoint to receive Twilio webhooks
    *   Resource policies and request validation for security
    *   Throttling and quota protection against abuse

*   **Lambda Functions:**
    *   `IncomingWebhookHandler`: Processes webhooks, updates conversation with user message, routes to appropriate queue
    *   `ReplyProcessorLambda`: Handles AI interaction and response generation
    *   `TwilioSenderLambda`: Manages external communication and final record updates

*   **DynamoDB (`ConversationsTable`):** 
    *   Primary store for conversation records
    *   Indexed on primary_channel (phone number/email) for efficient lookups
    *   Stores conversation state, messages, and metadata

*   **SQS Queues:**
    *   `ai-multi-comms-whatsapp-replies-queue-dev`: For messages to be handled by AI, with 30-second delay
    *   `ai-multi-comms-handoff-queue-dev`: For messages requiring human review
    *   `ai-multi-comms-sender-queue-dev`: For messages ready to be sent externally

*   **External Services:**
    *   **OpenAI Assistants API**: Used to continue existing conversation threads
    *   **Twilio API**: Used to send the AI-generated reply back to the user

*   **AWS Secrets Manager:** 
    *   Securely stores credentials for external services
    *   Referenced by credential identifiers in the context object

## 3. Single Lambda for Multi-Channel Processing

To efficiently handle webhooks from multiple communication channels, the `IncomingWebhookHandler` Lambda is designed as a unified processor for all incoming webhooks:

*   **Multi-Channel API Routes:**
    *   API Gateway exposes distinct routes for each channel (/whatsapp, /sms, /email)
    *   All routes integrate with the same Lambda function
    *   Channel type is determined by the API path in the event object

*   **Unified Processing Approach:**
    *   Employs a parser factory pattern to handle channel-specific payload formats
    *   Converts all webhook formats into a standardized context object
    *   Common logic for conversation lookup, update, and queue routing is shared
    *   Channel-specific processing is isolated in well-defined sections

*   **Resource Considerations:**
    *   Lambda sized appropriately to handle all webhook types efficiently
    *   Memory allocation accounts for varying payload sizes across channels
    *   Timeout configured to accommodate all expected processing paths
    *   Monitoring in place to identify any channel-specific performance issues

*   **Benefits:**
    *   Reduced cold start latency through higher invocation frequency
    *   Simplified deployment and maintenance of a single codebase
    *   Consistent handling of business logic across all channels
    *   More efficient resource utilization and cost management

This approach allows for centralized processing logic while still maintaining the flexibility to handle channel-specific requirements. Only after the SQS queue do we split into channel-specific Lambdas for specialized processing needs.

## 4. Structured Context Object

A standardized context object flows through the system, enriched at each step:

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
    "conversation_status": "user_reply_received",
    "hand_off_to_human": false,
    "thread_id": "thread_abc123xyz"
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

## 5. Assumptions & Considerations

*   Focus is initially on WhatsApp via Twilio, with the architecture designed to support future channels (SMS, email).
*   Message timestamps are preserved throughout the pipeline for accurate conversation ordering.
*   Concurrency and race conditions are addressed through:
    *   DynamoDB conditional updates
    *   Optimistic locking for record modifications
    *   SQS FIFO queues when strict ordering is required
*   Error handling paths include:
    *   DB lookup failures
    *   OpenAI API failures
    *   Twilio API failures
    *   Rate limiting for unknown senders
*   Security for webhook endpoints includes:
    *   API Gateway resource policies
    *   Request validation
    *   Throttling and quotas
*   Timeouts and retries are configured appropriately for each Lambda and external service interaction.
*   The architecture minimizes processing costs by:
    *   Validating requests early in the pipeline
    *   Using AWS Secrets Manager references instead of embedding credentials
    *   Leveraging serverless components that scale with demand

## 6. Future Enhancements

*   Support for SMS and email channels
*   Real-time status updates for conversations
*   Enhanced monitoring and alerting
*   Analytics and reporting capabilities
*   Integration with CRM systems 