# AI Multi-Comms Engine - Replies Engine Project Handover

## Project Overview

The AI Multi-Communications Engine is a serverless AWS application managing communication workflows. It consists of two separate microservices:

1. **template-sender-engine**: An existing microservice that handles outgoing messages
   - Receives API requests to `/initiate-conversation`
   - Validates, fetches config (DynamoDB CompanyDataTable)
   - Routes via SQS to channel-specific processors (currently whatsapp-channel-processor)
   - Uses OpenAI Assistants to generate template variables, sends messages via Twilio
   - Stores conversation state (including OpenAI thread_id) in DynamoDB (ConversationsTable)

2. **replies-engine**: A new microservice (this project) that processes incoming user replies
   - Will handle incoming replies from various channels (starting with WhatsApp via Twilio webhooks)
   - Will share existing DynamoDB tables (ConversationsTable and CompanyDataTable) with template-sender-engine
   - Will be implemented in Python using AWS Lambda, API Gateway, SQS, and other AWS services

## Current High-Level Design

### Core Processing Flow

1. **Webhook Reception**:
   - API Gateway receives POST from Twilio with incoming WhatsApp messages
   - Webhook endpoints will be structured as:
     - `/whatsapp` (initial implementation)
     - `/sms` (future)
     - `/email` (future)

2. **Initial Processing (IncomingWebhookHandler Lambda)**:
   - Validates Twilio signature
   - Parses payload (extracts sender number, message)
   - Queries ConversationsTable to find the conversation record, OpenAI thread_id, and check handoff_to_human flag

3. **Routing**:
   - If handoff_to_human is true → Send to ai-multi-comms-human-handoff-queue-dev (SQS)
   - If handoff_to_human is false → Send to ai-multi-comms-whatsapp-replies-queue-dev (SQS)

4. **Message Delay**:
   - SQS queue will use DelaySeconds (e.g., 30s) to allow for batching of messages

5. **AI Processing (ReplyProcessorLambda)**:
   - Triggered by SQS queue
   - Adds user message to existing OpenAI thread_id
   - Runs appropriate "replies" assistant
   - Gets response and sends it back to user via Twilio API
   - Updates ConversationsTable with message history and status

### Key Design Decisions

1. **Separate Microservice**: The replies-engine will be in its own project/repository with its own template.yaml, src/, tests/, etc.

2. **Shared Databases**: Will share the existing ConversationsTable and CompanyDataTable with template-sender-engine
   - DynamoDB table resources should be defined only once (in the original template-sender-engine or a dedicated shared infrastructure stack)
   - Tables will be accessed using table names passed as environment variables with appropriate IAM permissions

3. **API Gateway Strategy**: A single API Gateway resource with different resource paths for each channel

4. **Webhook Security**: Will validate the X-Twilio-Signature header using the Twilio Auth Token from Secrets Manager (not using API Gateway API Keys)

## Current Project Status

We are currently in the planning phase. The following items have been completed:

- High-level design documentation
- Initial architectural decisions
- Requirements gathering

## Next Steps

The next sequential step is to create detailed Low-Level Design (LLD) documents for:

1. **IncomingWebhookHandler Lambda**: Detailed design of input validation, Twilio signature verification, DynamoDB lookups, and SQS routing logic
2. **ReplyProcessorLambda**: Detailed design of OpenAI integration, Twilio message sending, and DynamoDB updates
3. **API Gateway Configuration**: Detailed design of API routes, security, and request/response mapping
4. **SQS Queue Configuration**: Detailed design of queue settings, message format, and retry policies
5. **IAM Permissions**: Detailed design of IAM roles and policies for each component

Only after these detailed designs are complete will we begin implementation.

## Ground Rules

1. **Development Approach**:
   - Planning first, implementation second
   - Create comprehensive documentation (HLDs and LLDs) before any code
   - Build incrementally with a thin end-to-end layer first

2. **Deployment Strategy**:
   - Initial deployment manually via AWS CLI
   - Later transition to AWS SAM template once the basic functionality is proven
   - Support for multiple environments (dev, prod) using environment variables

3. **CI/CD**:
   - Will set up a GitHub Actions pipeline for testing and deployment

4. **Code Structure Best Practices**:
   - Follow the recommended Python package structure for Lambda functions:
     - Place Lambda code in `lambda_pkg` subdirectory under the app directory
     - Use `__init__.py` to mark packages properly
     - Keep `requirements.txt` in the parent app directory (not inside lambda_pkg)
     - Use relative imports within the lambda_pkg directory
     - Configure Lambda handler as `lambda_pkg.index.lambda_handler`
   - This structure ensures compatibility between local testing and AWS Lambda deployment
   - **IMPORTANT**: Create a proper project setup that eliminates the need to manually set PYTHONPATH
     - The template-sender-engine project required setting `export PYTHONPATH=$PYTHONPATH:./src_dev` before running tests
     - For replies-engine, we will address this upfront in the project structure

5. **Testing**:
   - Comprehensive unit tests for all components
   - Integration tests for key workflows
   - Local testing before deployment
   - Include setup scripts or a Makefile that handles environment configuration automatically

6. **Documentation**:
   - Maintain clear documentation for all components
   - Document design decisions and rationales
   - Update documentation as the design evolves

7. **Dependency Management**:
   - Specify exact package versions in requirements.txt to ensure consistency
   - Use only the latest stable versions of packages to avoid compatibility issues
   - Test package combinations in both local and CI/CD environments before deployment
   - Pin versions of critical dependencies (like OpenAI, Twilio, boto3) to prevent unexpected breaking changes
   - Document any package version constraints or known issues 