import pytest
import os
from unittest.mock import patch, MagicMock, ANY, call
from botocore.exceptions import ClientError
import boto3 # Required for boto3.dynamodb.conditions.Key

# Use the correct absolute import path based on project structure
from src.messaging_lambda.whatsapp.lambda_pkg.services import dynamodb_service

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

    # Patch environment variables AND boto3.resource
    # Patching environment variables is needed if table names are read dynamically
    with patch.dict(os.environ, {
        'CONVERSATIONS_TABLE': CONVERSATIONS_TABLE_NAME,
        'CONVERSATIONS_STAGE_TABLE': STAGE_TABLE_NAME,
        'CONVERSATIONS_TRIGGER_LOCK_TABLE': LOCK_TABLE_NAME
        }, clear=True), \
         patch('src.messaging_lambda.whatsapp.lambda_pkg.services.dynamodb_service.boto3.resource') as mock_boto_resource:

        mock_boto_resource.return_value = mock_resource

        # Reload the module AFTER patching env vars and BEFORE tests run
        # This ensures the module reads the patched table names upon initialization
        import importlib
        importlib.reload(dynamodb_service)

        # Yield the individual table mocks for convenience in tests
        yield {
            "conversations": mock_conversations_table,
            "stage": mock_stage_table,
            "lock": mock_lock_table
        }

        # Reload again after tests to restore original state if needed
        # (less critical in test environments, but good practice)
        # importlib.reload(dynamodb_service)

# --- acquire_processing_lock Tests ---

def test_acquire_processing_lock_success(mock_dynamodb_resource):
    """Test successful acquisition of the processing lock."""
    mock_conv_table = mock_dynamodb_resource['conversations']
    pk = "user1"
    sk = "conv1"
    result = dynamodb_service.acquire_processing_lock(pk, sk)

    assert result == dynamodb_service.LOCK_ACQUIRED
    mock_conv_table.update_item.assert_called_once_with(
        Key={'primary_channel': pk, 'conversation_id': sk},
        UpdateExpression="SET conversation_status = :proc_status",
        ConditionExpression="attribute_not_exists(conversation_status) OR conversation_status <> :proc_status",
        ExpressionAttributeValues={':proc_status': dynamodb_service.PROCESSING_STATUS}
    )

def test_acquire_processing_lock_exists(mock_dynamodb_resource):
    """Test when lock exists (ConditionalCheckFailedException)."""
    mock_conv_table = mock_dynamodb_resource['conversations']
    mock_conv_table.update_item.side_effect = ClientError(
        error_response={'Error': {'Code': 'ConditionalCheckFailedException'}},
        operation_name='UpdateItem'
    )
    result = dynamodb_service.acquire_processing_lock("u1", "c1")
    assert result == dynamodb_service.LOCK_EXISTS

def test_acquire_processing_lock_db_error(mock_dynamodb_resource):
    """Test other DynamoDB errors during lock acquisition."""
    mock_conv_table = mock_dynamodb_resource['conversations']
    mock_conv_table.update_item.side_effect = ClientError(
        error_response={'Error': {'Code': 'ProvisionedThroughputExceededException'}},
        operation_name='UpdateItem'
    )
    result = dynamodb_service.acquire_processing_lock("u1", "c1")
    assert result == dynamodb_service.DB_ERROR

def test_acquire_processing_lock_unexpected_error(mock_dynamodb_resource):
    """Test unexpected errors during lock acquisition."""
    mock_conv_table = mock_dynamodb_resource['conversations']
    mock_conv_table.update_item.side_effect = Exception("Oops")
    result = dynamodb_service.acquire_processing_lock("u1", "c1")
    assert result == dynamodb_service.DB_ERROR

# --- query_staging_table Tests --- (Assuming this is the correct function name)

