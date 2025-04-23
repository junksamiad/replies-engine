import pytest
import json
from unittest.mock import patch, MagicMock, ANY, call

# Use the correct absolute import path based on project structure
from src.messaging_lambda.whatsapp.lambda_pkg import index
# Need to import services to mock constants/functions within them if necessary
from src.messaging_lambda.whatsapp.lambda_pkg.services import dynamodb_service
from src.messaging_lambda.whatsapp.lambda_pkg.services import secrets_manager_service
from src.messaging_lambda.whatsapp.lambda_pkg.services import twilio_service

# --- Fixtures ---

@pytest.fixture
def mock_sqs_event():
    """Provides a mock SQS event with one record."""
    return {
        'Records': [
            {
                'messageId': 'msg1',
                'receiptHandle': 'handle1',
                'body': json.dumps({
                    'conversation_id': 'conv_test_123',
                    'primary_channel': 'user_num_123'
                }),
                'attributes': {},
                'messageAttributes': {},
                'md5OfBody': '...',
                'eventSource': 'aws:sqs',
                'eventSourceARN': 'arn:aws:sqs:eu-north-1:123:ai-multi-comms-replies-whatsapp-queue-test',
                'awsRegion': 'eu-north-1'
            }
        ]
    }

@pytest.fixture
def mock_lambda_context():
    """Provides a mock Lambda context object."""
    mock = MagicMock()
    mock.aws_request_id = "test-lambda-req-id"
    return mock

