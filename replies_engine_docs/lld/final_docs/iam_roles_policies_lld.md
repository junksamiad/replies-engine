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

### 2.1 IncomingWebhookHandler Role

```yaml
IncomingWebhookHandlerRole:
  Type: AWS::IAM::Role
  Properties:
    RoleName: !Sub '${ProjectPrefix}-incoming-webhook-handler-role-${EnvironmentName}'
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
      - PolicyName: !Sub '${ProjectPrefix}-incoming-webhook-handler-policy-${EnvironmentName}'
        PolicyDocument:
          Version: '2012-10-17'
          Statement:
            # DynamoDB Permissions
            - Effect: Allow
              Action:
                - dynamodb:Query
              Resource:
                - !GetAtt ConversationsTable.Arn
                - !Sub '${ConversationsTable.Arn}/index/*'
              
            # SQS Permissions
            - Effect: Allow
              Action:
                - sqs:SendMessage
              Resource:
                - !GetAtt WhatsAppRepliesQueue.Arn
                - !GetAtt HumanHandoffQueue.Arn
                
            # Secrets Manager Permissions
            - Effect: Allow
              Action:
                - secretsmanager:GetSecretValue
              Resource:
                - !Sub 'arn:${AWS::Partition}:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:${ProjectPrefix}/whatsapp-credentials/*/*/twilio-*'
```

### 2.2 ReplyProcessorLambda Role

```yaml
ReplyProcessorLambdaRole:
  Type: AWS::IAM::Role
  Properties:
    RoleName: !Sub '${ProjectPrefix}-reply-processor-role-${EnvironmentName}'
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
      - PolicyName: !Sub '${ProjectPrefix}-reply-processor-policy-${EnvironmentName}'
        PolicyDocument:
          Version: '2012-10-17'
          Statement:
            # DynamoDB Permissions
            - Effect: Allow
              Action:
                - dynamodb:GetItem
                - dynamodb:UpdateItem
              Resource:
                - !GetAtt ConversationsTable.Arn
                
            # SQS Permissions
            - Effect: Allow
              Action:
                - sqs:ReceiveMessage
                - sqs:DeleteMessage
                - sqs:GetQueueAttributes
              Resource:
                - !GetAtt WhatsAppRepliesQueue.Arn
                
            # Secrets Manager Permissions
            - Effect: Allow
              Action:
                - secretsmanager:GetSecretValue
              Resource:
                - !Sub 'arn:${AWS::Partition}:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:${ProjectPrefix}/whatsapp-credentials/*/*/twilio-*'
                - !Sub 'arn:${AWS::Partition}:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:${ProjectPrefix}/openai-api-key/whatsapp-*'
```

## 3. Policy Details and Rationale

### 3.1 IncomingWebhookHandler Lambda Policies

#### 3.1.1 DynamoDB Access

```yaml
- Effect: Allow
  Action:
    - dynamodb:Query
  Resource:
    - !GetAtt ConversationsTable.Arn
    - !Sub '${ConversationsTable.Arn}/index/*'
```

**Rationale:**
- `dynamodb:Query` permission allows the Lambda to look up conversation records by recipient phone number.
- Access to all indexes is required to support querying by different attributes (e.g., recipient_tel).
- No write permissions are granted since this function only needs to read conversation data.

#### 3.1.2 SQS Access

```yaml
- Effect: Allow
  Action:
    - sqs:SendMessage
  Resource:
    - !GetAtt WhatsAppRepliesQueue.Arn
    - !GetAtt HumanHandoffQueue.Arn
```

**Rationale:**
- `sqs:SendMessage` permission allows the Lambda to send messages to both the AI processing queue and the human handoff queue.
- No receive or delete permissions are needed since this function only sends messages.

#### 3.1.3 Secrets Manager Access

```yaml
- Effect: Allow
  Action:
    - secretsmanager:GetSecretValue
  Resource:
    - !Sub 'arn:${AWS::Partition}:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:${ProjectPrefix}/whatsapp-credentials/*/*/twilio-*'
```

**Rationale:**
- Access to Twilio credentials is required for webhook signature validation.
- The resource pattern allows access to Twilio secrets across all companies/projects.
- Limited to only the GetSecretValue action for least privilege.

### 3.2 ReplyProcessorLambda Policies

#### 3.2.1 DynamoDB Access