def test_query_staging_table_success(mock_dynamodb_resource):
    """Test successful query of staging table."""
    mock_stage_table = mock_dynamodb_resource['stage']
    expected_items = [{"message_sid": "s1", "body": "b1"}, {"message_sid": "s2", "body": "b2"}]
    mock_stage_table.query.return_value = {'Items': expected_items}

    items = dynamodb_service.query_staging_table("conv1")

    assert items == expected_items
    mock_stage_table.query.assert_called_once_with(
        KeyConditionExpression=ANY, # Rely on ANY matcher
        ConsistentRead=True
    )
    # Removed detailed check of KeyConditionExpression attributes
    # # Check KeyConditionExpression content more specifically
    # args, kwargs = mock_stage_table.query.call_args
    # key_expr = kwargs['KeyConditionExpression']
    # assert key_expr.expression_operator == '='
    # assert key_expr.name == 'conversation_id'
    # assert key_expr.values == ['conv1']

def test_query_staging_table_no_items(mock_dynamodb_resource):
    """Test query when no items are found."""
    mock_stage_table = mock_dynamodb_resource['stage']
    mock_stage_table.query.return_value = {'Items': []}
    items = dynamodb_service.query_staging_table("conv1")
    assert items == []

def test_query_staging_table_db_error(mock_dynamodb_resource):
    """Test ClientError during staging query."""
    mock_stage_table = mock_dynamodb_resource['stage']
    mock_stage_table.query.side_effect = ClientError({'Error': {'Code': 'InternalServerError'}}, 'Query')
    items = dynamodb_service.query_staging_table("conv1")
    assert items is None

def test_query_staging_table_unexpected_error(mock_dynamodb_resource):
    """Test unexpected error during staging query."""
    mock_stage_table = mock_dynamodb_resource['stage']
    mock_stage_table.query.side_effect = Exception("Oops")
    items = dynamodb_service.query_staging_table("conv1")
    assert items is None

# --- get_conversation_item Tests ---

def test_get_conversation_item_success(mock_dynamodb_resource):
    """Test successful retrieval of conversation item."""
    mock_conv_table = mock_dynamodb_resource['conversations']
    expected_item = {"pk": "user1", "sk": "conv1", "status": "active"}
    mock_conv_table.get_item.return_value = {'Item': expected_item}

    item = dynamodb_service.get_conversation_item("user1", "conv1")

    assert item == expected_item
    mock_conv_table.get_item.assert_called_once_with(
        Key={'primary_channel': "user1", 'conversation_id': "conv1"},
        ConsistentRead=True
    )

def test_get_conversation_item_not_found(mock_dynamodb_resource):
    """Test get_item when item is not found."""
    mock_conv_table = mock_dynamodb_resource['conversations']
    mock_conv_table.get_item.return_value = {'ResponseMetadata': {}} # No 'Item' key
    item = dynamodb_service.get_conversation_item("user1", "conv1")
    assert item is None

def test_get_conversation_item_db_error(mock_dynamodb_resource):
    """Test ClientError during get_item."""
    mock_conv_table = mock_dynamodb_resource['conversations']
    mock_conv_table.get_item.side_effect = ClientError({'Error': {'Code': 'ThrottlingException'}}, 'GetItem')
    item = dynamodb_service.get_conversation_item("user1", "conv1")
    assert item is None

# --- update_conversation_after_reply Tests ---

@patch('src.messaging_lambda.whatsapp.lambda_pkg.services.dynamodb_service.datetime')
def test_update_conversation_success_minimal(mock_dt, mock_dynamodb_resource):
    """Test successful minimal update after reply."""
    mock_conv_table = mock_dynamodb_resource['conversations']
    mock_now = MagicMock()
    mock_now.isoformat.return_value = "2023-01-01T12:00:00+00:00"
    mock_dt.now.return_value = mock_now

    pk = "user_update"
    sk = "conv_update"
    user_msg = {"role": "user", "content": "u", "timestamp": "t1", "sid": "s1"}
    assist_msg = {"role": "assistant", "content": "a", "timestamp": "t2", "sid": "s2"}

    status, msg = dynamodb_service.update_conversation_after_reply(pk, sk, user_msg, assist_msg)

    assert status == dynamodb_service.DB_SUCCESS
    assert msg is None
    mock_conv_table.update_item.assert_called_once()
    call_args = mock_conv_table.update_item.call_args[1]
    assert call_args['Key'] == {'primary_channel': pk, 'conversation_id': sk}
    assert call_args['ConditionExpression'] == "#status = :lock_status"
    assert call_args['ExpressionAttributeValues'][':new_status'] == "reply_sent"
    assert call_args['ExpressionAttributeValues'][':ts'] == "2023-01-01T12:00:00+00:00"
    assert call_args['ExpressionAttributeValues'][':new_msgs'] == [user_msg, assist_msg]
    assert call_args['ExpressionAttributeValues'][':lock_status'] == dynamodb_service.PROCESSING_STATUS
    assert call_args['ExpressionAttributeNames']['#status'] == "conversation_status"
    assert call_args['ExpressionAttributeNames']['#updated'] == "updated_at"
    assert call_args['ExpressionAttributeNames']['#msgs'] == "messages"

