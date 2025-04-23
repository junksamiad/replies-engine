import pytest
import os
from unittest.mock import patch

# Use the correct absolute import path based on project structure
# We need to import the module itself to test its logic
from src.staging_lambda.lambda_pkg.core import routing

# --- Test Fixtures ---

@pytest.fixture(autouse=True)
def mock_env_vars(monkeypatch):
    """Sets required environment variables for the routing module."""
    monkeypatch.setenv("HANDOFF_QUEUE_URL", "mock_handoff_url")
    monkeypatch.setenv("WHATSAPP_QUEUE_URL", "mock_whatsapp_url")
    monkeypatch.setenv("SMS_QUEUE_URL", "mock_sms_url")
    monkeypatch.setenv("EMAIL_QUEUE_URL", "mock_email_url")
    # Reload the module to pick up the mocked environment variables
    # This is crucial because the URLs are read at the module level upon import
    import importlib
    importlib.reload(routing)

@pytest.fixture
def base_context():
    """Provides a base context for default routing."""
    return {
        'channel_type': 'whatsapp',
        'conversation_id': 'conv_123',
        'recipient_tel': '+15551234567', # Example field used in routing
        'recipient_email': 'test@example.com', # Example field used in routing
        # Flags default to None/False
        'auto_queue_reply_message': False,
        'auto_queue_reply_message_from_number': [],
        'auto_queue_reply_message_from_email': [],
        # 'hand_off_to_human': False # Currently commented out in source
    }

# --- Test Cases ---

def test_routing_default_whatsapp(base_context):
    """Test default routing to WhatsApp queue."""
    context = base_context
    context['channel_type'] = 'whatsapp'
    target_url = routing.determine_target_queue(context)
    assert target_url == "mock_whatsapp_url"

def test_routing_default_sms(base_context):
    """Test default routing to SMS queue."""
    context = base_context
    context['channel_type'] = 'sms'
    target_url = routing.determine_target_queue(context)
    assert target_url == "mock_sms_url"

def test_routing_default_email(base_context):
    """Test default routing to Email queue."""
    context = base_context
    context['channel_type'] = 'email'
    target_url = routing.determine_target_queue(context)
    assert target_url == "mock_email_url"

def test_routing_unknown_channel(base_context):
    """Test routing failure for unknown channel type."""
    context = base_context
    context['channel_type'] = 'unknown'
    target_url = routing.determine_target_queue(context)
    assert target_url is None

def test_routing_auto_queue_flag(base_context):
    """Test routing to handoff queue based on auto_queue_reply_message flag."""
    context = base_context
    context['channel_type'] = 'whatsapp' # Channel doesn't matter for this flag
    context['auto_queue_reply_message'] = True
    target_url = routing.determine_target_queue(context)
    assert target_url == "mock_handoff_url"

def test_routing_auto_queue_number_match(base_context):
    """Test routing to handoff queue based on matching recipient number."""
    context = base_context
    context['channel_type'] = 'whatsapp'
    context['recipient_tel'] = '+15559998888'
    context['auto_queue_reply_message_from_number'] = ['+15551112222', '+15559998888']
    target_url = routing.determine_target_queue(context)
    assert target_url == "mock_handoff_url"

def test_routing_auto_queue_number_no_match(base_context):
    """Test default routing when recipient number is not in the auto_queue list."""
    context = base_context
    context['channel_type'] = 'whatsapp'
    context['recipient_tel'] = '+15559998888'
    context['auto_queue_reply_message_from_number'] = ['+15551112222', '+15553334444']
    target_url = routing.determine_target_queue(context)
    assert target_url == "mock_whatsapp_url"

def test_routing_auto_queue_number_list_none(base_context):
    """Test default routing when auto_queue_reply_message_from_number is None."""
    context = base_context
    context['channel_type'] = 'whatsapp'
    context['recipient_tel'] = '+15559998888'
    context['auto_queue_reply_message_from_number'] = None # Test None case
    target_url = routing.determine_target_queue(context)
    assert target_url == "mock_whatsapp_url"