```yaml
- Effect: Allow
  Action:
    - dynamodb:GetItem
    - dynamodb:UpdateItem
  Resource:
    - !GetAtt ConversationsTable.Arn
```

**Rationale:**
- `dynamodb:GetItem` permission allows the Lambda to retrieve conversation details if needed.
- `dynamodb:UpdateItem` permission allows the Lambda to update the conversation with new messages.
- Access is limited to just the ConversationsTable, not indexes, since this function will access by primary key.

#### 3.2.2 SQS Access

```yaml
- Effect: Allow
  Action:
    - sqs:ReceiveMessage
    - sqs:DeleteMessage
    - sqs:GetQueueAttributes
  Resource:
    - !GetAtt WhatsAppRepliesQueue.Arn
```

**Rationale:**
- `sqs:ReceiveMessage` and `sqs:DeleteMessage` permissions are required for processing messages from the queue.
- `sqs:GetQueueAttributes` allows the Lambda to check queue attributes if needed.
- Access is limited to just the WhatsApp replies queue since this function doesn't interact with the human handoff queue.

#### 3.2.3 Secrets Manager Access

```yaml
- Effect: Allow
  Action:
    - secretsmanager:GetSecretValue
  Resource:
    - !Sub 'arn:${AWS::Partition}:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:${ProjectPrefix}/whatsapp-credentials/*/*/twilio-*'
    - !Sub 'arn:${AWS::Partition}:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:${ProjectPrefix}/openai-api-key/whatsapp-*'
```

**Rationale:**
- Access to Twilio credentials is required for sending WhatsApp messages.
- Access to OpenAI API keys is required for interacting with the OpenAI Assistants API.
- The resource patterns allow access to secrets across all companies/projects.

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
# Create IncomingWebhookHandler Role
aws iam create-role \
  --role-name ai-multi-comms-incoming-webhook-handler-role-dev \
  --assume-role-policy-document file://webhook-handler-trust-policy.json

# Attach AWSLambdaBasicExecutionRole
aws iam attach-role-policy \
  --role-name ai-multi-comms-incoming-webhook-handler-role-dev \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

# Create and attach custom policy
aws iam put-role-policy \
  --role-name ai-multi-comms-incoming-webhook-handler-role-dev \
  --policy-name ai-multi-comms-incoming-webhook-handler-policy-dev \
  --policy-document file://webhook-handler-policy.json

# Similar commands for ReplyProcessorLambda Role
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
  # IncomingWebhookHandler Role
  IncomingWebhookHandlerRole:
    Type: AWS::IAM::Role
    Properties:
      RoleName: !Sub '${ProjectPrefix}-incoming-webhook-handler-role-${EnvironmentName}'
      AssumeRolePolicyDocument: { ... }
      ManagedPolicyArns:
        - !Sub 'arn:${AWS::Partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'
      Policies:
        - PolicyName: !Sub '${ProjectPrefix}-incoming-webhook-handler-policy-${EnvironmentName}'
          PolicyDocument: { ... }

  # ReplyProcessorLambda Role
  ReplyProcessorLambdaRole:
    Type: AWS::IAM::Role
    Properties:
      RoleName: !Sub '${ProjectPrefix}-reply-processor-role-${EnvironmentName}'
      AssumeRolePolicyDocument: { ... }
      ManagedPolicyArns:
        - !Sub 'arn:${AWS::Partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'
      Policies:
        - PolicyName: !Sub '${ProjectPrefix}-reply-processor-policy-${EnvironmentName}'
          PolicyDocument: { ... }

  # Lambda Functions using these roles
  IncomingWebhookHandlerFunction:
    Type: AWS::Serverless::Function
    Properties:
      # ... other properties
      Role: !GetAtt IncomingWebhookHandlerRole.Arn

  ReplyProcessorLambdaFunction:
    Type: AWS::Serverless::Function
    Properties:
      # ... other properties
      Role: !GetAtt ReplyProcessorLambdaRole.Arn
```

## 8. Happy Path Analysis

### 8.1 IncomingWebhookHandler IAM Role

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

### 8.2 ReplyProcessorLambda IAM Role

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

1. Create IAM roles and policies via AWS CLI
2. Test permissions using AWS Policy Simulator
3. Associate roles with Lambda functions
4. Verify access patterns with test invocations
5. Document the actual implementation 