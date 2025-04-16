# AI Multi-Comms Engine - Incoming Replies Handling - High-Level Design (HLD) - v1.0

This document outlines the high-level design for handling incoming user replies, starting with WhatsApp messages received via Twilio webhooks, as an extension to the AI Multi-Communications Engine.

## 1. Process Flow

1.  **Webhook Reception:**
    *   Twilio receives a reply message from the end-user via WhatsApp.
    *   Twilio sends an HTTP POST request (webhook) containing the message details (sender number, message body, etc.) to a predefined endpoint in the AWS infrastructure.
    *   This endpoint is handled by **API Gateway**.

2.  **Initial Processing & DB Lookup (New Lambda: `IncomingWebhookHandler`):**
    *   API Gateway triggers a new Lambda function (e.g., `IncomingWebhookHandler`).
    *   This Lambda parses the incoming Twilio payload to extract essential information (sender's phone number `recipient_tel`, message content).
    *   It queries the `ConversationsTable` (DynamoDB) using `recipient_tel` (or a suitable secondary index) to find the existing conversation record.
    *   *Error Handling:* If no matching record is found, log an error or handle as per design (e.g., discard, start new conversation).
    *   If a record is found, retrieve the existing OpenAI `thread_id` and the `handoff_to_human` flag.

3.  **Routing Logic:**
    *   The `IncomingWebhookHandler` checks the `handoff_to_human` flag.
    *   **If `handoff_to_human` is `true`:**
        *   Construct a message payload (incoming message details, `conversation_id`, `thread_id`).
        *   Send payload to a dedicated **SQS queue for human intervention** (e.g., `ai-multi-comms-human-handoff-queue-dev`).
        *   Automated processing stops here for this message.
    *   **If `handoff_to_human` is `false`:**
        *   Construct a similar message payload.
        *   Send payload to the **SQS queue for AI-handled WhatsApp replies** (e.g., `ai-multi-comms-whatsapp-replies-queue-dev`).

4.  **Message Delay/Batching (SQS Feature):**
    *   Messages sent to `ai-multi-comms-whatsapp-replies-queue-dev` utilize SQS's `DelaySeconds` feature (e.g., 30 seconds).
    *   This delay allows subsequent messages in a user's burst to arrive before processing begins.
    *   *Note:* The specific logic for grouping/batching messages arriving within this window needs further definition.

5.  **AI Processing (New Lambda: `ReplyProcessorLambda`):**
    *   After the SQS delay, a separate Lambda function (e.g., `ReplyProcessorLambda`), triggered by `ai-multi-comms-whatsapp-replies-queue-dev`, receives the message(s).
    *   Perform validation on the message context.
    *   Interact with **OpenAI Assistants API**:
        *   Add user message(s) to the existing OpenAI `thread_id`.
        *   Run the appropriate OpenAI Assistant (e.g., `assistant_id_replies`) on the thread.
        *   Retrieve the AI-generated response.
    *   *Error Handling:* Implement checks for failures during OpenAI interaction.

6.  **Sending Reply via Twilio:**
    *   The `ReplyProcessorLambda` takes the AI-generated response.
    *   Call the **Twilio API** to send the response back to the original sender's WhatsApp number.
    *   *Error Handling:* Implement checks for failures during Twilio sending.

7.  **Final Conversation Update:**
    *   After sending the reply (or noting a failure), the `ReplyProcessorLambda` updates the corresponding record in the `ConversationsTable` (DynamoDB).
    *   Include user message(s), AI reply, timestamps, and update status.

## 2. Key Components Involved

*   **API Gateway:** New endpoint to receive Twilio webhooks.
*   **Lambda (`IncomingWebhookHandler`):** Parses webhook, queries DB, routes to SQS.
*   **DynamoDB (`ConversationsTable`):** Queried for existing conversation, `thread_id`, and `handoff_to_human` flag. Updated at the end.
*   **SQS (`ai-multi-comms-human-handoff-queue-dev`):** Queue for messages needing human review.
*   **SQS (`ai-multi-comms-whatsapp-replies-queue-dev`):** Queue for messages to be handled by AI, configured with `DelaySeconds`.
*   **Lambda (`ReplyProcessorLambda`):** Triggered by replies queue, interacts with OpenAI and Twilio, updates DB.
*   **OpenAI Assistants API:** Used to continue existing conversation threads.
*   **Twilio API:** Used to send the AI-generated reply back to the user.
*   **AWS Secrets Manager:** (Implicitly used by `ReplyProcessorLambda` to fetch OpenAI/Twilio credentials, similar to the outgoing processor).

## 3. Assumptions & Considerations

*   Focus is solely on WhatsApp via Twilio for this initial replies HLD.
*   A unique identifier (like `recipient_tel`) can reliably link incoming messages to existing conversation records in DynamoDB. An appropriate index might be needed on `ConversationsTable`.
*   The logic for handling multiple messages arriving during the SQS delay window (step 4) needs detailed design (e.g., combine messages before sending to AI, process sequentially).
*   Error handling paths (DB lookup failure, OpenAI failure, Twilio failure, DB update failure) need robust implementation.
*   Security for the webhook endpoint (e.g., validating Twilio signatures) is crucial.
*   Existing CI/CD and SAM templates will need updates to incorporate these new resources. 