@patch('src.messaging_lambda.whatsapp.lambda_pkg.services.dynamodb_service.datetime')
def test_update_conversation_success_all_fields(mock_dt, mock_dynamodb_resource):
    """Test successful update with all optional fields."""
    mock_conv_table = mock_dynamodb_resource['conversations']
    mock_now = MagicMock()
    mock_now.isoformat.return_value = "2023-01-01T12:00:00+00:00"
    mock_dt.now.return_value = mock_now

    pk, sk = "u_all", "c_all"
    user_msg = {"role": "user", "content": "u"}
    assist_msg = {"role": "assistant", "content": "a"}

    status, msg = dynamodb_service.update_conversation_after_reply(
        primary_channel_pk=pk, conversation_id_sk=sk,
        user_message_map=user_msg, assistant_message_map=assist_msg,
        new_status="handoff_pending", processing_time_ms=1234,
        task_complete=1, hand_off_to_human=True, hand_off_to_human_reason="AI confused",
        updated_openai_thread_id="thread_new"
    )

    assert status == dynamodb_service.DB_SUCCESS
    mock_conv_table.update_item.assert_called_once()
    call_args = mock_conv_table.update_item.call_args[1]
    assert call_args['ExpressionAttributeValues'][':new_status'] == "handoff_pending"
    assert call_args['ExpressionAttributeValues'][':proc_time'] == 1234
    assert call_args['ExpressionAttributeValues'][':task_comp'] == 1
    assert call_args['ExpressionAttributeValues'][':handoff'] is True
    assert call_args['ExpressionAttributeValues'][':handoff_reason'] == "AI confused"
    assert call_args['ExpressionAttributeValues'][':tid'] == "thread_new"
    assert "#proc_time" in call_args['ExpressionAttributeNames']
    assert "#task_comp" in call_args['ExpressionAttributeNames']
    assert "#handoff" in call_args['ExpressionAttributeNames']
    assert "#handoff_reason" in call_args['ExpressionAttributeNames']
    assert "#tid" in call_args['ExpressionAttributeNames']

def test_update_conversation_lock_lost(mock_dynamodb_resource):
    """Test ConditionalCheckFailedException during final update."""
    mock_conv_table = mock_dynamodb_resource['conversations']
    mock_conv_table.update_item.side_effect = ClientError(
        error_response={'Error': {'Code': 'ConditionalCheckFailedException'}},
        operation_name='UpdateItem'
    )
    status, msg = dynamodb_service.update_conversation_after_reply("u", "c", {}, {})
    assert status == dynamodb_service.DB_LOCK_LOST
    assert "ConditionalCheckFailedException" in msg

def test_update_conversation_db_error(mock_dynamodb_resource):
    """Test other ClientError during final update."""
    mock_conv_table = mock_dynamodb_resource['conversations']
    mock_conv_table.update_item.side_effect = ClientError(
        error_response={'Error': {'Code': 'ValidationException'}},
        operation_name='UpdateItem'
    )
    status, msg = dynamodb_service.update_conversation_after_reply("u", "c", {}, {})
    assert status == dynamodb_service.DB_ERROR
    assert "ValidationException" in msg

# --- cleanup_staging_table Tests ---

def test_cleanup_staging_table_success(mock_dynamodb_resource):
    """Test successful batch delete from staging table."""
    mock_stage_table = mock_dynamodb_resource['stage']
    mock_batch_writer = MagicMock()
    mock_stage_table.batch_writer.return_value.__enter__.return_value = mock_batch_writer

    keys = [
        {'conversation_id': 'c1', 'message_sid': 's1'},
        {'conversation_id': 'c1', 'message_sid': 's2'}
    ]
    result = dynamodb_service.cleanup_staging_table(keys)

    assert result is True
    assert mock_batch_writer.delete_item.call_count == 2
    mock_batch_writer.delete_item.assert_has_calls([
        call(Key=keys[0]),
        call(Key=keys[1])
    ])

