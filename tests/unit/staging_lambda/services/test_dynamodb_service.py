import pytest
from unittest.mock import patch, MagicMock, ANY
from botocore.exceptions import ClientError
import time

# Use the correct absolute import path based on project structure
from src.staging_lambda.lambda_pkg.services import dynamodb_service

# Define expected table names (match defaults or mock env vars if needed)
CONVERSATIONS_TABLE_NAME = 'ai-multi-comms-conversations-dev'
STAGE_TABLE_NAME = 'conversations-stage-test'
LOCK_TABLE_NAME = 'conversations-trigger-lock-test'

# --- Fixtures ---

@pytest.fixture
def mock_dynamodb_resource():
    """Mocks the boto3 DynamoDB resource and its Table objects."""
    mock_resource = MagicMock()
    mock_conversations_table = MagicMock(name="ConversationsTable")
    mock_stage_table = MagicMock(name="StageTable")
    mock_lock_table = MagicMock(name="LockTable")

    # Configure the resource mock to return specific table mocks based on name
    def table_side_effect(table_name):
        if table_name == CONVERSATIONS_TABLE_NAME:
            return mock_conversations_table
        elif table_name == STAGE_TABLE_NAME:
            return mock_stage_table
        elif table_name == LOCK_TABLE_NAME:
            return mock_lock_table
        else:
            raise ValueError(f"Unexpected table name: {table_name}")

    mock_resource.Table.side_effect = table_side_effect

    # Patch boto3.resource to return our mock resource
    with patch('src.staging_lambda.lambda_pkg.services.dynamodb_service.boto3.resource') as mock_boto_resource:
        mock_boto_resource.return_value = mock_resource

        # Reload the service module *after* patching boto3.resource
        # This ensures the module-level table variables use the mock resource
        import importlib
        importlib.reload(dynamodb_service)

        # Yield the individual table mocks for convenience in tests
        yield {
            "conversations": mock_conversations_table,
            "stage": mock_stage_table,
            "lock": mock_lock_table
        }
        # Optional: Reload again after tests to potentially restore original state
        # importlib.reload(dynamodb_service)

# --- get_credential_ref_for_validation Tests ---

@pytest.mark.parametrize("channel_type, from_id, to_id, gsi_pk, gsi_sk, credential_key, expected_cred_ref", [
    ('whatsapp', 'whatsapp:+111', 'whatsapp:+999', '+999', '+111', 'whatsapp_credentials_id', 'wa_secret_123'),
    ('sms', '+111', '+999', '+999', '+111', 'sms_credentials_id', 'sms_secret_456'),
    ('email', 'user@a.com', 'support@b.com', 'support@b.com', 'user@a.com', 'email_credentials_id', 'em_secret_789'),
])
def test_get_credential_ref_success(mock_dynamodb_resource, channel_type, from_id, to_id, gsi_pk, gsi_sk, credential_key, expected_cred_ref):
    """Test successful GSI query and credential extraction."""
    mock_conversations_table = mock_dynamodb_resource['conversations']
    mock_response = {
        'Items': [{
            'conversation_id': 'conv_abc',
            'channel_config': {credential_key: expected_cred_ref}
        }]
    }
    mock_conversations_table.query.return_value = mock_response

    result = dynamodb_service.get_credential_ref_for_validation(channel_type, from_id, to_id)

    assert result == {
        'status': 'FOUND',
        'credential_ref': expected_cred_ref,
        'conversation_id': 'conv_abc'
    }
    # Verify the query arguments
    config = dynamodb_service.GSI_CONFIG[channel_type]
    mock_conversations_table.query.assert_called_once_with(
        IndexName=config['index_name'],
        KeyConditionExpression=f'{config["pk_name"]} = :pk AND {config["sk_name"]} = :sk',
        ExpressionAttributeValues={':pk': gsi_pk, ':sk': gsi_sk},
        ProjectionExpression='channel_config, conversation_id',
        Limit=1
    )

