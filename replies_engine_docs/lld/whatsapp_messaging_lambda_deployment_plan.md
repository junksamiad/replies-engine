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

## Phase 2: Package Lambda Code (Manual CLI)

4.  **Navigate to Lambda Code Directory:**
    ```bash
    cd src/messaging_lambda/whatsapp/lambda_pkg/
    ```
5.  **Install Dependencies Locally:** Create a package directory and install dependencies into it.
    ```bash
    mkdir package
    pip install --target ./package -r ../requirements.txt
    ```
6.  **Prepare Deployment Package:** Copy the Lambda code into the package directory.
    ```bash
    cp -r ./* ./package/
    ```
7.  **Create Zip File:** Navigate into the package directory and zip its contents.
    ```bash
    cd package
    zip -r ../whatsapp_messaging_lambda_deployment.zip .
    cd .. # Go back to lambda_pkg directory
    ```
    *You will now have `whatsapp_messaging_lambda_deployment.zip` in the `src/messaging_lambda/whatsapp/` directory.* 

## Phase 3: Create/Update AWS Resources (Manual CLI)

*(Execute these commands from the project root directory or ensure paths in commands are adjusted)*

8.  **Define IAM Role Name and Policy Name:** Choose unique names.
    *   `ROLE_NAME="WhatsAppMessagingLambdaRole"`
    *   `POLICY_NAME="WhatsAppMessagingLambdaPolicy"`
9.  **Create Trust Policy JSON:** Create a file named `lambda-trust-policy.json` with the following content:
    ```json
    {
      "Version": "2012-10-17",
      "Statement": [
        {
          "Effect": "Allow",
          "Principal": {
            "Service": "lambda.amazonaws.com"
          },
          "Action": "sts:AssumeRole"
        }
      ]
    }
    ```
10. **Create IAM Role:** (Check if it exists first: `aws iam get-role --role-name $ROLE_NAME`)
    ```bash
    aws iam create-role --role-name $ROLE_NAME --assume-role-policy-document file://lambda-trust-policy.json
    # Note the ARN from the output (e.g., "Arn": "arn:aws:iam::ACCOUNT_ID:role/WhatsAppMessagingLambdaRole")
    # Export it for later use: export ROLE_ARN="arn:aws:iam::ACCOUNT_ID:role/WhatsAppMessagingLambdaRole"
    ```
11. **Define Permissions Policy JSON:** Create a file named `lambda-permissions-policy.json`. **Crucially, replace placeholders** with your actual Account ID, Region, Table Names, Queue ARN, and Secret ARNs.
    ```json
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents"
                ],
                "Resource": "arn:aws:logs:YOUR_REGION:YOUR_ACCOUNT_ID:log-group:/aws/lambda/WhatsAppMessagingLambda*:*"
            },
            {
                "Effect": "Allow",
                "Action": [
                    "dynamodb:UpdateItem",
                    "dynamodb:Query",
                    "dynamodb:GetItem",
                    "dynamodb:BatchWriteItem",
                    "dynamodb:DeleteItem"
                ],
                "Resource": [
                    "arn:aws:dynamodb:YOUR_REGION:YOUR_ACCOUNT_ID:table/YOUR_CONVERSATIONS_TABLE_NAME",
                    "arn:aws:dynamodb:YOUR_REGION:YOUR_ACCOUNT_ID:table/YOUR_CONVERSATIONS_STAGE_TABLE_NAME",
                    "arn:aws:dynamodb:YOUR_REGION:YOUR_ACCOUNT_ID:table/YOUR_CONVERSATIONS_TRIGGER_LOCK_TABLE_NAME"
                ]
            },
            {
                "Effect": "Allow",
                "Action": "sqs:ChangeMessageVisibility",
                "Resource": "YOUR_WHATSAPP_QUEUE_ARN"
            },
            {
                "Effect": "Allow",
                "Action": "secretsmanager:GetSecretValue",
                "Resource": [
                    "YOUR_OPENAI_SECRET_ARN",
                    "YOUR_TWILIO_SECRET_ARN"
                ]
            }
        ]
    }
    ```
12. **Create/Update IAM Policy:** (Check if it exists: `aws iam get-policy --policy-arn arn:aws:iam::ACCOUNT_ID:policy/$POLICY_NAME`)
    *   **If creating:**
        ```bash
        aws iam create-policy --policy-name $POLICY_NAME --policy-document file://lambda-permissions-policy.json
        # Note the ARN: export POLICY_ARN="arn:aws:iam::ACCOUNT_ID:policy/$POLICY_NAME"
        ```
    *   **If updating (requires existing policy ARN):**
        ```bash
        # Get the default version ID first (e.g., v1, v2...)
        # aws iam list-policy-versions --policy-arn $POLICY_ARN
        # aws iam delete-policy-version --policy-arn $POLICY_ARN --version-id <OLD_VERSION_ID> # Optional: Delete old non-default versions if needed
        aws iam create-policy-version --policy-arn $POLICY_ARN --policy-document file://lambda-permissions-policy.json --set-as-default
        ```
