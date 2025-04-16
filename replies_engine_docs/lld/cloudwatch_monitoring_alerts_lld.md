# CloudWatch Monitoring and Alerts - Low-Level Design

## 1. Purpose and Responsibilities

CloudWatch Monitoring and Alerts in the replies-engine microservice provide comprehensive observability and operational awareness. The main responsibilities include:

- Collecting and storing logs from all components
- Monitoring key metrics across the service
- Detecting anomalies and failures
- Alerting appropriate stakeholders when issues occur
- Providing dashboards for operational visibility
- Supporting troubleshooting and performance analysis

This LLD focuses on the CloudWatch configuration required for effective monitoring of the replies-engine microservice.

## 2. Log Groups

### 2.1 Lambda Function Log Groups

```yaml
# IncomingWebhookHandler Log Group
IncomingWebhookHandlerLogGroup:
  Type: AWS::Logs::LogGroup
  Properties:
    LogGroupName: !Sub '/aws/lambda/${ProjectPrefix}-incoming-webhook-handler-${EnvironmentName}'
    RetentionInDays: 14

# ReplyProcessorLambda Log Group
ReplyProcessorLambdaLogGroup:
  Type: AWS::Logs::LogGroup
  Properties:
    LogGroupName: !Sub '/aws/lambda/${ProjectPrefix}-reply-processor-${EnvironmentName}'
    RetentionInDays: 14
```

### 2.2 API Gateway Log Group

```yaml
# API Gateway Access Logs
ApiGatewayLogGroup:
  Type: AWS::Logs::LogGroup
  Properties:
    LogGroupName: !Sub '/aws/apigateway/${ProjectPrefix}-api-${EnvironmentName}'
    RetentionInDays: 14
```

## 3. Metric Filters

### 3.1 Error Rate Metric Filters

```yaml
# IncomingWebhookHandler Error Metric Filter
IncomingWebhookHandlerErrorMetricFilter:
  Type: AWS::Logs::MetricFilter
  Properties:
    LogGroupName: !Ref IncomingWebhookHandlerLogGroup
    FilterPattern: '?ERROR ?Error ?error'
    MetricTransformations:
      - MetricName: IncomingWebhookHandlerErrors
        MetricNamespace: !Sub '${ProjectPrefix}/${EnvironmentName}'
        MetricValue: '1'
        DefaultValue: 0
        Unit: Count

# ReplyProcessorLambda Error Metric Filter
ReplyProcessorLambdaErrorMetricFilter:
  Type: AWS::Logs::MetricFilter
  Properties:
    LogGroupName: !Ref ReplyProcessorLambdaLogGroup
    FilterPattern: '?ERROR ?Error ?error'
    MetricTransformations:
      - MetricName: ReplyProcessorLambdaErrors
        MetricNamespace: !Sub '${ProjectPrefix}/${EnvironmentName}'
        MetricValue: '1'
        DefaultValue: 0
        Unit: Count
```

### 3.2 Critical Failure Metric Filters

```yaml
# DynamoDB Final Update Failure Metric Filter
DynamoDBFinalUpdateFailureMetricFilter:
  Type: AWS::Logs::MetricFilter
  Properties:
    LogGroupName: !Ref ReplyProcessorLambdaLogGroup
    FilterPattern: 'CRITICAL Final DynamoDB update failed'
    MetricTransformations:
      - MetricName: DynamoDBFinalUpdateFailures
        MetricNamespace: !Sub '${ProjectPrefix}/${EnvironmentName}'
        MetricValue: '1'
        DefaultValue: 0
        Unit: Count

# Missing Conversation Metric Filter
MissingConversationMetricFilter:
  Type: AWS::Logs::MetricFilter
  Properties:
    LogGroupName: !Ref IncomingWebhookHandlerLogGroup
    FilterPattern: 'WARNING No conversation found for'
    MetricTransformations:
      - MetricName: MissingConversationCount
        MetricNamespace: !Sub '${ProjectPrefix}/${EnvironmentName}'
        MetricValue: '1'
        DefaultValue: 0
        Unit: Count

# Invalid Signature Metric Filter
InvalidSignatureMetricFilter:
  Type: AWS::Logs::MetricFilter
  Properties:
    LogGroupName: !Ref IncomingWebhookHandlerLogGroup
    FilterPattern: 'WARNING Invalid Twilio signature'
    MetricTransformations:
      - MetricName: InvalidSignatureCount
        MetricNamespace: !Sub '${ProjectPrefix}/${EnvironmentName}'
        MetricValue: '1'
        DefaultValue: 0
        Unit: Count
```