def test_routing_auto_queue_number_missing_recipient(base_context):
    """Test default routing when recipient_tel is missing."""
    context = base_context
    context['channel_type'] = 'whatsapp'
    del context['recipient_tel']
    context['auto_queue_reply_message_from_number'] = ['+15551112222', '+15559998888']
    target_url = routing.determine_target_queue(context)
    assert target_url == "mock_whatsapp_url"

def test_routing_auto_queue_email_match(base_context):
    """Test routing to handoff queue based on matching recipient email."""
    context = base_context
    context['channel_type'] = 'email'
    context['recipient_email'] = 'user@example.com'
    context['auto_queue_reply_message_from_email'] = ['admin@example.com', 'user@example.com']
    target_url = routing.determine_target_queue(context)
    assert target_url == "mock_handoff_url"

def test_routing_auto_queue_email_no_match(base_context):
    """Test default routing when recipient email is not in the auto_queue list."""
    context = base_context
    context['channel_type'] = 'email'
    context['recipient_email'] = 'user@example.com'
    context['auto_queue_reply_message_from_email'] = ['admin@example.com', 'support@example.com']
    target_url = routing.determine_target_queue(context)
    assert target_url == "mock_email_url"

def test_routing_auto_queue_email_list_none(base_context):
    """Test default routing when auto_queue_reply_message_from_email is None."""
    context = base_context
    context['channel_type'] = 'email'
    context['recipient_email'] = 'user@example.com'
    context['auto_queue_reply_message_from_email'] = None # Test None case
    target_url = routing.determine_target_queue(context)
    assert target_url == "mock_email_url"

def test_routing_auto_queue_email_missing_recipient(base_context):
    """Test default routing when recipient_email is missing."""
    context = base_context
    context['channel_type'] = 'email'
    del context['recipient_email']
    context['auto_queue_reply_message_from_email'] = ['admin@example.com', 'user@example.com']
    target_url = routing.determine_target_queue(context)
    assert target_url == "mock_email_url"

def test_routing_priority_flags_over_default(base_context):
    """Test that auto-queue flags take priority over default channel routing."""
    # Test flag override
    context_flag = base_context
    context_flag['channel_type'] = 'whatsapp'
    context_flag['auto_queue_reply_message'] = True
    target_url_flag = routing.determine_target_queue(context_flag)
    assert target_url_flag == "mock_handoff_url"

    # Test number list override
    context_num = base_context
    context_num['channel_type'] = 'whatsapp'
    context_num['recipient_tel'] = '+15559998888'
    context_num['auto_queue_reply_message_from_number'] = ['+15559998888']
    target_url_num = routing.determine_target_queue(context_num)
    assert target_url_num == "mock_handoff_url"

    # Test email list override
    context_email = base_context
    context_email['channel_type'] = 'email'
    context_email['recipient_email'] = 'user@example.com'
    context_email['auto_queue_reply_message_from_email'] = ['user@example.com']
    target_url_email = routing.determine_target_queue(context_email)
    assert target_url_email == "mock_handoff_url"

# Test loading failure if environment variables are missing
# This requires manipulating the import/reload mechanism

# def test_routing_env_var_missing(monkeypatch):
#     """Test that the module raises EnvironmentError if a required URL is missing."""
#     # Remove one of the mocked env vars BEFORE importing/reloading routing
#     monkeypatch.setenv("HANDOFF_QUEUE_URL", "mock_handoff_url")
#     monkeypatch.setenv("WHATSAPP_QUEUE_URL", "mock_whatsapp_url")
#     # monkeypatch.setenv("SMS_QUEUE_URL", "mock_sms_url") # MISSING
#     monkeypatch.setenv("EMAIL_QUEUE_URL", "mock_email_url")
#
#     import importlib
#     with pytest.raises(EnvironmentError) as excinfo:
#         importlib.reload(routing)
#     assert "Missing required environment variables" in str(excinfo.value)
#     assert "SMS_QUEUE_URL" in str(excinfo.value)
#
#     # IMPORTANT: Reset env vars and reload again to not affect other tests
#     monkeypatch.setenv("SMS_QUEUE_URL", "mock_sms_url")
#     importlib.reload(routing) 