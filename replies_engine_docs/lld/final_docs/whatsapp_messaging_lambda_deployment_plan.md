# Deployment Plan: WhatsApp Messaging Lambda

This document outlines the steps to deploy the WhatsApp Messaging Lambda (`src/messaging_lambda/whatsapp/lambda_pkg/`), connect it to AWS services, and prepare for end-to-end testing.

## Phase 1: Prerequisites & Configuration (Manual Steps)

1.  **OpenAI Setup:**
    *   Ensure you have an OpenAI API Key readily available.
    *   Verify that the OpenAI Assistant intended for handling replies exists (as referenced by `assistant_id_replies` in the conversation configuration). Note down its **Assistant ID**. If it doesn't exist, create it via the OpenAI platform, ensuring its instructions are appropriate for generating replies based on the context provided by the `template-sender-engine`.
2.  **Twilio Setup:**
    *   Ensure you have your Twilio **Account SID** and **Auth Token**.
    *   Verify you have a Twilio Phone Number configured for WhatsApp sending. Note this **WhatsApp Sender Number**.
3.  **AWS Secrets Manager:**
    *   Navigate to AWS Secrets Manager in your target region (`eu-north-1` assumed unless specified otherwise).
    *   **Create/Verify OpenAI Secret:** Create a new secret (or verify an existing one) to store your OpenAI API Key.
        *   Secret type: "Other type of secret".
        *   Secret key/value: Use **Plaintext** and structure it exactly as the code expects: `{"ai_api_key": "sk-YOUR_OPENAI_API_KEY"}`.
        *   Secret name: Choose a descriptive name (e.g., `openai-api-key-replies-engine`). Note this **Secret Name/ARN**.
    *   **Create/Verify Twilio Secret:** Create a new secret (or verify an existing one) for Twilio credentials.
        *   Secret type: "Other type of secret".
        *   Secret key/value: Use **Plaintext** and structure it exactly as the code expects: `{"twilio_account_sid": "ACxxxxxxxxxxxx", "twilio_auth_token": "your_auth_token"}`.
        *   Secret name: Choose a descriptive name (e.g., `twilio-credentials-whatsapp`). Note this **Secret Name/ARN**.

## Phase 2: Infrastructure Definition (IaC using SAM)

4.  **Locate/Create `template.yaml`:** Determine if a `template.yaml` file already exists at the root of the `replies-engine` project or within `src/messaging_lambda/whatsapp/`. If not, create one, likely at the project root.
5.  **Define/Update SAM Template (`template.yaml`):** Add or modify the following resources within the template:
    *   **Lambda Function (`AWS::Serverless::Function`):**
        *   `Logical ID`: e.g., `WhatsAppMessagingLambda`
        *   `CodeUri`: `src/messaging_lambda/whatsapp/lambda_pkg/`
        *   `Handler`: `index.handler`
        *   `Runtime`: `python3.11` (or `python3.12`, ensure compatibility with dependencies)
        *   `Timeout`: `600` seconds (10 minutes - must be longer than the 540s OpenAI internal timeout).
        *   `MemorySize`: `512` MB (Adjust based on testing if needed).
        *   `Policies`: Define necessary IAM permissions. This is critical:
            *   Allow `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents` on CloudWatch Logs.
            *   Allow `dynamodb:UpdateItem`, `dynamodb:Query`, `dynamodb:GetItem`, `dynamodb:BatchWriteItem`, `dynamodb:DeleteItem` on the specific `ConversationsTable`, `ConversationsStageTable`, and `ConversationsTriggerLockTable` ARNs.
            *   Allow `sqs:ChangeMessageVisibility` on the specific `WhatsAppQueue` ARN (for the heartbeat).
            *   Allow `secretsmanager:GetSecretValue` on the specific ARNs of the OpenAI and Twilio secrets created in Phase 1.
        *   `Environment`:
            *   `Variables`:
                *   `CONVERSATIONS_TABLE`: Name of your conversations table.
                *   `CONVERSATIONS_STAGE_TABLE`: Name of your staging table.
                *   `CONVERSATIONS_TRIGGER_LOCK_TABLE`: Name of your trigger lock table.
                *   `WHATSAPP_QUEUE_URL`: URL of your `WhatsAppQueue`.
                *   `SQS_HEARTBEAT_INTERVAL_MS`: `300000` (5 minutes - or adjust as needed, must be less than queue visibility timeout).
                *   `LOG_LEVEL`: `INFO` (or `DEBUG` for more verbose logs).
                *   `AWS_REGION`: Your target AWS region (e.g., `eu-north-1`).
    *   **Lambda Event Source Mapping (`Events` section within Function or separate `AWS::Lambda::EventSourceMapping`):**
        *   `Type`: `SQS`
        *   `Properties`:
            *   `Queue`: ARN of the `WhatsAppQueue`.
            *   `BatchSize`: `1` (Based on handover, process one SQS message/conversation trigger at a time).
            *   `Enabled`: `True`
    *   **DynamoDB Tables (Verify/Update):**
        *   Check if `ConversationsStageTable` and `ConversationsTriggerLockTable` resources are defined *within this template*.
        *   If they are, **ensure** the `TimeToLiveSpecification` property is correctly defined for both:
            ```yaml
            Properties:
              TimeToLiveSpecification:
                AttributeName: expires_at # Must match the attribute used in the table
                Enabled: true
            ```
        *   If these tables are defined in a *different* SAM template (e.g., a shared resources template or the template for the first lambda), you'll need to update *that* template to include the TTL specification. **This TTL configuration is critical for automatic cleanup.**
    *   **SQS Queue (Verify):** Ensure the `WhatsAppQueue` exists and you have its ARN/URL. If it's defined in this template, reference it using `!Ref` or `!GetAtt QueueArn`. If defined elsewhere, you'll need the explicit ARN/URL.