## 4. CloudWatch Alarms

### 4.1 Lambda Error Rate Alarms

```yaml
# IncomingWebhookHandler Error Rate Alarm
IncomingWebhookHandlerErrorAlarm:
  Type: AWS::CloudWatch::Alarm
  Properties:
    AlarmName: !Sub '${ProjectPrefix}-IncomingWebhookHandler-ErrorRate-${EnvironmentName}'
    AlarmDescription: 'Alarm when IncomingWebhookHandler error rate exceeds threshold'
    Namespace: !Sub '${ProjectPrefix}/${EnvironmentName}'
    MetricName: IncomingWebhookHandlerErrors
    Statistic: Sum
    Period: 60
    EvaluationPeriods: 5
    Threshold: 5
    ComparisonOperator: GreaterThanThreshold
    TreatMissingData: notBreaching
    AlarmActions:
      - !Ref AlertsTopicArn

# ReplyProcessorLambda Error Rate Alarm
ReplyProcessorLambdaErrorAlarm:
  Type: AWS::CloudWatch::Alarm
  Properties:
    AlarmName: !Sub '${ProjectPrefix}-ReplyProcessorLambda-ErrorRate-${EnvironmentName}'
    AlarmDescription: 'Alarm when ReplyProcessorLambda error rate exceeds threshold'
    Namespace: !Sub '${ProjectPrefix}/${EnvironmentName}'
    MetricName: ReplyProcessorLambdaErrors
    Statistic: Sum
    Period: 60
    EvaluationPeriods: 5
    Threshold: 5
    ComparisonOperator: GreaterThanThreshold
    TreatMissingData: notBreaching
    AlarmActions:
      - !Ref AlertsTopicArn
```

### 4.2 Lambda Invocation Alarms

```yaml
# IncomingWebhookHandler Invocation Alarm
IncomingWebhookHandlerInvocationAlarm:
  Type: AWS::CloudWatch::Alarm
  Properties:
    AlarmName: !Sub '${ProjectPrefix}-IncomingWebhookHandler-Invocations-${EnvironmentName}'
    AlarmDescription: 'Alarm when IncomingWebhookHandler has no invocations for 15 minutes'
    Namespace: 'AWS/Lambda'
    MetricName: Invocations
    Dimensions:
      - Name: FunctionName
        Value: !Sub '${ProjectPrefix}-incoming-webhook-handler-${EnvironmentName}'
    Statistic: Sum
    Period: 900
    EvaluationPeriods: 1
    Threshold: 1
    ComparisonOperator: LessThanThreshold
    TreatMissingData: breaching
    AlarmActions:
      - !Ref AlertsTopicArn

# ReplyProcessorLambda Invocation Alarm (only for production)
ReplyProcessorLambdaInvocationAlarm:
  Type: AWS::CloudWatch::Alarm
  Condition: IsProduction
  Properties:
    AlarmName: !Sub '${ProjectPrefix}-ReplyProcessorLambda-Invocations-${EnvironmentName}'
    AlarmDescription: 'Alarm when ReplyProcessorLambda has no invocations for 30 minutes'
    Namespace: 'AWS/Lambda'
    MetricName: Invocations
    Dimensions:
      - Name: FunctionName
        Value: !Sub '${ProjectPrefix}-reply-processor-${EnvironmentName}'
    Statistic: Sum
    Period: 1800
    EvaluationPeriods: 1
    Threshold: 1
    ComparisonOperator: LessThanThreshold
    TreatMissingData: breaching
    AlarmActions:
      - !Ref AlertsTopicArn
```