def test_get_credential_ref_not_found(mock_dynamodb_resource):
    """Test GSI query when no items are found."""
    mock_conversations_table = mock_dynamodb_resource['conversations']
    mock_conversations_table.query.return_value = {'Items': []}
    result = dynamodb_service.get_credential_ref_for_validation('whatsapp', 'whatsapp:+111', 'whatsapp:+999')
    assert result == {'status': 'NOT_FOUND'}

def test_get_credential_ref_missing_config_key(mock_dynamodb_resource):
    """Test GSI query when item found but credential key is missing in channel_config."""
    mock_conversations_table = mock_dynamodb_resource['conversations']
    mock_conversations_table.query.return_value = {
        'Items': [{
            'conversation_id': 'conv_abc',
            'channel_config': {'other_key': 'value'} # Missing whatsapp_credentials_id
        }]
    }
    result = dynamodb_service.get_credential_ref_for_validation('whatsapp', 'whatsapp:+111', 'whatsapp:+999')
    assert result == {'status': 'MISSING_CREDENTIAL_CONFIG', 'conversation_id': 'conv_abc'}

def test_get_credential_ref_unsupported_channel(mock_dynamodb_resource):
    """Test handling of unsupported channel type."""
    result = dynamodb_service.get_credential_ref_for_validation('telegram', 'id1', 'id2')
    assert result == {'status': 'UNSUPPORTED_CHANNEL'}
    mock_dynamodb_resource['conversations'].query.assert_not_called()

@pytest.mark.parametrize(
    "aws_error_code, expected_status",
    [
        ('ProvisionedThroughputExceededException', 'DB_TRANSIENT_ERROR'),
        ('InternalServerError', 'DB_TRANSIENT_ERROR'),
        ('ResourceNotFoundException', 'DB_CONFIG_ERROR'),
        ('AccessDeniedException', 'DB_CONFIG_ERROR'),
        ('ValidationException', 'DB_VALIDATION_ERROR'),
        ('SomeOtherDynamoDBError', 'DB_QUERY_ERROR'),
    ]
)
def test_get_credential_ref_client_error(mock_dynamodb_resource, aws_error_code, expected_status):
    """Test mapping of ClientErrors during GSI query."""
    mock_conversations_table = mock_dynamodb_resource['conversations']
    mock_conversations_table.query.side_effect = ClientError(
        error_response={'Error': {'Code': aws_error_code, 'Message': 'Test error'}},
        operation_name='Query'
    )
    result = dynamodb_service.get_credential_ref_for_validation('whatsapp', 'whatsapp:+111', 'whatsapp:+999')
    assert result == {'status': expected_status}

def test_get_credential_ref_unexpected_error(mock_dynamodb_resource):
    """Test handling of unexpected errors during GSI query."""
    mock_conversations_table = mock_dynamodb_resource['conversations']
    mock_conversations_table.query.side_effect = Exception("Something broke")
    result = dynamodb_service.get_credential_ref_for_validation('whatsapp', 'whatsapp:+111', 'whatsapp:+999')
    assert result == {'status': 'INTERNAL_ERROR'}

# --- get_full_conversation Tests ---

def test_get_full_conversation_success(mock_dynamodb_resource):
    """Test successful retrieval of a full conversation item."""
    mock_conversations_table = mock_dynamodb_resource['conversations']
    expected_item = {'primary_channel': 'user1', 'conversation_id': 'conv_abc', 'data': 'test'}
    mock_conversations_table.get_item.return_value = {'Item': expected_item}

    result = dynamodb_service.get_full_conversation('user1', 'conv_abc')
    assert result == {'status': 'FOUND', 'data': expected_item}
    mock_conversations_table.get_item.assert_called_once_with(Key={'primary_channel': 'user1', 'conversation_id': 'conv_abc'})

def test_get_full_conversation_not_found(mock_dynamodb_resource):
    """Test get_item when the item is not found."""
    mock_conversations_table = mock_dynamodb_resource['conversations']
    mock_conversations_table.get_item.return_value = {} # No 'Item' key
    result = dynamodb_service.get_full_conversation('user1', 'conv_abc')
    assert result == {'status': 'NOT_FOUND'}

