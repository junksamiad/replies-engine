import pytest
import json
from unittest.mock import patch, MagicMock
from botocore.exceptions import ClientError

# Use the correct absolute import path based on project structure
from src.staging_lambda.lambda_pkg.services import sqs_service

# Define constants used within the module for patching/verification
MOCK_HANDOFF_URL = "mock_handoff_url_in_tests"
MOCK_BATCH_WINDOW = 15 # Use a distinct value for testing

# --- Fixtures ---

@pytest.fixture
def mock_sqs_client():
    """Mocks the boto3 SQS client AND patches module constants."""
    # Patch the module-level constants first
    with patch('src.staging_lambda.lambda_pkg.services.sqs_service.HANDOFF_QUEUE_URL', MOCK_HANDOFF_URL), \
         patch('src.staging_lambda.lambda_pkg.services.sqs_service.BATCH_WINDOW_SECONDS', MOCK_BATCH_WINDOW):
        # Now patch the client that the functions will use
        with patch('src.staging_lambda.lambda_pkg.services.sqs_service.boto3.client') as mock_boto_client:
            mock_client = MagicMock()
            # Configure default success behaviour for send_message
            mock_client.send_message.return_value = {'MessageId': 'mock-message-id-from-fixture'}
            mock_boto_client.return_value = mock_client

            # Reload the service module *after* patching boto3.client
            import importlib
            importlib.reload(sqs_service)

            yield mock_client # Yield the mock *client* instance

            # Optional: Reload again after tests?
            # importlib.reload(sqs_service)

@pytest.fixture
def base_context():
    """Provides a base context object."""
    return {
        'conversation_id': 'conv_sqs_123',
        'channel_type': 'whatsapp',
        'from': 'whatsapp:+1112223333',
        'to': 'whatsapp:+4445556666',
        'message_sid': 'SMxxx',
        'body': 'Test Body'
        # Other fields might be present
    }

# --- Test Cases ---

def test_send_to_channel_queue_success(mock_sqs_client, base_context):
    """Test sending a trigger message to a channel queue."""
    target_url = "mock_channel_queue_url"
    context = base_context
    mock_sqs_client.send_message.return_value = {'MessageId': 'test-msg-id'}

    result = sqs_service.send_message_to_queue(target_url, context)

    assert result == 'SUCCESS'
    expected_body = json.dumps({
        "conversation_id": "conv_sqs_123",
        "primary_channel": "+1112223333" # Prefix stripped
    })
    mock_sqs_client.send_message.assert_called_once_with(
        QueueUrl=target_url,
        MessageBody=expected_body,
        DelaySeconds=10 # Use actual default from source code
    )

def test_send_to_channel_queue_email_success(mock_sqs_client, base_context):
    """Test sending a trigger message for email channel."""
    target_url = "mock_email_channel_url"
    context = base_context.copy()
    context['channel_type'] = 'email'
    context['from_address'] = 'sender@example.com'
    del context['from']
    del context['to']

    mock_sqs_client.send_message.return_value = {'MessageId': 'test-email-msg-id'}

    result = sqs_service.send_message_to_queue(target_url, context)

    assert result == 'SUCCESS'
    expected_body = json.dumps({
        "conversation_id": "conv_sqs_123",
        "primary_channel": "sender@example.com"
    })
    mock_sqs_client.send_message.assert_called_once_with(
        QueueUrl=target_url,
        MessageBody=expected_body,
        DelaySeconds=10 # Use actual default from source code
    )

# def test_send_to_handoff_queue_success(mock_sqs_client, base_context):
#     """Test sending the full context to the handoff queue."""
#     target_url = MOCK_HANDOFF_URL # Target the mocked handoff URL
#     context = base_context
#     mock_sqs_client.send_message.return_value = {'MessageId': 'test-handoff-id'}
#
#     result = sqs_service.send_message_to_queue(target_url, context)
#
#     assert result == 'SUCCESS'
#     expected_body = json.dumps(context) # Full context expected
#     mock_sqs_client.send_message.assert_called_once_with(
#         QueueUrl=target_url,
#         MessageBody=expected_body,
#         DelaySeconds=0 # No delay for handoff
#     )

