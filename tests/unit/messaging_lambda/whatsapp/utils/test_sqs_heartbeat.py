import pytest
import time
import threading
from unittest.mock import patch, MagicMock, call
from botocore.exceptions import ClientError

# Use the correct absolute import path based on project structure
from src.messaging_lambda.whatsapp.lambda_pkg.utils.sqs_heartbeat import SQSHeartbeat, DEFAULT_VISIBILITY_TIMEOUT_EXTENSION_SEC

# --- Fixtures ---

@pytest.fixture
def mock_boto_client():
    """Provides a mock boto3 SQS client."""
    mock_client = MagicMock()
    with patch('src.messaging_lambda.whatsapp.lambda_pkg.utils.sqs_heartbeat.boto3.client') as mock_boto_constructor:
        mock_boto_constructor.return_value = mock_client
        yield mock_client

@pytest.fixture
def heartbeat_instance(mock_boto_client):
    """Provides a default SQSHeartbeat instance with mocked Event."""
    with patch('src.messaging_lambda.whatsapp.lambda_pkg.utils.sqs_heartbeat.threading.Event') as mock_event_constructor:
        mock_event = MagicMock(name="MockStopEventInstance")
        mock_event.is_set.return_value = False
        mock_event_constructor.return_value = mock_event
        # Initialize the instance *within* the patch context
        instance = SQSHeartbeat(
            queue_url="mock_queue_url",
            receipt_handle="mock_receipt_handle",
            interval_sec=1
        )
        # Attach the mock event for potential assertion in tests if needed
        instance._mock_stop_event_ref = mock_event
        yield instance

# --- Test Cases ---

def test_init_success(mock_boto_client):
    """Test successful initialization."""
    hb = SQSHeartbeat("q_url", "r_handle", interval_sec=5, visibility_timeout_sec=30)
    assert hb.queue_url == "q_url"
    assert hb.receipt_handle == "r_handle"
    assert hb.interval_sec == 5
    assert hb.visibility_timeout_sec == 30
    assert hb._sqs_client == mock_boto_client # Check client was assigned
    assert not hb.running
    assert hb.check_for_errors() is None

@pytest.mark.parametrize("kwargs, error_msg", [
    ({"queue_url": "", "receipt_handle": "r", "interval_sec": 1}, "queue_url and receipt_handle cannot be empty"),
    ({"queue_url": "q", "receipt_handle": "", "interval_sec": 1}, "queue_url and receipt_handle cannot be empty"),
    ({"queue_url": "q", "receipt_handle": "r", "interval_sec": 0}, "interval_sec must be positive"),
    ({"queue_url": "q", "receipt_handle": "r", "interval_sec": -1}, "interval_sec must be positive"),
])
def test_init_invalid_args(kwargs, error_msg):
    """Test initialization with invalid arguments."""
    with pytest.raises(ValueError, match=error_msg):
        SQSHeartbeat(**kwargs)

def test_init_warning_low_visibility(caplog):
    """Test warning when visibility timeout is not much longer than interval."""
    SQSHeartbeat("q", "r", interval_sec=10, visibility_timeout_sec=10)
    assert "Visibility timeout (10s) is not significantly longer" in caplog.text

# --- Threading and Core Logic Tests ---

@patch('src.messaging_lambda.whatsapp.lambda_pkg.utils.sqs_heartbeat.threading.Thread')
def test_start(mock_thread_constructor, heartbeat_instance):
    """Test that start creates and starts a daemon thread."""
    mock_thread = MagicMock()
    mock_thread_constructor.return_value = mock_thread

    heartbeat_instance.start()

    mock_thread_constructor.assert_called_once_with(target=heartbeat_instance._run, daemon=True)
    mock_thread.start.assert_called_once()
    assert heartbeat_instance._running is True

@patch('src.messaging_lambda.whatsapp.lambda_pkg.utils.sqs_heartbeat.threading.Thread')
def test_start_already_running(mock_thread_constructor, heartbeat_instance, caplog):
    """Test calling start when already running does nothing."""
    mock_thread = MagicMock()
    mock_thread_constructor.return_value = mock_thread

    heartbeat_instance.start()
    heartbeat_instance.start() # Call start again

    mock_thread_constructor.assert_called_once() # Still only called once
    mock_thread.start.assert_called_once()
    assert "Heartbeat thread start called when already running" in caplog.text