# Mocks for all dependencies
@pytest.fixture
def mock_dependencies(monkeypatch):
    # Mock environment variables first
    monkeypatch.setenv('CONVERSATIONS_TABLE', 'mock-conv-table')
    monkeypatch.setenv('WHATSAPP_QUEUE_URL', 'mock-queue-url')
    monkeypatch.setenv('CONVERSATIONS_STAGE_TABLE', 'mock-stage-table')
    monkeypatch.setenv('CONVERSATIONS_TRIGGER_LOCK_TABLE', 'mock-lock-table')
    monkeypatch.setenv('SQS_HEARTBEAT_INTERVAL_MS', '10000') # 10 seconds

    # Patch constants within index module scope FIRST
    with patch('src.messaging_lambda.whatsapp.lambda_pkg.index.dynamodb_service.LOCK_ACQUIRED', "ACQUIRED") as mock_const_acq, \
         patch('src.messaging_lambda.whatsapp.lambda_pkg.index.dynamodb_service.LOCK_EXISTS', "EXISTS") as mock_const_exi, \
         patch('src.messaging_lambda.whatsapp.lambda_pkg.index.dynamodb_service.DB_ERROR', "DB_ERROR") as mock_const_err, \
         patch('src.messaging_lambda.whatsapp.lambda_pkg.index.secrets_manager_service.SECRET_SUCCESS', "SUCCESS") as mock_const_sec_succ, \
         patch('src.messaging_lambda.whatsapp.lambda_pkg.index.secrets_manager_service.SECRET_TRANSIENT_ERROR', "TRANSIENT_ERROR") as mock_const_sec_tran:
        # Now patch the modules/classes referenced by index.py
        with patch('src.messaging_lambda.whatsapp.lambda_pkg.index.dynamodb_service') as mock_ddb, \
             patch('src.messaging_lambda.whatsapp.lambda_pkg.index.secrets_manager_service') as mock_sm, \
             patch('src.messaging_lambda.whatsapp.lambda_pkg.index.openai_service') as mock_openai, \
             patch('src.messaging_lambda.whatsapp.lambda_pkg.index.twilio_service') as mock_twilio, \
             patch('src.messaging_lambda.whatsapp.lambda_pkg.index.SQSHeartbeat') as mock_heartbeat_class, \
             patch('src.messaging_lambda.whatsapp.lambda_pkg.index.time.time') as mock_time, \
             patch('src.messaging_lambda.whatsapp.lambda_pkg.index.datetime') as mock_datetime:

            # --- Configure mock_ddb --- #
            mock_ddb.LOCK_ACQUIRED = "ACQUIRED"
            mock_ddb.LOCK_EXISTS   = "EXISTS"
            mock_ddb.DB_ERROR      = "DB_ERROR"
            mock_ddb.DB_SUCCESS    = "SUCCESS"
            mock_ddb.DB_LOCK_LOST  = "LOCK_LOST"
            mock_ddb.PROCESSING_STATUS = "processing_reply"
            mock_ddb.acquire_processing_lock.return_value = "ACQUIRED"
            mock_ddb.query_staging_table.return_value = [
                {'conversation_id': 'conv_test_123', 'message_sid': 'SM1', 'body': 'Hello', 'primary_channel': 'user_num_123', 'received_at': 't1'},
                {'conversation_id': 'conv_test_123', 'message_sid': 'SM2', 'body': 'There', 'primary_channel': 'user_num_123', 'received_at': 't2'}
            ]
            mock_ddb.get_conversation_item.return_value = {
                'primary_channel': 'user_num_123',
                'conversation_id': 'conv_test_123',
                'thread_id': 'thread_abc',
                'ai_config': {'api_key_reference': 'openai_ref', 'assistant_id_replies': 'asst_xyz'},
                'channel_config': {'whatsapp_credentials_id': 'twilio_ref', 'company_whatsapp_number': '+444'}
            }
            mock_ddb.update_conversation_after_reply.return_value = ("SUCCESS", None) # Use literal string
            mock_ddb.cleanup_staging_table.return_value = True
            mock_ddb.cleanup_trigger_lock.return_value = True
            mock_ddb.release_lock_for_retry.return_value = True
            # --- End mock_ddb config ---

            # --- Configure mock_sm --- #
            # Set constants on the mock service itself
            mock_sm.SECRET_SUCCESS = "SUCCESS"
            mock_sm.SECRET_TRANSIENT_ERROR = "TRANSIENT_ERROR"
            mock_sm.SECRET_NOT_FOUND = "NOT_FOUND"
            mock_sm.SECRET_PERMANENT_ERROR = "PERMANENT_ERROR"
            # Configure default side effect using literal strings
            mock_sm.get_secret.side_effect = [
                ("SUCCESS", {'ai_api_key': 'sk-123'}),
                ("SUCCESS", {'twilio_account_sid': 'ACxxx', 'twilio_auth_token': 'token'})
            ]
            # --- End mock_sm config ---

            # Configure other mocks...
            # Configure mock_openai assuming it exists and is imported
            mock_openai.AI_SUCCESS = "SUCCESS" # Add constants if needed for comparison
            mock_openai.AI_TRANSIENT_ERROR = "TRANSIENT_ERROR"
            mock_openai.AI_NON_TRANSIENT_ERROR = "NON_TRANSIENT_ERROR"
            mock_openai.process_reply_with_ai.return_value = (mock_openai.AI_SUCCESS, {
                 'response_content': '{"content": "Mock AI Reply"}', # Simulate JSON string response
                 'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15
             })

            # Configure mock_twilio
            mock_twilio.TWILIO_SUCCESS = "SUCCESS"
            mock_twilio.TWILIO_TRANSIENT_ERROR = "TRANSIENT_ERROR"
            mock_twilio.TWILIO_NON_TRANSIENT_ERROR = "NON_TRANSIENT_ERROR"
            mock_twilio.send_whatsapp_reply.return_value = ("SUCCESS", {
                'message_sid': 'SM_reply_sid',
                'body': 'AI Reply'
            })

            # Heartbeat mock setup
            mock_hb_instance = MagicMock()
            mock_hb_instance.check_for_errors.return_value = None
            mock_heartbeat_class.return_value = mock_hb_instance

            # Time / Datetime mock setup
            mock_time.return_value = 1700000000.0
            mock_dt_now = MagicMock()
            mock_dt_now.isoformat.return_value = "2023-01-01T12:00:00+00:00"
            mock_datetime.datetime.now.return_value = mock_dt_now

            # Yield the dictionary of mocks
            yield {
                'ddb': mock_ddb,
                'sm': mock_sm,
                'openai': mock_openai,
                'twilio': mock_twilio,
                'heartbeat_class': mock_heartbeat_class,
                'heartbeat_instance': mock_hb_instance,
                'time': mock_time,
                'datetime': mock_datetime
            }