def test_send_invalid_input(mock_sqs_client):
    """Test handling of invalid input arguments."""
    result1 = sqs_service.send_message_to_queue(None, {'conversation_id': 'c1'})
    result2 = sqs_service.send_message_to_queue("some_url", None)
    result3 = sqs_service.send_message_to_queue("some_url", {})

    assert result1 == 'INTERNAL_ERROR'
    assert result2 == 'INTERNAL_ERROR'
    assert result3 == 'INTERNAL_ERROR'
    mock_sqs_client.send_message.assert_not_called()

def test_send_to_channel_missing_primary_channel(mock_sqs_client, base_context):
    """Test error when primary_channel cannot be derived for channel queue message."""
    target_url = "mock_channel_queue_url"
    context = base_context
    del context['from'] # Remove the source field

    result = sqs_service.send_message_to_queue(target_url, context)

    assert result == 'INTERNAL_ERROR'
    mock_sqs_client.send_message.assert_not_called()

# def test_send_to_handoff_unserializable(mock_sqs_client, base_context):
#     """Test context object is not JSON serializable for handoff - EXPECT SUCCESS."""
#     target_url = MOCK_HANDOFF_URL
#     context = base_context
#     context['unserializable'] = object() # Add unserializable object
#
#     # Mock json.dumps to raise TypeError ONLY for this specific test
#     with patch('src.staging_lambda.lambda_pkg.services.sqs_service.json.dumps') as mock_dumps:
#         mock_dumps.side_effect = TypeError("Object of type MagicMock is not JSON serializable")
#         result = sqs_service.send_message_to_queue(target_url, context)
#         # Corrected Assertion: Expect INTERNAL_ERROR when dumps fails
#         assert result == 'INTERNAL_ERROR'
#         mock_dumps.assert_called_once_with(context)
#         mock_sqs_client.send_message.assert_not_called()

@pytest.mark.parametrize(
    "aws_error_code, expected_status",
    [
        ('ServiceUnavailable', 'SQS_TRANSIENT_ERROR'),
        ('InternalFailure', 'SQS_TRANSIENT_ERROR'),
        ('QueueDoesNotExist', 'SQS_CONFIG_ERROR'),
        ('AccessDenied', 'SQS_CONFIG_ERROR'),
        ('InvalidParameterValue', 'SQS_PARAMETER_ERROR'),
        ('InvalidMessageContents', 'SQS_PARAMETER_ERROR'),
        ('SomeOtherSQSError', 'SQS_SEND_ERROR'),
    ]
)
def test_send_client_error(mock_sqs_client, base_context, aws_error_code, expected_status):
    """Test mapping of SQS ClientErrors during send_message."""
    target_url = "mock_channel_queue_url"
    context = base_context

    # Define a function to raise the specific ClientError
    def raise_client_error(*args, **kwargs):
        raise ClientError(
            error_response={'Error': {'Code': aws_error_code, 'Message': 'Test error'}},
            operation_name='SendMessage'
        )

    # Assign the function to side_effect
    mock_sqs_client.send_message.side_effect = raise_client_error

    result = sqs_service.send_message_to_queue(target_url, context)
    assert result == expected_status
    mock_sqs_client.send_message.assert_called_once() # Verify send_message was called

def test_send_unexpected_error(mock_sqs_client, base_context):
    """Test handling of unexpected errors during send_message."""
    target_url = "mock_channel_queue_url"
    context = base_context
    mock_sqs_client.send_message.side_effect = Exception("Something broke")

    result = sqs_service.send_message_to_queue(target_url, context)
    assert result == 'INTERNAL_ERROR' 