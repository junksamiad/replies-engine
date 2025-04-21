# IAM Roles and Policies - Low-Level Design

## 1. Purpose and Responsibilities

IAM Roles and Policies in the replies-engine microservice ensure secure and controlled access to AWS resources. They define:

- Execution permissions for each Lambda function
- Access boundaries for AWS services (DynamoDB, SQS, Secrets Manager, etc.)
- Proper implementation of the least privilege principle
- Secure handling of sensitive operations
- Appropriate logging capabilities

This LLD focuses on the IAM roles and policies required for the core components of the replies-engine microservice.

## 2. Core IAM Roles

### 2.1 StagingLambda Role

```yaml
StagingLambdaRole:
  Type: AWS::IAM::Role
  Properties:
    RoleName: !Sub '${ProjectPrefix}-staging-lambda-role-${EnvironmentName}'
    AssumeRolePolicyDocument:
      Version: '2012-10-17'
      Statement:
        - Effect: Allow
          Principal:
            Service: lambda.amazonaws.com
          Action: sts:AssumeRole
    ManagedPolicyArns:
      - !Sub 'arn:${AWS::Partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'
    Policies:
      - PolicyName: !Sub '${ProjectPrefix}-staging-lambda-policy-${EnvironmentName}'
        PolicyDocument:
          Version: '2012-10-17'
          Statement:
            # DynamoDB Permissions (Conversations, Stage, Lock tables)
            - Effect: Allow
              Action:
                - dynamodb:Query # For GSI lookup
                - dynamodb:GetItem # For fetching full context
                - dynamodb:PutItem # For stage table and lock table
              Resource:
                - !GetAtt ConversationsTable.Arn
                - !Sub '${ConversationsTable.Arn}/index/*' # Allows Query on GSIs/LSIs
                - !GetAtt ConversationsStageTable.Arn
                - !GetAtt ConversationsTriggerLockTable.Arn

            # SQS Permissions (All potential target queues)
            - Effect: Allow
              Action:
                - sqs:SendMessage
              Resource:
                - !GetAtt WhatsAppQueue.Arn
                - !GetAtt SMSQueue.Arn
                - !GetAtt EmailQueue.Arn
                - !GetAtt HumanHandoffQueue.Arn

            # Secrets Manager Permissions (Pattern matching required secrets)
            - Effect: Allow
              Action:
                - secretsmanager:GetSecretValue
              Resource:
                # Added specific patterns based on LLD naming convention
                - !Sub 'arn:${AWS::Partition}:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:${ProjectPrefix}/whatsapp-credentials/*/*/twilio-${EnvironmentName}'
                - !Sub 'arn:${AWS::Partition}:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:${ProjectPrefix}/sms-credentials/*/*/twilio-${EnvironmentName}'
                - !Sub 'arn:${AWS::Partition}:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:${ProjectPrefix}/email-credentials/*/*/sendgrid-${EnvironmentName}'
                # Add other potential credential types if needed

            # CloudWatch Logs Permissions (Provided by managed policy, but can be explicit)
            - Effect: Allow
              Action:
                - logs:CreateLogGroup
                - logs:CreateLogStream
                - logs:PutLogEvents
              Resource: !Sub 'arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/lambda/${ProjectPrefix}-staging-lambda-${EnvironmentName}:*'
```

### 2.2 MessagingLambda Role

```yaml
MessagingLambdaRole:
  Type: AWS::IAM::Role
  Properties:
    RoleName: !Sub '${ProjectPrefix}-messaging-lambda-role-${EnvironmentName}'
    AssumeRolePolicyDocument:
      Version: '2012-10-17'
      Statement:
        - Effect: Allow
          Principal:
            Service: lambda.amazonaws.com
          Action: sts:AssumeRole
    ManagedPolicyArns:
      - !Sub 'arn:${AWS::Partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'
    Policies:
      - PolicyName: !Sub '${ProjectPrefix}-messaging-lambda-policy-${EnvironmentName}'
        PolicyDocument:
          Version: '2012-10-17'
          Statement:
            # DynamoDB Permissions
            - Effect: Allow
              Action:
                - dynamodb:GetItem
                - dynamodb:UpdateItem
                - dynamodb:Query
                - dynamodb:BatchWriteItem
                - dynamodb:DeleteItem
              Resource:
                - !GetAtt ConversationsTable.Arn
                - !GetAtt ConversationsStageTable.Arn
                - !GetAtt ConversationsTriggerLockTable.Arn

            # SQS Permissions
            - Effect: Allow
              Action:
                - sqs:ReceiveMessage
                - sqs:DeleteMessage
                - sqs:GetQueueAttributes
                - sqs:SendMessage
              Resource:
                - !GetAtt WhatsAppQueue.Arn
                - !GetAtt SMSQueue.Arn
                - !GetAtt EmailQueue.Arn
                - !GetAtt HumanHandoffQueue.Arn

            # Secrets Manager Permissions
            - Effect: Allow
              Action:
                - secretsmanager:GetSecretValue
              Resource:
                - !Sub 'arn:${AWS::Partition}:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:${ProjectPrefix}/whatsapp-credentials/*/*/twilio-*'
```