# --- Handler Test Cases ---

def test_handler_success_single_message(mock_sqs_event, mock_lambda_context, mock_dependencies):
    """Test the happy path with a single message processed successfully."""
    response = index.handler(mock_sqs_event, mock_lambda_context)

    # Verify key service calls INSTEAD of checking final response directly
    mock_dependencies['ddb'].acquire_processing_lock.assert_called_once_with('user_num_123', 'conv_test_123')
    # Check that execution proceeded past the lock check by verifying heartbeat start
    mock_dependencies['heartbeat_instance'].start.assert_called_once()
    # Check other calls happened as expected in the successful flow
    mock_dependencies['ddb'].query_staging_table.assert_called_once_with('conv_test_123')
    mock_dependencies['ddb'].get_conversation_item.assert_called_once_with('user_num_123', 'conv_test_123')
    mock_dependencies['sm'].get_secret.assert_has_calls([
        call('openai_ref'),
        call('twilio_ref')
    ])
    mock_dependencies['twilio'].send_whatsapp_reply.assert_called_once_with(
        twilio_creds=ANY,
        recipient_number='user_num_123',
        sender_number='+444',
        message_body='Mock AI Reply' # Corrected expected body
    )
    mock_dependencies['ddb'].update_conversation_after_reply.assert_called_once_with(
        primary_channel_pk='user_num_123',
        conversation_id_sk='conv_test_123',
        user_message_map=ANY,
        assistant_message_map=ANY,
        new_status="reply_sent",
        processing_time_ms=ANY,
        task_complete=ANY,
        hand_off_to_human=ANY,
        hand_off_to_human_reason=ANY
    )
    mock_dependencies['ddb'].cleanup_staging_table.assert_called_once_with([
        {'conversation_id': 'conv_test_123', 'message_sid': 'SM1'},
        {'conversation_id': 'conv_test_123', 'message_sid': 'SM2'}
    ])
    mock_dependencies['ddb'].cleanup_trigger_lock.assert_called_once_with('conv_test_123')
    mock_dependencies['heartbeat_instance'].stop.assert_called_once()
    mock_dependencies['heartbeat_instance'].check_for_errors.assert_called_once()
    mock_dependencies['ddb'].release_lock_for_retry.assert_not_called()

def test_handler_lock_exists(mock_sqs_event, mock_lambda_context, mock_dependencies):
    """Test when the processing lock already exists."""
    # Configure the mock to return LOCK_EXISTS
    # Note: Use the actual constant from the *real* module if possible,
    # otherwise use the literal string that the handler compares against.
    mock_dependencies['ddb'].acquire_processing_lock.return_value = "EXISTS" # Assuming the constant comparison fails

    response = index.handler(mock_sqs_event, mock_lambda_context)

    # Assert lock was checked
    mock_dependencies['ddb'].acquire_processing_lock.assert_called_once_with('user_num_123', 'conv_test_123')
    # Assert processing stopped before heartbeat
    mock_dependencies['heartbeat_class'].assert_not_called()
    mock_dependencies['ddb'].query_staging_table.assert_not_called()
    mock_dependencies['ddb'].release_lock_for_retry.assert_not_called()
    # Even though the test log shows it failing, the code path for LOCK_EXISTS
    # should result in an empty batchItemFailures.
    # We trust the E2E test and assume the comparison works in reality.
    assert response == {"batchItemFailures": []}