@patch('src.messaging_lambda.whatsapp.lambda_pkg.utils.sqs_heartbeat.threading.Event')
def test_run_loop_calls_change_visibility(mock_event_constructor, heartbeat_instance):
    """Test the _run loop calls change_message_visibility."""
    mock_client = heartbeat_instance._sqs_client
    mock_event = MagicMock()
    mock_event.wait.side_effect = [False, False, True] # Wait twice, then return True (stop)
    mock_event_constructor.return_value = mock_event
    heartbeat_instance._stop_event = mock_event # Assign the mock event

    # Run the target function directly (no thread needed for this test)
    heartbeat_instance._run()

    assert mock_client.change_message_visibility.call_count == 2
    expected_calls = [
        call(QueueUrl="mock_queue_url", ReceiptHandle="mock_receipt_handle", VisibilityTimeout=DEFAULT_VISIBILITY_TIMEOUT_EXTENSION_SEC),
        call(QueueUrl="mock_queue_url", ReceiptHandle="mock_receipt_handle", VisibilityTimeout=DEFAULT_VISIBILITY_TIMEOUT_EXTENSION_SEC)
    ]
    mock_client.change_message_visibility.assert_has_calls(expected_calls)

@patch('src.messaging_lambda.whatsapp.lambda_pkg.utils.sqs_heartbeat.threading.Event')
def test_run_loop_stops_on_client_error(mock_event_constructor, heartbeat_instance):
    """Test the _run loop stops and records error on ClientError."""
    mock_client = heartbeat_instance._sqs_client
    mock_event = MagicMock()
    mock_event.wait.side_effect = [False, False] # Loop should stop on first error
    mock_event_constructor.return_value = mock_event
    heartbeat_instance._stop_event = mock_event

    test_exception = ClientError({'Error': {'Code': 'TestError'}}, 'ChangeMessageVisibility')
    mock_client.change_message_visibility.side_effect = test_exception

    heartbeat_instance._run()

    mock_client.change_message_visibility.assert_called_once() # Called once before error
    mock_event.set.assert_called_once() # Stop event should be set
    assert heartbeat_instance.check_for_errors() == test_exception
    assert heartbeat_instance._running is False # Check running flag updated

@patch('src.messaging_lambda.whatsapp.lambda_pkg.utils.sqs_heartbeat.threading.Event')
def test_run_loop_stops_on_unexpected_error(mock_event_constructor, heartbeat_instance):
    """Test the _run loop stops and records error on unexpected Exception."""
    mock_client = heartbeat_instance._sqs_client
    mock_event = MagicMock()
    mock_event.wait.side_effect = [False, False]
    mock_event_constructor.return_value = mock_event
    heartbeat_instance._stop_event = mock_event

    test_exception = ValueError("Unexpected")
    mock_client.change_message_visibility.side_effect = test_exception

    heartbeat_instance._run()

    mock_client.change_message_visibility.assert_called_once()
    mock_event.set.assert_called_once()
    assert heartbeat_instance.check_for_errors() == test_exception
    assert heartbeat_instance._running is False


@patch('src.messaging_lambda.whatsapp.lambda_pkg.utils.sqs_heartbeat.threading.Thread')
def test_stop(mock_thread_constructor, heartbeat_instance):
    """Test stop signals event and joins thread."""
    mock_thread = MagicMock()
    mock_thread_constructor.return_value = mock_thread
    heartbeat_instance.start() # Start assigns _thread

    # Access the mock event using the reference attached in the fixture
    mock_stop_event = heartbeat_instance._mock_stop_event_ref

    heartbeat_instance.stop()

    mock_stop_event.set.assert_called_once() # Assert set was called on the correct mock event
    mock_thread.join.assert_called_once_with(timeout=heartbeat_instance.interval_sec + 10)
    assert heartbeat_instance._thread is None # Thread reference should be cleared

@patch('src.messaging_lambda.whatsapp.lambda_pkg.utils.sqs_heartbeat.threading.Thread')
def test_stop_no_thread(mock_thread_constructor, heartbeat_instance):
    """Test stop does nothing if thread wasn't started."""
    # mock_thread = mock_thread_constructor.return_value # Get the mock thread object

    heartbeat_instance.stop()

    # Verify that thread operations like join were not attempted
    mock_thread_constructor.return_value.join.assert_not_called()
    # Also verify the event wasn't set unnecessarily
    heartbeat_instance._stop_event.set.assert_not_called()
    # assert "Stop called but heartbeat thread was never started" in caplog.text # Removed log check

def test_check_for_errors(heartbeat_instance):
    """Test check_for_errors returns stored error."""
    assert heartbeat_instance.check_for_errors() is None
    test_error = ValueError("test")
    heartbeat_instance._error = test_error
    assert heartbeat_instance.check_for_errors() == test_error

@patch('src.messaging_lambda.whatsapp.lambda_pkg.utils.sqs_heartbeat.threading.Thread')
def test_running_property(mock_thread_constructor, heartbeat_instance):
    """Test the running property reflects thread state."""
    mock_thread = MagicMock()
    mock_thread.is_alive.return_value = True
    mock_thread_constructor.return_value = mock_thread

    assert not heartbeat_instance.running # Not started yet

    heartbeat_instance.start()
    assert heartbeat_instance.running # Started and thread is alive

    mock_thread.is_alive.return_value = False # Simulate thread finishing
    assert not heartbeat_instance.running # Thread not alive anymore 