13. **Attach Policy to Role:**
    ```bash
    aws iam attach-role-policy --role-name $ROLE_NAME --policy-arn $POLICY_ARN
    # Allow time for permissions to propagate (a few seconds)
    ```
14. **Define Lambda Environment Variables:** Prepare the variables needed. **Replace placeholders**.
    ```bash
    # Example - adjust values as needed
    VARIABLES="{\
CONVERSATIONS_TABLE='YOUR_CONVERSATIONS_TABLE_NAME',\
CONVERSATIONS_STAGE_TABLE='YOUR_CONVERSATIONS_STAGE_TABLE_NAME',\
CONVERSATIONS_TRIGGER_LOCK_TABLE='YOUR_CONVERSATIONS_TRIGGER_LOCK_TABLE_NAME',\
WHATSAPP_QUEUE_URL='YOUR_WHATSAPP_QUEUE_URL',\
SQS_HEARTBEAT_INTERVAL_MS='300000',\
LOG_LEVEL='INFO',\
AWS_REGION='YOUR_REGION'
}"
    ```
15. **Create/Update Lambda Function:** (Check if it exists: `aws lambda get-function --function-name WhatsAppMessagingLambda`)
    *   **If creating:**
        ```bash
        aws lambda create-function --function-name WhatsAppMessagingLambda \
        --runtime python3.11 \
        --role $ROLE_ARN \
        --handler index.handler \
        --zip-file fileb://src/messaging_lambda/whatsapp/whatsapp_messaging_lambda_deployment.zip \
        --environment "Variables=$VARIABLES" \
        --timeout 600 \
        --memory-size 512
        # Note the Function ARN
        ```
    *   **If updating:**
        ```bash
        aws lambda update-function-code --function-name WhatsAppMessagingLambda \
        --zip-file fileb://src/messaging_lambda/whatsapp/whatsapp_messaging_lambda_deployment.zip

        aws lambda update-function-configuration --function-name WhatsAppMessagingLambda \
        --environment "Variables=$VARIABLES" \
        --runtime python3.11 \
        --role $ROLE_ARN \
        --handler index.handler \
        --timeout 600 \
        --memory-size 512
        ```
16. **Create SQS Event Source Mapping:** (Check if mapping exists: `aws lambda list-event-source-mappings --function-name WhatsAppMessagingLambda --event-source-arn YOUR_WHATSAPP_QUEUE_ARN`)
    *   **If creating:**
        ```bash
        aws lambda create-event-source-mapping --function-name WhatsAppMessagingLambda \
        --event-source-arn YOUR_WHATSAPP_QUEUE_ARN \
        --batch-size 1 \
        --enabled
        ```
    *   **If updating (requires mapping UUID):**
        ```bash
        aws lambda update-event-source-mapping --uuid YOUR_MAPPING_UUID \
        --batch-size 1 \
        --enabled
        ```

## Phase 4: Post-Deployment Verification

17. **AWS Console Checks:**
    *   Go to the Lambda console, find the `WhatsAppMessagingLambda` function.
    *   Verify the Environment Variables are set correctly under the 'Configuration' -> 'Environment variables' tab.
    *   Check the 'Configuration' -> 'Triggers' tab to ensure the SQS queue trigger is present and enabled.
    *   Go to the SQS console, find `WhatsAppQueue`, check its configuration (especially the default visibility timeout - should be >= 10 minutes).
    *   Go to the DynamoDB console, check the `ConversationsStageTable` and `ConversationsTriggerLockTable` and verify TTL is enabled on the `expires_at` attribute under 'Table details' -> 'Additional settings'.

## Phase 5: End-to-End Test

18. **Prepare Test Data:**
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
19. **Trigger Lambda:**
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
20. **Monitor & Verify:**
    *   **CloudWatch Logs:** Immediately check the log group for `/aws/lambda/WhatsAppMessagingLambda`. Look for logs indicating processing steps, potential errors, heartbeat activity, and final success/failure.
    *   **Twilio Console:** Check your Twilio Programmable Messaging logs to see if an outbound WhatsApp message was attempted/sent to the `primary_channel`.
    *   **DynamoDB (`ConversationsTable`):** Refresh the test item. Verify:
        *   `messages` list has new 'user' and 'assistant' entries appended.
        *   `conversation_status` is updated (e.g., to `reply_sent`).
        *   `updated_at` timestamp has changed.
        *   OpenAI token counts (`prompt_tokens`, etc.) are populated in the assistant message map.
    *   **DynamoDB (`ConversationsStageTable`):** Verify the test staging items you created have been **deleted**.
    *   **DynamoDB (`ConversationsTriggerLockTable`):** Verify any lock item created for this `conversation_id` during the run has been **deleted**.

## Phase 6: Monitoring Setup

21. **CloudWatch Alarms:**
    *   Create Metric Filters on the Lambda's log group to capture CRITICAL errors (e.g., filter for `CRITICAL`).
    *   Create CloudWatch Alarms based on these Metric Filters (e.g., alarm if the count > 0 in 5 minutes). Configure the alarm to notify an SNS topic for alerts.
    *   Consider alarms on standard Lambda metrics like `Errors` and `Throttles`.

This plan covers the essential steps. Remember to replace placeholders with your actual resource names, IDs, and credentials. 