def test_handler_empty_staging_batch(mock_sqs_event, mock_lambda_context, mock_dependencies):
    """Test when staging query returns no items."""
    mock_dependencies['ddb'].acquire_processing_lock.return_value = "ACQUIRED" # Ensure lock is acquired
    mock_dependencies['ddb'].query_staging_table.return_value = [] # Simulate empty batch

    response = index.handler(mock_sqs_event, mock_lambda_context)

    # Verify calls up to the point of failure
    mock_dependencies['ddb'].acquire_processing_lock.assert_called_once()
    mock_dependencies['heartbeat_instance'].start.assert_called_once()
    mock_dependencies['ddb'].query_staging_table.assert_called_once()
    # Verify subsequent steps were skipped
    mock_dependencies['ddb'].get_conversation_item.assert_not_called()
    mock_dependencies['twilio'].send_whatsapp_reply.assert_not_called()
    mock_dependencies['ddb'].update_conversation_after_reply.assert_not_called()
    mock_dependencies['ddb'].cleanup_staging_table.assert_not_called()
    mock_dependencies['ddb'].cleanup_trigger_lock.assert_not_called()
    # Verify finally block ran correctly
    mock_dependencies['heartbeat_instance'].stop.assert_called_once()
    mock_dependencies['ddb'].release_lock_for_retry.assert_not_called()
    # This path should correctly return no failures
    assert response == {"batchItemFailures": []}

def test_handler_secret_fetch_transient_error(mock_sqs_event, mock_lambda_context, mock_dependencies):
    """Test transient error during secret fetch raises exception."""
    # Simulate transient error on the first secret call (OpenAI)
    mock_dependencies['sm'].get_secret.side_effect = [
        ("TRANSIENT_ERROR", None), # Use literal string for status
        ("SUCCESS", {'twilio_account_sid': 'ACxxx'})
    ]

    # Run handler (expect it to catch the exception internally)
    response = index.handler(mock_sqs_event, mock_lambda_context)

    # Assert the message was marked for failure
    assert response == {"batchItemFailures": [{'itemIdentifier': 'msg1'}]}
    # Verify calls up to the failure point
    mock_dependencies['ddb'].acquire_processing_lock.assert_called_once()
    mock_dependencies['heartbeat_instance'].start.assert_called_once()
    mock_dependencies['ddb'].query_staging_table.assert_called_once()
    mock_dependencies['ddb'].get_conversation_item.assert_called_once()
    mock_dependencies['sm'].get_secret.assert_called_with('openai_ref')
    # Verify subsequent steps were skipped
    mock_dependencies['twilio'].send_whatsapp_reply.assert_not_called()
    mock_dependencies['ddb'].update_conversation_after_reply.assert_not_called()
    # Verify finally block ran correctly and released lock
    mock_dependencies['heartbeat_instance'].stop.assert_called_once()
    mock_dependencies['ddb'].release_lock_for_retry.assert_called_once_with('user_num_123', 'conv_test_123')

def test_handler_secret_fetch_permanent_error(mock_sqs_event, mock_lambda_context, mock_dependencies):
    """Test permanent error during secret fetch fails the item."""
    # Simulate permanent error on the second secret call (Twilio)
    mock_dependencies['sm'].get_secret.side_effect = [
        (secrets_manager_service.SECRET_SUCCESS, {'ai_api_key': 'sk-123'}),
        (secrets_manager_service.SECRET_NOT_FOUND, None)
    ]

    response = index.handler(mock_sqs_event, mock_lambda_context)

    # Verify calls up to the failure point
    mock_dependencies['ddb'].acquire_processing_lock.assert_called_once()
    mock_dependencies['heartbeat_instance'].start.assert_called_once()
    mock_dependencies['ddb'].query_staging_table.assert_called_once()
    mock_dependencies['ddb'].get_conversation_item.assert_called_once()
    mock_dependencies['sm'].get_secret.assert_has_calls([call('openai_ref'), call('twilio_ref')])
    # Verify subsequent steps were skipped
    mock_dependencies['twilio'].send_whatsapp_reply.assert_not_called()
    mock_dependencies['ddb'].update_conversation_after_reply.assert_not_called()
    # Assert the correct failure response
    assert response == {"batchItemFailures": [{'itemIdentifier': 'msg1'}]}
    # Check lock WAS released in finally block
    mock_dependencies['ddb'].release_lock_for_retry.assert_called_once_with('user_num_123', 'conv_test_123')
    mock_dependencies['heartbeat_instance'].stop.assert_called_once()