## Phase 3: Build & Deploy

6.  **Navigate:** Open your terminal in the directory containing the `template.yaml` file.
7.  **Build:** Run `sam build --use-container`. This builds the Lambda deployment package inside a Docker container, ensuring dependencies are correctly packaged for the Lambda environment.
8.  **Deploy:** Run `sam deploy --guided`.
    *   Follow the prompts:
        *   `Stack Name`: Choose a name (e.g., `replies-engine-messaging-lambda`).
        *   `AWS Region`: Confirm your target region.
        *   `Parameter EnvironmentVariables ...`: Confirm the environment variables are correct (it should pick them up from the template).
        *   `Confirm changes before deploy`: Recommended `y`.
        *   `Allow SAM CLI IAM role creation`: Likely `y`.
        *   `Capabilities`: It will likely require `CAPABILITY_IAM`. Confirm `y`.
        *   `Save arguments to configuration file`: `y` is convenient for future deployments.
        *   `Deploy this changeset?`: `y`.

## Phase 4: Post-Deployment Verification

9.  **AWS Console Checks:**
    *   Go to the Lambda console, find the `WhatsAppMessagingLambda` function.
    *   Verify the Environment Variables are set correctly under the 'Configuration' tab.
    *   Check the 'Triggers' tab to ensure the SQS queue trigger is present and enabled.
    *   Go to the SQS console, find `WhatsAppQueue`, check its configuration (especially the default visibility timeout - should be >= 10 minutes to accommodate Lambda execution and OpenAI processing).
    *   Go to the DynamoDB console, check the `ConversationsStageTable` and `ConversationsTriggerLockTable` and verify TTL is enabled on the `expires_at` attribute under 'Table details' -> 'Additional settings'.

## Phase 5: End-to-End Test

10. **Prepare Test Data:**
    *   **DynamoDB (`ConversationsTable`):** Manually create or update an item representing the test conversation. Ensure it has:
        *   Correct `primary_channel` (user's WhatsApp number, e.g., `whatsapp:+1...`) and `conversation_id`.
        *   `conversation_status` is *not* equal to `processing_reply` (e.g., `template_sent` or `retry`).
        *   Valid `ai_config` including the `openai_config` -> `whatsapp` -> `assistant_id_replies` pointing to the correct Assistant ID.
        *   Valid `channel_config` including `whatsapp` -> `company_whatsapp_number` and `whatsapp_credentials_id` pointing to the **Secret Name/ARN** of your Twilio secret.
        *   Valid `openai_thread_id` (from a previous interaction or create one for testing).
    *   **DynamoDB (`ConversationsStageTable`):** Manually create one or more items representing the staged messages for the *same* `conversation_id`. Ensure they have:
        *   `conversation_id` (Partition Key).
        *   `message_sid` (Sort Key - e.g., `SMxxx_test_stage1`).
        *   `primary_channel` (matching the main record).
        *   `received_at` (ISO 8601 timestamp).
        *   `body` (The content of the simulated incoming user message parts).
        *   `expires_at` (A Unix timestamp in the future, e.g., `$(date -v+1H +%s)`).
11. **Trigger Lambda:**
    *   Go to the SQS console for `WhatsAppQueue`.
    *   Click "Send and receive messages".
    *   In the "Message body", enter the trigger message JSON, matching the `conversation_id` and `primary_channel` used in the DynamoDB records:
        ```json
        {
          "conversation_id": "YOUR_TEST_CONVERSATION_ID",
          "primary_channel": "whatsapp:+1..."
        }
        ```
    *   Click "Send message".
12. **Monitor & Verify:**
    *   **CloudWatch Logs:** Immediately check the log group for `/aws/lambda/WhatsAppMessagingLambda` (or similar name based on deployment). Look for logs indicating processing steps, potential errors, heartbeat activity, and final success/failure.
    *   **Twilio Console:** Check your Twilio Programmable Messaging logs to see if an outbound WhatsApp message was attempted/sent to the `primary_channel`.
    *   **DynamoDB (`ConversationsTable`):** Refresh the test item. Verify:
        *   `messages` list has new 'user' and 'assistant' entries appended.
        *   `conversation_status` is updated (e.g., to `reply_sent`).
        *   `updated_at` timestamp has changed.
        *   OpenAI token counts (`prompt_tokens`, etc.) are populated in the assistant message map.
    *   **DynamoDB (`ConversationsStageTable`):** Verify the test staging items you created have been **deleted**.
    *   **DynamoDB (`ConversationsTriggerLockTable`):** Verify any lock item created for this `conversation_id` during the run has been **deleted**.

## Phase 6: Monitoring Setup

13. **CloudWatch Alarms:**
    *   Create Metric Filters on the Lambda's log group to capture CRITICAL errors (e.g., filter for `CRITICAL`).
    *   Create CloudWatch Alarms based on these Metric Filters (e.g., alarm if the count > 0 in 5 minutes). Configure the alarm to notify an SNS topic for alerts.
    *   Consider alarms on standard Lambda metrics like `Errors` and `Throttles`.

This plan covers the essential steps. Remember to replace placeholders with your actual resource names, IDs, and credentials. 