### 4.3 Critical Failure Alarms

```yaml
# DynamoDB Final Update Failure Alarm
DynamoDBFinalUpdateFailureAlarm:
  Type: AWS::CloudWatch::Alarm
  Properties:
    AlarmName: !Sub '${ProjectPrefix}-DynamoDB-FinalUpdateFailure-${EnvironmentName}'
    AlarmDescription: 'Alarm when final DynamoDB update fails after message is sent'
    Namespace: !Sub '${ProjectPrefix}/${EnvironmentName}'
    MetricName: DynamoDBFinalUpdateFailures
    Statistic: Sum
    Period: 60
    EvaluationPeriods: 1
    Threshold: 0
    ComparisonOperator: GreaterThanThreshold
    TreatMissingData: notBreaching
    AlarmActions:
      - !Ref AlertsTopicArn
```

### 4.4 SQS Queue Alarms

```yaml
# WhatsApp Replies DLQ Not Empty Alarm
WhatsAppRepliesDLQNotEmptyAlarm:
  Type: AWS::CloudWatch::Alarm
  Properties:
    AlarmName: !Sub '${ProjectPrefix}-WhatsAppRepliesDLQ-NotEmpty-${EnvironmentName}'
    AlarmDescription: 'Alarm when there are messages in the WhatsApp Replies Dead Letter Queue'
    Namespace: 'AWS/SQS'
    MetricName: ApproximateNumberOfMessagesVisible
    Dimensions:
      - Name: QueueName
        Value: !GetAtt WhatsAppRepliesDLQ.QueueName
    Statistic: Sum
    Period: 60
    EvaluationPeriods: 1
    Threshold: 0
    ComparisonOperator: GreaterThanThreshold
    TreatMissingData: notBreaching
    AlarmActions:
      - !Ref AlertsTopicArn

# Human Handoff DLQ Not Empty Alarm
HumanHandoffDLQNotEmptyAlarm:
  Type: AWS::CloudWatch::Alarm
  Properties:
    AlarmName: !Sub '${ProjectPrefix}-HumanHandoffDLQ-NotEmpty-${EnvironmentName}'
    AlarmDescription: 'Alarm when there are messages in the Human Handoff Dead Letter Queue'
    Namespace: 'AWS/SQS'
    MetricName: ApproximateNumberOfMessagesVisible
    Dimensions:
      - Name: QueueName
        Value: !GetAtt HumanHandoffDLQ.QueueName
    Statistic: Sum
    Period: 60
    EvaluationPeriods: 1
    Threshold: 0
    ComparisonOperator: GreaterThanThreshold
    TreatMissingData: notBreaching
    AlarmActions:
      - !Ref AlertsTopicArn
```

## 5. SNS Topics for Alerting

```yaml
# Alerts Topic
AlertsTopic:
  Type: AWS::SNS::Topic
  Properties:
    TopicName: !Sub '${ProjectPrefix}-alerts-${EnvironmentName}'
    DisplayName: !Sub '${ProjectPrefix} Alerts (${EnvironmentName})'

# Email Subscription
AlertsEmailSubscription:
  Type: AWS::SNS::Subscription
  Properties:
    TopicArn: !Ref AlertsTopic
    Protocol: email
    Endpoint: !Ref AlertsEmail
```

## 6. CloudWatch Dashboard