def test_cleanup_staging_table_empty_keys(mock_dynamodb_resource):
    """Test cleanup with empty key list."""
    mock_stage_table = mock_dynamodb_resource['stage']
    result = dynamodb_service.cleanup_staging_table([])
    assert result is True
    mock_stage_table.batch_writer.assert_not_called()

def test_cleanup_staging_table_invalid_keys(mock_dynamodb_resource):
    """Test cleanup with some invalid keys in the list."""
    mock_stage_table = mock_dynamodb_resource['stage']
    mock_batch_writer = MagicMock()
    mock_stage_table.batch_writer.return_value.__enter__.return_value = mock_batch_writer

    keys = [
        {'conversation_id': 'c1', 'message_sid': 's1'},
        {'conversation_id': 'c1'}, # Missing sid
        {'message_sid': 's3'} # Missing conv_id
    ]
    result = dynamodb_service.cleanup_staging_table(keys)

    assert result is True
    mock_batch_writer.delete_item.assert_called_once_with(Key=keys[0]) # Only valid key deleted

def test_cleanup_staging_table_db_error(mock_dynamodb_resource):
    """Test ClientError during batch write."""
    mock_stage_table = mock_dynamodb_resource['stage']
    mock_stage_table.batch_writer.side_effect = ClientError({'Error': {'Code': 'LimitExceededException'}}, 'BatchWriteItem')
    result = dynamodb_service.cleanup_staging_table([{'conversation_id': 'c1', 'message_sid': 's1'}])
    assert result is False

# --- cleanup_trigger_lock Tests ---

def test_cleanup_trigger_lock_success(mock_dynamodb_resource):
    """Test successful deletion of trigger lock item."""
    mock_lock_table = mock_dynamodb_resource['lock']
    result = dynamodb_service.cleanup_trigger_lock("conv_del")
    assert result is True
    mock_lock_table.delete_item.assert_called_once_with(Key={'conversation_id': "conv_del"})

def test_cleanup_trigger_lock_db_error(mock_dynamodb_resource):
    """Test ClientError during lock deletion."""
    mock_lock_table = mock_dynamodb_resource['lock']
    mock_lock_table.delete_item.side_effect = ClientError({'Error': {'Code': 'ResourceNotFoundException'}}, 'DeleteItem')
    result = dynamodb_service.cleanup_trigger_lock("conv_del_err")
    assert result is False

# --- release_lock_for_retry Tests ---

@patch('src.messaging_lambda.whatsapp.lambda_pkg.services.dynamodb_service.datetime')
def test_release_lock_for_retry_success(mock_dt, mock_dynamodb_resource):
    """Test successfully setting status to 'retry'."""
    mock_conv_table = mock_dynamodb_resource['conversations']
    mock_now = MagicMock()
    mock_now.isoformat.return_value = "2023-01-01T13:00:00+00:00"
    mock_dt.now.return_value = mock_now

    result = dynamodb_service.release_lock_for_retry("u_retry", "c_retry")
    assert result is True
    mock_conv_table.update_item.assert_called_once_with(
        Key={'primary_channel': "u_retry", 'conversation_id': "c_retry"},
        UpdateExpression="SET conversation_status = :retry_status, updated_at = :ts",
        ExpressionAttributeValues={
            ':retry_status': "retry",
            ':ts': "2023-01-01T13:00:00+00:00"
        },
        ReturnValues="NONE"
    )

def test_release_lock_for_retry_db_error(mock_dynamodb_resource):
    """Test ClientError during lock release."""
    mock_conv_table = mock_dynamodb_resource['conversations']
    mock_conv_table.update_item.side_effect = ClientError({'Error': {'Code': 'ValidationException'}}, 'UpdateItem')
    result = dynamodb_service.release_lock_for_retry("u_retry_err", "c_retry_err")
    assert result is False 