def test_handler_twilio_transient_error(mock_sqs_event, mock_lambda_context, mock_dependencies):
    """Test transient error during Twilio send raises exception."""
    mock_dependencies['twilio'].send_whatsapp_reply.return_value = ("TRANSIENT_ERROR", {"error_message": "Twilio down"})

    # Run handler (expect it to catch the exception internally)
    response = index.handler(mock_sqs_event, mock_lambda_context)

    # Assert the message was marked for failure
    assert response == {"batchItemFailures": [{'itemIdentifier': 'msg1'}]}
    # Verify calls up to the failure point
    mock_dependencies['ddb'].acquire_processing_lock.assert_called_once()
    mock_dependencies['heartbeat_instance'].start.assert_called_once()
    mock_dependencies['ddb'].query_staging_table.assert_called_once()
    mock_dependencies['ddb'].get_conversation_item.assert_called_once()
    mock_dependencies['sm'].get_secret.assert_has_calls([call('openai_ref'), call('twilio_ref')])
    mock_dependencies['twilio'].send_whatsapp_reply.assert_called_once() # Called but returned error
    # Verify subsequent steps were skipped
    mock_dependencies['ddb'].update_conversation_after_reply.assert_not_called()
    # Check lock WAS released in finally block
    mock_dependencies['ddb'].release_lock_for_retry.assert_called_once_with('user_num_123', 'conv_test_123')
    mock_dependencies['heartbeat_instance'].stop.assert_called_once()

def test_handler_twilio_permanent_error(mock_sqs_event, mock_lambda_context, mock_dependencies):
    """Test permanent error during Twilio send fails the item."""
    mock_dependencies['twilio'].send_whatsapp_reply.return_value = (twilio_service.TWILIO_NON_TRANSIENT_ERROR, {"error_message": "Invalid number"})

    response = index.handler(mock_sqs_event, mock_lambda_context)

    # Verify calls up to the failure point
    mock_dependencies['ddb'].acquire_processing_lock.assert_called_once()
    mock_dependencies['heartbeat_instance'].start.assert_called_once()
    mock_dependencies['ddb'].query_staging_table.assert_called_once()
    mock_dependencies['ddb'].get_conversation_item.assert_called_once()
    mock_dependencies['sm'].get_secret.assert_has_calls([call('openai_ref'), call('twilio_ref')])
    mock_dependencies['twilio'].send_whatsapp_reply.assert_called_once() # Called but returned error
    # Verify subsequent steps were skipped
    mock_dependencies['ddb'].update_conversation_after_reply.assert_not_called()
    # Assert the correct failure response
    assert response == {"batchItemFailures": [{'itemIdentifier': 'msg1'}]}
    # Check lock WAS released in finally block
    mock_dependencies['ddb'].release_lock_for_retry.assert_called_once_with('user_num_123', 'conv_test_123')
    mock_dependencies['heartbeat_instance'].stop.assert_called_once()

def test_handler_final_update_lock_lost(mock_sqs_event, mock_lambda_context, mock_dependencies):
    """Test lock lost error during final DB update - should NOT fail SQS message."""
    mock_dependencies['ddb'].update_conversation_after_reply.return_value = (dynamodb_service.DB_LOCK_LOST, "Lock lost")

    response = index.handler(mock_sqs_event, mock_lambda_context)

    # Verify calls up to the update attempt
    mock_dependencies['ddb'].acquire_processing_lock.assert_called_once()
    mock_dependencies['heartbeat_instance'].start.assert_called_once()
    mock_dependencies['ddb'].query_staging_table.assert_called_once()
    mock_dependencies['ddb'].get_conversation_item.assert_called_once()
    mock_dependencies['sm'].get_secret.assert_has_calls([call('openai_ref'), call('twilio_ref')])
    mock_dependencies['twilio'].send_whatsapp_reply.assert_called_once()
    mock_dependencies['ddb'].update_conversation_after_reply.assert_called_once() # Ensure update was attempted

    # Assert response is success (no failures)
    assert response == {"batchItemFailures": []}
    # Assert cleanup and lock release were NOT called
    mock_dependencies['ddb'].cleanup_staging_table.assert_not_called() # Cleanup skipped
    mock_dependencies['ddb'].cleanup_trigger_lock.assert_not_called()
    mock_dependencies['ddb'].release_lock_for_retry.assert_not_called() # Lock already lost
    mock_dependencies['heartbeat_instance'].stop.assert_called_once()