def test_get_full_conversation_empty_keys(mock_dynamodb_resource):
    """Test handling when called with empty keys."""
    result_pk = dynamodb_service.get_full_conversation('', 'conv_abc')
    result_sk = dynamodb_service.get_full_conversation('user1', '')
    assert result_pk == {'status': 'INTERNAL_ERROR'}
    assert result_sk == {'status': 'INTERNAL_ERROR'}
    mock_dynamodb_resource['conversations'].get_item.assert_not_called()

@pytest.mark.parametrize(
    "aws_error_code, expected_status",
    [
        ('ProvisionedThroughputExceededException', 'DB_TRANSIENT_ERROR'),
        ('ResourceNotFoundException', 'DB_CONFIG_ERROR'),
        ('ValidationException', 'DB_VALIDATION_ERROR'),
        ('SomeOtherDynamoDBError', 'DB_GET_ITEM_ERROR'),
    ]
)
def test_get_full_conversation_client_error(mock_dynamodb_resource, aws_error_code, expected_status):
    """Test mapping of ClientErrors during get_item."""
    mock_conversations_table = mock_dynamodb_resource['conversations']
    mock_conversations_table.get_item.side_effect = ClientError(
        error_response={'Error': {'Code': aws_error_code, 'Message': 'Test error'}},
        operation_name='GetItem'
    )
    result = dynamodb_service.get_full_conversation('user1', 'conv_abc')
    assert result == {'status': expected_status}

def test_get_full_conversation_unexpected_error(mock_dynamodb_resource):
    """Test handling of unexpected errors during get_item."""
    mock_conversations_table = mock_dynamodb_resource['conversations']
    mock_conversations_table.get_item.side_effect = Exception("Something broke")
    result = dynamodb_service.get_full_conversation('user1', 'conv_abc')
    assert result == {'status': 'INTERNAL_ERROR'}

# --- write_to_stage_table Tests ---

@patch('src.staging_lambda.lambda_pkg.services.dynamodb_service.time.time')
def test_write_to_stage_table_success(mock_time, mock_dynamodb_resource):
    """Test successful write to the stage table."""
    mock_stage_table = mock_dynamodb_resource['stage']
    mock_time.return_value = 1700000000.0 # Fixed time for predictable TTL
    context = {
        'conversation_id': 'conv_xyz',
        'message_sid': 'SM_sid_1',
        'primary_channel': 'company_wa_num',
        'body': 'Test message'
        # other fields ignored by this function
    }

    result = dynamodb_service.write_to_stage_table(context)
    assert result == 'SUCCESS'

    expected_ttl = 1700000000 + 10 + 60 # time() + BATCH_WINDOW + TTL_BUFFER
    mock_stage_table.put_item.assert_called_once_with(Item={
        'conversation_id': 'conv_xyz',
        'message_sid': 'SM_sid_1',
        'primary_channel': 'company_wa_num',
        'body': 'Test message',
        'received_at': ANY, # Use ANY for the timestamp
        'expires_at': expected_ttl
    })

def test_write_to_stage_table_missing_keys(mock_dynamodb_resource):
    """Test failure when context is missing conversation_id or message_sid."""
    mock_stage_table = mock_dynamodb_resource['stage']
    result1 = dynamodb_service.write_to_stage_table({'message_sid': 'sid1'})
    result2 = dynamodb_service.write_to_stage_table({'conversation_id': 'conv1'})
    assert result1 == 'INTERNAL_ERROR'
    assert result2 == 'INTERNAL_ERROR'
    mock_stage_table.put_item.assert_not_called()

@pytest.mark.parametrize(
    "aws_error_code, expected_status",
    [
        ('ProvisionedThroughputExceededException', 'STAGE_DB_TRANSIENT_ERROR'),
        ('ResourceNotFoundException', 'STAGE_DB_CONFIG_ERROR'),
        ('ValidationException', 'STAGE_DB_VALIDATION_ERROR'),
        ('SomeOtherDynamoDBError', 'STAGE_WRITE_ERROR'),
    ]
)
def test_write_to_stage_table_client_error(mock_dynamodb_resource, aws_error_code, expected_status):
    """Test mapping of ClientErrors during stage table write."""
    mock_stage_table = mock_dynamodb_resource['stage']
    mock_stage_table.put_item.side_effect = ClientError(
        error_response={'Error': {'Code': aws_error_code, 'Message': 'Test error'}},
        operation_name='PutItem'
    )
    context = {'conversation_id': 'c1', 'message_sid': 's1'}
    result = dynamodb_service.write_to_stage_table(context)
    assert result == expected_status