## 3. Policy Details and Rationale

### 3.1 StagingLambda Policies

#### 3.1.1 DynamoDB Access

```yaml
- Effect: Allow
  Action:
    - dynamodb:Query # For GSI lookup of credential reference
    - dynamodb:GetItem # For fetching full conversation context post-validation
    - dynamodb:PutItem # For staging table and lock table
  Resource:
    - !GetAtt ConversationsTable.Arn # Permission needed on the table itself for GetItem
    - !Sub '${ConversationsTable.Arn}/index/*' # Permission needed on indexes for Query
    - !GetAtt ConversationsStageTable.Arn
    - !GetAtt ConversationsTriggerLockTable.Arn
```

**Rationale:**
- `dynamodb:Query` permission allows the Lambda to look up the credential reference and `conversation_id` via GSI.
- `dynamodb:GetItem` allows fetching the full conversation record using its primary key *after* successful signature validation.
- `dynamodb:PutItem` allows writing the context to the `conversations-stage` table and attempting the conditional write to the `conversations-trigger-lock` table.

#### 3.1.2 SQS Access

```yaml
- Effect: Allow
  Action:
    - sqs:SendMessage
  Resource:
    - !GetAtt WhatsAppQueue.Arn
    - !GetAtt SMSQueue.Arn
    - !GetAtt EmailQueue.Arn
    - !GetAtt HumanHandoffQueue.Arn
```

**Rationale:**
- `sqs:SendMessage` permission allows the Lambda to send trigger messages (with delay) to the Channel Queues or context messages (no delay) to the Human Handoff queue.

#### 3.1.3 Secrets Manager Access

```yaml
- Effect: Allow
  Action:
    - secretsmanager:GetSecretValue
  Resource:
    # Use wildcard patterns matching the naming convention
    - !Sub 'arn:${AWS::Partition}:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:${ProjectPrefix}/whatsapp-credentials/*/*/twilio-${EnvironmentName}'
    - !Sub 'arn:${AWS::Partition}:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:${ProjectPrefix}/sms-credentials/*/*/twilio-${EnvironmentName}'
    - !Sub 'arn:${AWS::Partition}:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:${ProjectPrefix}/email-credentials/*/*/sendgrid-${EnvironmentName}'
```

**Rationale:**
- `secretsmanager:GetSecretValue` allows the Lambda to retrieve the tenant-specific Twilio (or other provider) Auth Token required for webhook signature validation.
- Wildcard patterns (`*`) are used for company and project names to support the dynamic multi-tenant model.
- The environment name suffix (e.g., `-dev`) is included to scope permissions correctly per environment.
- **Important:** Ensure secret names in Secrets Manager strictly follow this pattern, including the environment suffix, for the policy to grant access.

#### 3.1.4 CloudWatch Logs Access

```yaml
- Effect: Allow
  Action:
    - logs:CreateLogGroup
    - logs:CreateLogStream
    - logs:PutLogEvents
  Resource: !Sub 'arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/lambda/${ProjectPrefix}-staging-lambda-${EnvironmentName}:*'
```

**Rationale:**
- This permission is typically granted by the attached AWSLambdaBasicExecutionRole managed policy. Explicit definition here is optional but provides clarity.

### 3.2 MessagingLambda Policies

#### 3.2.1 DynamoDB Access

```yaml
- Effect: Allow
  Action:
    - dynamodb:GetItem
    - dynamodb:UpdateItem
    - dynamodb:Query
    - dynamodb:BatchWriteItem
    - dynamodb:DeleteItem
  Resource:
    - !GetAtt ConversationsTable.Arn
    - !GetAtt ConversationsStageTable.Arn
    - !GetAtt ConversationsTriggerLockTable.Arn
```

**Rationale:**
- `dynamodb:GetItem`/`UpdateItem` for reading and locking/unlocking the main conversation record.
- `dynamodb:Query`/`BatchWriteItem` for reading the message batch from and deleting it from the `conversations-stage` table.
- `dynamodb:DeleteItem` for cleaning up the `conversations-trigger-lock` entry.

#### 3.2.2 SQS Access

```yaml
- Effect: Allow
  Action:
    - sqs:ReceiveMessage
    - sqs:DeleteMessage
    - sqs:GetQueueAttributes
    - sqs:SendMessage
  Resource:
    - !GetAtt WhatsAppQueue.Arn
    - !GetAtt SMSQueue.Arn
    - !GetAtt EmailQueue.Arn
    - !GetAtt HumanHandoffQueue.Arn
```