```yaml
# Main Monitoring Dashboard
MonitoringDashboard:
  Type: AWS::CloudWatch::Dashboard
  Properties:
    DashboardName: !Sub '${ProjectPrefix}-${EnvironmentName}'
    DashboardBody: !Sub |
      {
        "widgets": [
          {
            "type": "metric",
            "x": 0,
            "y": 0,
            "width": 12,
            "height": 6,
            "properties": {
              "metrics": [
                [ "AWS/Lambda", "Invocations", "FunctionName", "${ProjectPrefix}-incoming-webhook-handler-${EnvironmentName}", { "stat": "Sum", "period": 300 } ],
                [ "AWS/Lambda", "Invocations", "FunctionName", "${ProjectPrefix}-reply-processor-${EnvironmentName}", { "stat": "Sum", "period": 300 } ]
              ],
              "view": "timeSeries",
              "stacked": false,
              "region": "${AWS::Region}",
              "title": "Lambda Invocations",
              "period": 300
            }
          },
          {
            "type": "metric",
            "x": 12,
            "y": 0,
            "width": 12,
            "height": 6,
            "properties": {
              "metrics": [
                [ "AWS/Lambda", "Errors", "FunctionName", "${ProjectPrefix}-incoming-webhook-handler-${EnvironmentName}", { "stat": "Sum", "period": 300 } ],
                [ "AWS/Lambda", "Errors", "FunctionName", "${ProjectPrefix}-reply-processor-${EnvironmentName}", { "stat": "Sum", "period": 300 } ]
              ],
              "view": "timeSeries",
              "stacked": false,
              "region": "${AWS::Region}",
              "title": "Lambda Errors",
              "period": 300
            }
          },
          {
            "type": "metric",
            "x": 0,
            "y": 6,
            "width": 12,
            "height": 6,
            "properties": {
              "metrics": [
                [ "AWS/Lambda", "Duration", "FunctionName", "${ProjectPrefix}-incoming-webhook-handler-${EnvironmentName}", { "stat": "Average", "period": 300 } ],
                [ "AWS/Lambda", "Duration", "FunctionName", "${ProjectPrefix}-reply-processor-${EnvironmentName}", { "stat": "Average", "period": 300 } ]
              ],
              "view": "timeSeries",
              "stacked": false,
              "region": "${AWS::Region}",
              "title": "Lambda Duration",
              "period": 300
            }
          },
          {
            "type": "metric",
            "x": 12,
            "y": 6,
            "width": 12,
            "height": 6,
            "properties": {
              "metrics": [
                [ "AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", "${ProjectPrefix}-whatsapp-replies-queue-${EnvironmentName}" ],
                [ "AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", "${ProjectPrefix}-human-handoff-queue-${EnvironmentName}" ],
                [ "AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", "${ProjectPrefix}-whatsapp-replies-dlq-${EnvironmentName}" ],
                [ "AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", "${ProjectPrefix}-human-handoff-dlq-${EnvironmentName}" ]
              ],
              "view": "timeSeries",
              "stacked": false,
              "region": "${AWS::Region}",
              "title": "SQS Queue Depth",
              "period": 300
            }
          },
          {
            "type": "metric",
            "x": 0,
            "y": 12,
            "width": 24,
            "height": 6,
            "properties": {
              "metrics": [
                [ "${ProjectPrefix}/${EnvironmentName}", "DynamoDBFinalUpdateFailures", { "stat": "Sum", "period": 300 } ],
                [ "${ProjectPrefix}/${EnvironmentName}", "MissingConversationCount", { "stat": "Sum", "period": 300 } ],
                [ "${ProjectPrefix}/${EnvironmentName}", "InvalidSignatureCount", { "stat": "Sum", "period": 300 } ]
              ],
              "view": "timeSeries",
              "stacked": false,
              "region": "${AWS::Region}",
              "title": "Custom Metrics",
              "period": 300
            }
          }
        ]
      }
```

## 7. API Gateway Access Logging

```yaml
# API Gateway Stage with Access Logging
ApiGatewayStage:
  Type: AWS::ApiGateway::Stage
  Properties:
    StageName: !Ref EnvironmentName
    RestApiId: !Ref ApiGateway
    DeploymentId: !Ref ApiGatewayDeployment
    AccessLogSetting:
      DestinationArn: !GetAtt ApiGatewayLogGroup.Arn
      Format: '$context.identity.sourceIp $context.identity.caller $context.identity.user [$context.requestTime] "$context.httpMethod $context.resourcePath $context.protocol" $context.status $context.responseLength $context.requestId'
    MethodSettings:
      - ResourcePath: '/*'
        HttpMethod: '*'
        MetricsEnabled: true
        DataTraceEnabled: true
        LoggingLevel: INFO
```

## 8. Lambda Logging Configuration

### 8.1 Environment Variables

For each Lambda function, set the following environment variables:

```yaml
Environment:
  Variables:
    LOG_LEVEL: !Ref LogLevel  # Parameter with default value of INFO
```

### 8.2 Lambda Logging Code Structure

Each Lambda function should implement structured logging:

```python
import json
import logging
import structlog
import os

def setup_logger(context):
    """
    Set up structured logger for Lambda function.
    
    Args:
        context: AWS Lambda context
        
    Returns:
        Logger: Configured logger instance
    """
    # Get log level from environment variable
    log_level = os.environ.get('LOG_LEVEL', 'INFO')
    
    # Configure structlog
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer()
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    
    # Create logger with AWS Lambda request ID
    logger = structlog.get_logger()
    logger = logger.bind(
        aws_request_id=context.aws_request_id,
        function_name=context.function_name,
        function_version=context.function_version
    )
    
    return logger
```

## 9. Monitoring Key Metrics

### 9.1 Lambda Metrics

| Metric | Description | Threshold | Alarm |
|--------|-------------|-----------|-------|
| Errors | Number of Lambda invocation errors | >5 in 5 minutes | Warning |
| Throttles | Number of Lambda throttling events | >0 in 5 minutes | Warning |
| Duration | Lambda execution time | >5000ms (avg) | Warning |
| ConcurrentExecutions | Number of concurrent executions | >10 | Monitoring only |
| Invocations | Number of Lambda invocations | 0 in 15/30 minutes | Warning |

### 9.2 SQS Metrics

| Metric | Description | Threshold | Alarm |
|--------|-------------|-----------|-------|
| ApproximateNumberOfMessagesVisible | Message backlog | >100 for 5 minutes | Warning |
| ApproximateAgeOfOldestMessage | Message age | >1 hour | Warning |
| NumberOfMessagesReceived | Message arrival rate | N/A | Monitoring only |
| NumberOfMessagesSent | Message dispatch rate | N/A | Monitoring only |
| ApproximateNumberOfMessagesVisible (DLQ) | Messages in DLQ | >0 | Critical |

### 9.3 API Gateway Metrics

| Metric | Description | Threshold | Alarm |
|--------|-------------|-----------|-------|
| 4XXError | Client errors | >10 in 5 minutes | Warning |
| 5XXError | Server errors | >0 in 5 minutes | Warning |
| Latency | API response time | >1000ms (p95) | Warning |
| Count | Request count | 0 in 30 minutes (prod) | Warning |

### 9.4 Custom Metrics

| Metric | Description | Threshold | Alarm |
|--------|-------------|-----------|-------|
| DynamoDBFinalUpdateFailures | Failed DynamoDB updates | >0 | Critical |
| MissingConversationCount | Missing conversation lookups | >10 in 5 minutes | Warning |
| InvalidSignatureCount | Invalid Twilio signatures | >5 in 5 minutes | Warning |

## 10. Implementation and Testing Strategy

### 10.1 Manual Implementation Steps

```bash
# Create Log Groups
aws logs create-log-group \
  --log-group-name /aws/lambda/ai-multi-comms-incoming-webhook-handler-dev \
  --retention-in-days 14

aws logs create-log-group \
  --log-group-name /aws/lambda/ai-multi-comms-reply-processor-dev \
  --retention-in-days 14

aws logs create-log-group \
  --log-group-name /aws/apigateway/ai-multi-comms-api-dev \
  --retention-in-days 14

# Create Metric Filters
aws logs put-metric-filter \
  --log-group-name /aws/lambda/ai-multi-comms-incoming-webhook-handler-dev \
  --filter-name IncomingWebhookHandlerErrors \
  --filter-pattern "?ERROR ?Error ?error" \
  --metric-transformations \
    metricName=IncomingWebhookHandlerErrors,metricNamespace=ai-multi-comms/dev,metricValue=1,defaultValue=0

# Create SNS Topic
aws sns create-topic \
  --name ai-multi-comms-alerts-dev

# Create CloudWatch Alarms
aws cloudwatch put-metric-alarm \
  --alarm-name ai-multi-comms-IncomingWebhookHandler-ErrorRate-dev \
  --alarm-description "Alarm when IncomingWebhookHandler error rate exceeds threshold" \
  --metric-name IncomingWebhookHandlerErrors \
  --namespace ai-multi-comms/dev \
  --statistic Sum \
  --period 60 \
  --evaluation-periods 5 \
  --threshold 5 \
  --comparison-operator GreaterThanThreshold \
  --alarm-actions arn:aws:sns:us-east-1:123456789012:ai-multi-comms-alerts-dev

# Create Dashboard
aws cloudwatch put-dashboard \
  --dashboard-name ai-multi-comms-dev \
  --dashboard-body file://dashboard-body.json
```