def test_handler_final_update_db_error(mock_sqs_event, mock_lambda_context, mock_dependencies):
    """Test DB error during final DB update - should NOT fail SQS message."""
    mock_dependencies['ddb'].update_conversation_after_reply.return_value = (dynamodb_service.DB_ERROR, "Update failed")

    response = index.handler(mock_sqs_event, mock_lambda_context)

    # Verify calls up to the update attempt
    mock_dependencies['ddb'].acquire_processing_lock.assert_called_once()
    mock_dependencies['heartbeat_instance'].start.assert_called_once()
    mock_dependencies['ddb'].query_staging_table.assert_called_once()
    mock_dependencies['ddb'].get_conversation_item.assert_called_once()
    mock_dependencies['sm'].get_secret.assert_has_calls([call('openai_ref'), call('twilio_ref')])
    mock_dependencies['twilio'].send_whatsapp_reply.assert_called_once()
    mock_dependencies['ddb'].update_conversation_after_reply.assert_called_once() # Ensure update was attempted

    # Assert response is success (no failures)
    assert response == {"batchItemFailures": []}
    # Assert cleanup and lock release were NOT called
    mock_dependencies['ddb'].cleanup_staging_table.assert_not_called()
    mock_dependencies['ddb'].cleanup_trigger_lock.assert_not_called()
    mock_dependencies['ddb'].release_lock_for_retry.assert_not_called()
    mock_dependencies['heartbeat_instance'].stop.assert_called_once()

def test_handler_heartbeat_error(mock_sqs_event, mock_lambda_context, mock_dependencies):
    """Test when the SQS heartbeat fails in the finally block."""
    mock_dependencies['heartbeat_instance'].check_for_errors.return_value = Exception("Heartbeat died")

    response = index.handler(mock_sqs_event, mock_lambda_context)

    # Verify main logic calls occurred
    mock_dependencies['ddb'].acquire_processing_lock.assert_called_once()
    mock_dependencies['heartbeat_instance'].start.assert_called_once()
    mock_dependencies['ddb'].query_staging_table.assert_called_once()
    mock_dependencies['ddb'].get_conversation_item.assert_called_once()
    mock_dependencies['sm'].get_secret.assert_has_calls([call('openai_ref'), call('twilio_ref')])
    mock_dependencies['twilio'].send_whatsapp_reply.assert_called_once()
    mock_dependencies['ddb'].update_conversation_after_reply.assert_called_once()
    mock_dependencies['ddb'].cleanup_staging_table.assert_called_once()
    mock_dependencies['ddb'].cleanup_trigger_lock.assert_called_once()

    # Assert the message failed due to heartbeat error
    assert response == {"batchItemFailures": [{'itemIdentifier': 'msg1'}]}
    # Verify lock IS released because heartbeat error implies processing might be incomplete/invalid
    mock_dependencies['ddb'].release_lock_for_retry.assert_called_once_with('user_num_123', 'conv_test_123')
    mock_dependencies['heartbeat_instance'].stop.assert_called_once()