**Rationale:**
- Read permissions (`ReceiveMessage`, `DeleteMessage`, `GetQueueAttributes`) are needed for the Channel Queues that trigger this Lambda.
- `sqs:SendMessage` allows the Lambda to send the processed/merged payload to the *next* stage (e.g., AI queue, or potentially Human Handoff if logic dictates, though Handoff is usually determined by `StagingLambda`). Adjust target resources based on actual downstream queues.

## 4. Additional Considerations

### 4.1 AWS Managed Policies

Both roles include the AWS managed policy `AWSLambdaBasicExecutionRole`, which provides:

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
      "Resource": "*"
    }
  ]
}
```

**Rationale:**
- This managed policy grants the Lambda functions permissions to write to CloudWatch Logs.
- It's a standard policy for Lambda functions to enable proper logging.
- In production, consider replacing with a more restrictive custom policy that limits log access to specific log groups.
- Note: While this policy grants basic logging, the custom policy for `StagingLambda` also explicitly defines log permissions scoped to its specific log group for clarity and potential future restriction if the managed policy is removed.

### 4.2 CloudWatch Metrics Permissions

For enhanced monitoring, consider adding CloudWatch metrics permissions:

```yaml
- Effect: Allow
  Action:
    - cloudwatch:PutMetricData
  Resource: "*"
```

**Rationale:**
- Allows Lambdas to publish custom metrics for monitoring.
- The resource wildcard is necessary as CloudWatch metrics don't support resource-level permissions.

### 4.3 API Gateway Invoke Permission

The Staging Lambda requires a resource-based policy to allow the API Gateway service to invoke it.

**Implementation (Manual CLI Example):**
```bash
aws lambda add-permission --function-name <staging-lambda-function-name> \
  --region <region> \
  --statement-id apigateway-<stage>-invoke \
  --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:<region>:<account-id>:<api-id>/<stage>/POST/<resourcePath>"
```
*(Replace placeholders. `<resourcePath>` is e.g., `whatsapp` without leading slash)*

**Rationale:**
- Grants the specific API Gateway stage/method the explicit right to trigger the Lambda function.
- This is essential for `AWS_PROXY` integration to work.

## 5. Security Best Practices Implementation

### 5.1 Least Privilege Principle

Our IAM policies implement the least privilege principle by:

1. **Limiting Actions**: Only granting specific actions required for each function
2. **Scoping Resources**: Specifically targeting the exact resources needed
3. **Avoiding Wildcards**: Using ARNs with specific resource names where possible
4. **Service-Specific Roles**: Creating separate roles for each Lambda function

### 5.2 Secrets Handling

For secure secrets management:

1. **Restricted Access**: Only the Lambda functions that need specific secrets can access them
2. **Specific Secret ARNs**: Using ARN patterns that match only the required secrets
3. **Read-Only Access**: Only granting GetSecretValue permission, not write or list permissions

### 5.3 Resource Isolation

To maintain separation of concerns:

1. **Function-Specific Queues**: Each function only has access to its relevant queues
2. **Limited DB Operations**: Only granting the specific DynamoDB operations needed for each function
3. **Scoped Secret Access**: Limiting secret access to only what's needed for each function

## 6. Implementation and Testing Strategy

### 6.1 Manual Implementation Steps

```bash
# Create StagingLambda Role
aws iam create-role \
  --role-name ai-multi-comms-staging-lambda-role-dev \
  --assume-role-policy-document file://staging-lambda-trust-policy.json

# Attach AWSLambdaBasicExecutionRole
aws iam attach-role-policy \
  --role-name ai-multi-comms-staging-lambda-role-dev \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

# Create and attach custom policy
aws iam put-role-policy \
  --role-name ai-multi-comms-staging-lambda-role-dev \
  --policy-name ai-multi-comms-staging-lambda-policy-dev \
  --policy-document file://staging-lambda-policy.json