### 10.2 Testing Approach

#### Log Generation Testing

- Generate test logs by invoking Lambda functions
- Verify logs appear in the correct log groups
- Check that the log format is correct and all necessary fields are included

#### Metric Filter Testing

- Generate test logs with specific patterns (errors, warnings)
- Verify that metric filters correctly count the occurrences
- Validate metrics appear in CloudWatch with the right namespace

#### Alarm Testing

- Manually trigger alarm conditions
- Verify that alarms transition to ALARM state
- Confirm that notifications are delivered via SNS

#### Dashboard Testing

- Ensure dashboard loads correctly
- Check that all widgets display the expected metrics
- Verify that metrics update in near real-time

## 11. Security Considerations

### 11.1 Log Data Protection

- Ensure sensitive data is not logged (PII, credentials, tokens)
- Implement log field masking for potentially sensitive fields
- Maintain appropriate retention periods based on compliance requirements

### 11.2 Access Control

- Restrict access to CloudWatch data using IAM policies
- Limit access to dashboards and alarms to authorized personnel
- Consider implementing CloudWatch cross-account sharing for central monitoring

### 11.3 Alert Management

- Implement alert routing to appropriate teams
- Consider alert fatigue and set thresholds appropriately
- Establish escalation procedures for critical alerts

## 12. Happy Path Analysis

### 12.1 Normal Operation Monitoring

#### Preconditions
- All CloudWatch resources are configured correctly
- Application is functioning normally

#### Flow
1. Lambda functions execute and emit logs to CloudWatch
2. Metric filters extract relevant metrics from logs
3. Metrics appear on dashboards in near real-time
4. All alarms remain in OK state

#### Expected Outcome
- Operators have visibility into system performance
- Dashboards show normal operation patterns
- No alarm notifications are triggered

### 12.2 Alert Scenario

#### Preconditions
- Monitoring is configured correctly
- An issue occurs (e.g., DynamoDB update failure)

#### Flow
1. Lambda logs an error or specific error pattern
2. Metric filter detects the pattern and increments the metric
3. Alarm evaluates the metric and transitions to ALARM state
4. SNS notification is sent to subscribers
5. Operations team investigates and resolves the issue
6. Alarm returns to OK state

#### Expected Outcome
- Issue is detected quickly
- Appropriate personnel are notified
- Issue is resolved before significant impact

## 13. Unhappy Path Analysis

### 13.1 Missing Logs

#### Flow
1. Lambda function fails to emit logs (or logs at a lower level)
2. Metric filters don't have data to process
3. Metrics show no activity
4. Alarm for missing invocations may trigger

#### Expected Outcome
- Missing data is detected through invocation alarms
- Operations team investigates logging configuration
- Logging is restored to proper level

### 13.2 Metric Filter Mismatch

#### Flow
1. Error format in logs changes (e.g., new error pattern)
2. Metric filter no longer matches the error pattern
3. Errors occur but metrics don't increment
4. Issues may go undetected

#### Expected Outcome
- Regular log reviews catch the mismatch
- Metric filters are updated to match new patterns
- Comprehensive error patterns are implemented

## 14. Next Steps

1. Create CloudWatch log groups via AWS CLI
2. Configure metric filters for critical patterns
3. Create SNS topic and subscribe operations team
4. Set up CloudWatch alarms for key metrics
5. Create dashboard for operational visibility
6. Test the monitoring configuration
7. Document alerting and incident response procedures 