import pytest

# Use the correct absolute import path based on project structure
from src.staging_lambda.lambda_pkg.core.validation import validate_conversation_rules

# --- Test Fixtures ---

@pytest.fixture
def valid_context_base():
    """Provides a base context object that should pass validation."""
    return {
        'project_status': 'active',
        'channel_type': 'whatsapp',
        'allowed_channels': ['whatsapp', 'sms'],
        'conversation_status': 'template_sent', # Example non-locked status
        'conversation_id': 'conv_123',
        # Add other fields if needed by future validation rules
    }

# --- Test Cases ---

def test_validate_rules_success(valid_context_base):
    """Test successful validation with valid context."""
    result = validate_conversation_rules(valid_context_base)
    assert result['valid'] is True
    assert 'error_code' not in result
    assert result['data'] == valid_context_base # Check context passthrough

def test_validate_rules_inactive_project(valid_context_base):
    """Test failure when project_status is not active."""
    context = valid_context_base
    context['project_status'] = 'inactive'
    result = validate_conversation_rules(context)
    assert result['valid'] is False
    assert result['error_code'] == 'PROJECT_INACTIVE'
    assert "Project is not active" in result['message']

def test_validate_rules_inactive_project_missing(valid_context_base):
    """Test failure when project_status is missing (treated as inactive)."""
    context = valid_context_base
    del context['project_status']
    result = validate_conversation_rules(context)
    assert result['valid'] is False
    assert result['error_code'] == 'PROJECT_INACTIVE'
    assert "(status: None)" in result['message'] # Check how missing status is reported

def test_validate_rules_disallowed_channel(valid_context_base):
    """Test failure when channel_type is not in allowed_channels."""
    context = valid_context_base
    context['channel_type'] = 'email' # email is not in allowed_channels fixture
    result = validate_conversation_rules(context)
    assert result['valid'] is False
    assert result['error_code'] == 'CHANNEL_NOT_ALLOWED'
    assert "Channel 'email' is not allowed" in result['message']

def test_validate_rules_allowed_channels_missing(valid_context_base):
    """Test failure when allowed_channels is missing (channel effectively not allowed)."""
    context = valid_context_base
    context['channel_type'] = 'whatsapp'
    del context['allowed_channels']
    result = validate_conversation_rules(context)
    assert result['valid'] is False
    assert result['error_code'] == 'CHANNEL_NOT_ALLOWED'
    assert "Channel 'whatsapp' is not allowed" in result['message'] # Checks against default empty list

def test_validate_rules_conversation_locked(valid_context_base):
    """Test failure when conversation_status is 'processing_reply'."""
    context = valid_context_base
    context['conversation_status'] = 'processing_reply'
    result = validate_conversation_rules(context)
    assert result['valid'] is False
    assert result['error_code'] == 'CONVERSATION_LOCKED'
    assert "Conversation is currently processing" in result['message']

def test_validate_rules_conversation_status_missing(valid_context_base):
    """Test success when conversation_status is missing (not locked)."""
    context = valid_context_base
    del context['conversation_status']
    result = validate_conversation_rules(context)
    assert result['valid'] is True # Missing status is not considered locked 