# Similar commands for MessagingLambda Role
```

### 6.2 Testing Approach

#### Policy Simulator Testing

Use the AWS IAM Policy Simulator to verify:
- Lambda can access required resources
- Lambda cannot access unauthorized resources
- All required actions are permitted

#### Permissions Boundary Testing

Create a test Lambda function with the role and verify:
- It can read from appropriate DynamoDB tables
- It can send messages to appropriate SQS queues
- It can access secrets from AWS Secrets Manager
- It can write logs to CloudWatch

#### Negative Testing

Verify that the roles correctly prevent:
- Writing to tables when only read is needed
- Reading from unauthorized tables
- Sending messages to unauthorized queues
- Accessing unauthorized secrets

## 7. Future SAM Template

In the future SAM template, these roles will be defined as:

```yaml
Resources:
  # StagingLambda Role
  StagingLambdaRole:
    Type: AWS::IAM::Role
    Properties:
      RoleName: !Sub '${ProjectPrefix}-staging-lambda-role-${EnvironmentName}'
      AssumeRolePolicyDocument: { ... }
      ManagedPolicyArns:
        - !Sub 'arn:${AWS::Partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'
      Policies:
        - PolicyName: !Sub '${ProjectPrefix}-staging-lambda-policy-${EnvironmentName}'
          PolicyDocument: { ... }

  # MessagingLambda Role
  MessagingLambdaRole:
    Type: AWS::IAM::Role
    Properties:
      RoleName: !Sub '${ProjectPrefix}-messaging-lambda-role-${EnvironmentName}'
      AssumeRolePolicyDocument: { ... }
      ManagedPolicyArns:
        - !Sub 'arn:${AWS::Partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'
      Policies:
        - PolicyName: !Sub '${ProjectPrefix}-messaging-lambda-policy-${EnvironmentName}'
          PolicyDocument: { ... }

  # Lambda Functions using these roles
  StagingLambdaFunction:
    Type: AWS::Serverless::Function
    Properties:
      # ... other properties
      Role: !GetAtt StagingLambdaRole.Arn

  MessagingLambdaFunction:
    Type: AWS::Serverless::Function
    Properties:
      # ... other properties
      Role: !GetAtt MessagingLambdaRole.Arn
```

## 8. Happy Path Analysis

### 8.1 StagingLambda IAM Role

#### Preconditions
- IAM role and policies are created with correct permissions
- Lambda function is configured to use this role

#### Flow
1. Lambda is triggered by API Gateway
2. Lambda uses IAM role to authenticate AWS requests
3. Lambda queries DynamoDB to retrieve conversation records
4. Lambda sends messages to appropriate SQS queues

#### Expected Outcome
- Lambda can successfully access required AWS resources
- No unauthorized access attempts occur
- Operational logging shows successful authentication

### 8.2 MessagingLambda IAM Role

#### Preconditions
- IAM role and policies are created with correct permissions
- Lambda function is configured to use this role

#### Flow
1. Lambda is triggered by SQS event
2. Lambda uses IAM role to authenticate AWS requests
3. Lambda retrieves secrets from Secrets Manager
4. Lambda processes messages and updates DynamoDB

#### Expected Outcome
- Lambda can successfully access required AWS resources
- No unauthorized access attempts occur
- Operational logging shows successful authentication

## 9. Unhappy Path Analysis

### 9.1 Missing Permissions

#### Flow
1. Lambda attempts to access a resource without proper permissions
2. AWS denies the request with an Access Denied error
3. Lambda logs the error
4. Function fails to complete its task

#### Expected Outcome
- Clear error message indicating the missing permission
- CloudWatch logs show the specific access denial
- Function may need to be updated with correct permissions

### 9.2 Overly Permissive Roles

#### Flow
1. Security review identifies overly permissive policies
2. Policies are adjusted to follow least privilege principle
3. Function is tested to ensure it still works with restricted permissions

#### Expected Outcome
- Access is appropriately restricted
- Function continues to operate correctly with minimal permissions

## 10. Next Steps

1. Create IAM roles and policies via AWS CLI or SAM
2. Test permissions using AWS Policy Simulator
3. Associate roles with Lambda functions
4. Verify access patterns with test invocations
5. Document the actual implementation 

## 11. Manual Deployment Status (AWS CLI)

This section tracks the progress of manually deploying the IAM resources described in this document using the AWS CLI for the `test` environment.

*   **Environment:** `test`

**Status for StagingLambda:**
*   [x] Create IAM Policy (`StagingLambdaPolicy-test`) - *Updated with GetItem & corrected Secrets Manager Resource*
    *   Policy Definition File: `staging-lambda-policy.json`
    *   Policy ARN: `arn:aws:iam::337909745089:policy/StagingLambdaPolicy-test`
*   [x] Create IAM Role (`StagingLambdaRole-test`)
    *   Trust Policy File: `staging-lambda-trust-policy.json` (Allows `lambda.amazonaws.com`)
    *   Role ARN: `arn:aws:iam::337909745089:role/StagingLambdaRole-test`
*   [x] Attach Policy to Role (`StagingLambdaPolicy-test` to `StagingLambdaRole-test`)
*   [x] Add Lambda Resource Policy (Allow API Gateway Invoke) - *Added via `aws lambda add-permission`*

**Status for MessagingLambda:**
*   [ ] Create IAM Policy (`MessagingLambdaPolicy-test`)
*   [ ] Create IAM Role (`MessagingLambdaRole-test`)
*   [ ] Attach Policy to Role (`MessagingLambdaPolicy-test` to `MessagingLambdaRole-test`) 