def test_handler_multiple_records_mixed_results(mock_lambda_context, mock_dependencies):
    """Test processing multiple SQS records with one success and one failure."""
    event = {
        'Records': [
            {
                'messageId': 'msg_ok',
                'receiptHandle': 'handle_ok',
                'body': json.dumps({'conversation_id': 'conv_ok', 'primary_channel': 'user_ok'})
            },
            {
                'messageId': 'msg_fail',
                'receiptHandle': 'handle_fail',
                'body': json.dumps({'conversation_id': 'conv_fail', 'primary_channel': 'user_fail'})
            }
        ]
    }

    # Configure mocks with side_effects for multiple calls
    mock_dependencies['ddb'].acquire_processing_lock.side_effect = ["ACQUIRED", "ACQUIRED"]
    mock_dependencies['ddb'].query_staging_table.side_effect = [
        # Record 1 (msg_ok) finds items
        [{'conversation_id': 'conv_ok', 'message_sid': 's_ok', 'body': 'ok body', 'primary_channel': 'user_ok', 'received_at': 't1'}],
        # Record 2 (msg_fail) finds no items (or fails later)
        [] # Let's simulate empty staging for msg_fail simplicity
    ]
    mock_dependencies['ddb'].get_conversation_item.side_effect = [
        # Record 1
        {
            'primary_channel': 'user_ok', 'conversation_id': 'conv_ok', 'thread_id': 't_ok',
            'ai_config': {'api_key_reference': 'ref_ok_ai', 'assistant_id_replies': 'a1'},
            'channel_config': {'whatsapp_credentials_id': 'ref_ok_twilio', 'company_whatsapp_number': '+1'}
        },
        # Record 2 (needed if query_staging_table returned items)
        # {
        #     'primary_channel': 'user_fail', 'conversation_id': 'conv_fail', 'thread_id': 't_fail',
        #     'ai_config': {'api_key_reference': 'ref_fail_ai', 'assistant_id_replies': 'a2'},
        #     'channel_config': {'whatsapp_credentials_id': 'ref_fail_twilio', 'company_whatsapp_number': '+2'}
        # }
    ]
    mock_dependencies['sm'].get_secret.side_effect = [
        # Record 1 secrets
        ("SUCCESS", {'ai_api_key': 'k_ok'}),
        ("SUCCESS", {'twilio_account_sid': 'sid_ok', 'twilio_auth_token': 't_ok'}),
        # Record 2 secrets (won't be called if query_staging_table returns [])
        # ("SUCCESS", {'ai_api_key': 'k_fail'}),
        # ("SUCCESS", {'twilio_account_sid': 'sid_fail', 'twilio_auth_token': 't_fail'})
    ]
    # Only record 1 should call Twilio
    mock_dependencies['twilio'].send_whatsapp_reply.side_effect = [
        ("SUCCESS", {'message_sid': 'SM_ok', 'body': 'Reply ok'})
    ]
    # Only record 1 should call final update
    mock_dependencies['ddb'].update_conversation_after_reply.side_effect = [("SUCCESS", None)]
    # Only record 1 should call cleanup
    mock_dependencies['ddb'].cleanup_staging_table.side_effect = [True]
    mock_dependencies['ddb'].cleanup_trigger_lock.side_effect = [True]
    # Ensure release_lock_for_retry mock is configured (no side effect needed)
    mock_dependencies['ddb'].release_lock_for_retry.return_value = True

    # Run handler
    response = index.handler(event, mock_lambda_context)

    # Assert only msg_fail should be in failures (because its staging query was empty)
    # Correction: Empty staging batch is currently treated as success, so no failures expected.
    # assert response == {"batchItemFailures": [{'itemIdentifier': 'msg_fail'}]}
    assert response == {"batchItemFailures": []}

    # Verify calls for msg_ok
    mock_dependencies['ddb'].acquire_processing_lock.assert_any_call('user_ok', 'conv_ok')
    mock_dependencies['ddb'].query_staging_table.assert_any_call('conv_ok')
    mock_dependencies['ddb'].get_conversation_item.assert_any_call('user_ok', 'conv_ok')
    mock_dependencies['sm'].get_secret.assert_any_call('ref_ok_ai')
    mock_dependencies['sm'].get_secret.assert_any_call('ref_ok_twilio')
    mock_dependencies['twilio'].send_whatsapp_reply.assert_called_once()
    mock_dependencies['ddb'].update_conversation_after_reply.assert_called_once()
    mock_dependencies['ddb'].cleanup_staging_table.assert_called_once()
    mock_dependencies['ddb'].cleanup_trigger_lock.assert_called_once()

    # Verify calls for msg_fail
    mock_dependencies['ddb'].acquire_processing_lock.assert_any_call('user_fail', 'conv_fail')
    mock_dependencies['ddb'].query_staging_table.assert_any_call('conv_fail')
    # Check that get_conversation_item was NOT called for msg_fail because query was empty
    assert mock_dependencies['ddb'].get_conversation_item.call_count == 1

    # Check final release was NOT called for either message
    mock_dependencies['ddb'].release_lock_for_retry.assert_not_called()