def test_write_to_stage_table_unexpected_error(mock_dynamodb_resource):
    """Test handling of unexpected errors during stage table write."""
    mock_stage_table = mock_dynamodb_resource['stage']
    mock_stage_table.put_item.side_effect = Exception("Something broke")
    context = {'conversation_id': 'c1', 'message_sid': 's1'}
    result = dynamodb_service.write_to_stage_table(context)
    assert result == 'INTERNAL_ERROR'

# --- acquire_trigger_lock Tests ---

@patch('src.staging_lambda.lambda_pkg.services.dynamodb_service.time.time')
def test_acquire_trigger_lock_success(mock_time, mock_dynamodb_resource):
    """Test successful acquisition of the trigger lock."""
    mock_lock_table = mock_dynamodb_resource['lock']
    mock_time.return_value = 1700000100.0
    conv_id = 'conv_lock_1'

    result = dynamodb_service.acquire_trigger_lock(conv_id)
    assert result == 'ACQUIRED'

    expected_ttl = 1700000100 + 10 + 60
    mock_lock_table.put_item.assert_called_once_with(
        Item={'conversation_id': conv_id, 'expires_at': expected_ttl},
        ConditionExpression='attribute_not_exists(conversation_id)'
    )

def test_acquire_trigger_lock_exists(mock_dynamodb_resource):
    """Test when the lock already exists (ConditionalCheckFailedException)."""
    mock_lock_table = mock_dynamodb_resource['lock']
    mock_lock_table.put_item.side_effect = ClientError(
        error_response={'Error': {'Code': 'ConditionalCheckFailedException', 'Message': 'Test fail'}},
        operation_name='PutItem'
    )
    result = dynamodb_service.acquire_trigger_lock('conv_exists')
    assert result == 'EXISTS'

def test_acquire_trigger_lock_missing_id(mock_dynamodb_resource):
    """Test failure when called with empty conversation_id."""
    mock_lock_table = mock_dynamodb_resource['lock']
    result = dynamodb_service.acquire_trigger_lock('')
    assert result == 'INTERNAL_ERROR'
    mock_lock_table.put_item.assert_not_called()

@pytest.mark.parametrize(
    "aws_error_code, expected_status",
    [
        ('ProvisionedThroughputExceededException', 'TRIGGER_DB_TRANSIENT_ERROR'),
        ('ResourceNotFoundException', 'TRIGGER_DB_CONFIG_ERROR'),
        ('ValidationException', 'TRIGGER_DB_VALIDATION_ERROR'),
        ('SomeOtherDynamoDBError', 'TRIGGER_LOCK_WRITE_ERROR'),
    ]
)
def test_acquire_trigger_lock_client_error(mock_dynamodb_resource, aws_error_code, expected_status):
    """Test mapping of ClientErrors (other than ConditionalCheck) during lock acquisition."""
    mock_lock_table = mock_dynamodb_resource['lock']
    mock_lock_table.put_item.side_effect = ClientError(
        error_response={'Error': {'Code': aws_error_code, 'Message': 'Test error'}},
        operation_name='PutItem'
    )
    result = dynamodb_service.acquire_trigger_lock('conv_err')
    assert result == expected_status

def test_acquire_trigger_lock_unexpected_error(mock_dynamodb_resource):
    """Test handling of unexpected errors during lock acquisition."""
    mock_lock_table = mock_dynamodb_resource['lock']
    mock_lock_table.put_item.side_effect = Exception("Something broke")
    result = dynamodb_service.acquire_trigger_lock('conv_unexp')
    assert result == 'INTERNAL_ERROR' 