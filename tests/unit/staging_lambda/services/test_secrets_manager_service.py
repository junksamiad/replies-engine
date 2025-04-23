import pytest
import json
from unittest.mock import patch, MagicMock
from botocore.exceptions import ClientError

# Use the correct absolute import path based on project structure
from src.staging_lambda.lambda_pkg.services import secrets_manager_service

# --- Test Fixtures ---

@pytest.fixture(autouse=True)
def reset_secrets_manager_client():
    """Resets the global client before each test to ensure isolation."""
    secrets_manager_service.secrets_manager = None
    yield
    secrets_manager_service.secrets_manager = None

@pytest.fixture
def mock_sm_client():
    """Provides a mock Secrets Manager client."""
    mock_client = MagicMock()
    # Patch boto3.client to return our mock client
    with patch('src.staging_lambda.lambda_pkg.services.secrets_manager_service.boto3.client') as mock_boto_client:
        mock_boto_client.return_value = mock_client
        yield mock_client

# --- Test Cases ---

def test_get_token_success(mock_sm_client):
    """Test successful retrieval and extraction of the token."""
    secret_id = "arn:aws:secretsmanager:eu-north-1:123:secret:test-secret-123"
    secret_content = {"twilio_auth_token": "EXPECTED_TOKEN"}
    mock_sm_client.get_secret_value.return_value = {
        'SecretString': json.dumps(secret_content)
    }

    token = secrets_manager_service.get_twilio_auth_token(secret_id)

    assert token == "EXPECTED_TOKEN"
    mock_sm_client.get_secret_value.assert_called_once_with(SecretId=secret_id)

def test_get_token_missing_key_in_secret(mock_sm_client):
    """Test case where SecretString exists but lacks the 'twilio_auth_token' key."""
    secret_id = "secret-missing-key"
    secret_content = {"other_key": "some_value"}
    mock_sm_client.get_secret_value.return_value = {
        'SecretString': json.dumps(secret_content)
    }

    token = secrets_manager_service.get_twilio_auth_token(secret_id)

    assert token is None
    mock_sm_client.get_secret_value.assert_called_once_with(SecretId=secret_id)

def test_get_token_no_secret_string(mock_sm_client):
    """Test case where the response doesn't contain 'SecretString'."""
    secret_id = "secret-no-string"
    mock_sm_client.get_secret_value.return_value = {
        'SecretBinary': b'somebinarydata' # Example response without SecretString
    }

    token = secrets_manager_service.get_twilio_auth_token(secret_id)

    assert token is None
    mock_sm_client.get_secret_value.assert_called_once_with(SecretId=secret_id)

def test_get_token_invalid_json(mock_sm_client):
    """Test case where SecretString is not valid JSON."""
    secret_id = "secret-bad-json"
    mock_sm_client.get_secret_value.return_value = {
        'SecretString': "this is { not valid json"
    }

    token = secrets_manager_service.get_twilio_auth_token(secret_id)

    assert token is None
    mock_sm_client.get_secret_value.assert_called_once_with(SecretId=secret_id)

# Parametrize testing for various boto3 client exceptions
@pytest.mark.parametrize(
    "exception_type, error_code",
    [
        ("ResourceNotFoundException", "ResourceNotFoundException"),
        ("InvalidParameterException", "InvalidParameterException"),
        ("InvalidRequestException", "InvalidRequestException"),
        ("DecryptionFailure", "DecryptionFailure"),
        ("InternalServiceError", "InternalServiceError"),
    ]
)
def test_get_token_client_exceptions(mock_sm_client, exception_type, error_code):
    """Test handling of various ClientErrors raised by get_secret_value."""
    secret_id = f"secret-fail-{error_code.lower()}"
    mock_sm_client.get_secret_value.side_effect = ClientError(
        error_response={'Error': {'Code': error_code, 'Message': 'Test error'}},
        operation_name='GetSecretValue'
    )

    token = secrets_manager_service.get_twilio_auth_token(secret_id)

    assert token is None
    mock_sm_client.get_secret_value.assert_called_once_with(SecretId=secret_id)

def test_get_token_unexpected_exception(mock_sm_client):
    """Test handling of unexpected non-ClientError exceptions."""
    secret_id = "secret-fail-unexpected"
    mock_sm_client.get_secret_value.side_effect = ValueError("Something completely unexpected went wrong")

    token = secrets_manager_service.get_twilio_auth_token(secret_id)

    assert token is None
    mock_sm_client.get_secret_value.assert_called_once_with(SecretId=secret_id)

def test_client_initialization(reset_secrets_manager_client): # Use the reset fixture
    """Test that the client is initialized only once."""
    with patch('src.staging_lambda.lambda_pkg.services.secrets_manager_service.boto3.client') as mock_boto_client:
        mock_boto_client.return_value = MagicMock()

        # Call the function multiple times
        secrets_manager_service.get_twilio_auth_token("id1")
        secrets_manager_service.get_twilio_auth_token("id2")
        secrets_manager_service.get_twilio_auth_token("id3")

        # Assert boto3.client was called only once
        mock_boto_client.assert_called_once() 