import pytest
from unittest.mock import patch, MagicMock
from twilio.base.exceptions import TwilioRestException

# Use the correct absolute import path based on project structure
from src.messaging_lambda.whatsapp.lambda_pkg.services import twilio_service

# --- Fixtures ---

@pytest.fixture
def mock_twilio_client():
    """Mocks the twilio Client and its messages.create method."""
    mock_client_instance = MagicMock()
    mock_message = MagicMock()
    mock_message.sid = "SM_mock_sid_123"
    mock_message.status = "queued" # or sent, delivered etc.
    mock_message.body = "Mocked message body sent"
    mock_client_instance.messages.create.return_value = mock_message

    # Patch the Client constructor in the twilio_service module
    with patch('src.messaging_lambda.whatsapp.lambda_pkg.services.twilio_service.Client') as mock_client_constructor:
        mock_client_constructor.return_value = mock_client_instance
        yield mock_client_instance

@pytest.fixture
def valid_creds():
    return {'twilio_account_sid': 'ACmockxxx', 'twilio_auth_token': 'mocktoken'}

# --- Test Cases ---

def test_send_whatsapp_reply_success(mock_twilio_client, valid_creds):
    """Test successful WhatsApp reply sending."""
    recipient = "+15551112222"
    sender = "+15558889999"
    body = "This is the reply message."

    status, result = twilio_service.send_whatsapp_reply(valid_creds, recipient, sender, body)

    assert status == twilio_service.TWILIO_SUCCESS
    assert result == {'message_sid': "SM_mock_sid_123", 'body': "Mocked message body sent"}
    mock_twilio_client.messages.create.assert_called_once_with(
        from_=f"whatsapp:{sender}",
        to=f"whatsapp:{recipient}",
        body=body
    )

def test_send_whatsapp_reply_missing_creds():
    """Test failure when credentials dictionary is missing keys."""
    status, result = twilio_service.send_whatsapp_reply(
        {'twilio_account_sid': 'ACmock'}, # Missing token
        "+1", "+2", "body"
    )
    assert status == twilio_service.TWILIO_INVALID_INPUT
    assert "Missing required arguments" in result['error_message']

def test_send_whatsapp_reply_missing_args():
    """Test failure when other arguments are missing."""
    creds = {'twilio_account_sid': 'ACmock', 'twilio_auth_token': 'mocktoken'}
    status1, result1 = twilio_service.send_whatsapp_reply(creds, "", "+2", "body") # Missing recipient
    status2, result2 = twilio_service.send_whatsapp_reply(creds, "+1", "", "body") # Missing sender
    status3, result3 = twilio_service.send_whatsapp_reply(creds, "+1", "+2", "")     # Missing body

    assert status1 == twilio_service.TWILIO_INVALID_INPUT
    assert status2 == twilio_service.TWILIO_INVALID_INPUT
    assert status3 == twilio_service.TWILIO_INVALID_INPUT
    assert "Missing required arguments" in result1['error_message']

@pytest.mark.parametrize("status_code, error_code, expected_status", [
    (400, 21211, twilio_service.TWILIO_NON_TRANSIENT_ERROR), # Invalid 'To' number
    (403, 20003, twilio_service.TWILIO_NON_TRANSIENT_ERROR), # Auth error
    (429, 20429, twilio_service.TWILIO_NON_TRANSIENT_ERROR), # Rate limit (treat non-transient here as per code)
    (500, 20500, twilio_service.TWILIO_TRANSIENT_ERROR),     # Internal Twilio error
    (503, 20503, twilio_service.TWILIO_TRANSIENT_ERROR),     # Service unavailable
    (300, 99999, twilio_service.TWILIO_NON_TRANSIENT_ERROR) # Unexpected status
])
def test_send_whatsapp_reply_twilio_rest_exception(mock_twilio_client, valid_creds, status_code, error_code, expected_status):
    """Test handling of various TwilioRestExceptions."""
    test_exception = TwilioRestException(status=status_code, uri="/Messages", msg=f"Test Error {error_code}", code=error_code)
    mock_twilio_client.messages.create.side_effect = test_exception

    status, result = twilio_service.send_whatsapp_reply(valid_creds, "+1", "+2", "body")

    assert status == expected_status
    assert f"Twilio API error sending message: Status={status_code}, Code={error_code}" in result['error_message']

def test_send_whatsapp_reply_unexpected_exception(mock_twilio_client, valid_creds):
    """Test handling of non-Twilio exceptions during send."""
    test_exception = ValueError("Something else broke")
    mock_twilio_client.messages.create.side_effect = test_exception

    status, result = twilio_service.send_whatsapp_reply(valid_creds, "+1", "+2", "body")

    assert status == twilio_service.TWILIO_TRANSIENT_ERROR # Assumes transient
    assert "Unexpected error sending message via Twilio" in result['error_message']
    assert str(test_exception) in result['error_message'] 