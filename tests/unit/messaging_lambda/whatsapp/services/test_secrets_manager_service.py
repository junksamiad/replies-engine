import pytest
import json
from unittest.mock import patch, MagicMock
from botocore.exceptions import ClientError

# Use the correct absolute import path based on project structure
from src.messaging_lambda.whatsapp.lambda_pkg.services import secrets_manager_service

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
    with patch('src.messaging_lambda.whatsapp.lambda_pkg.services.secrets_manager_service.boto3.client') as mock_boto_client:
        mock_boto_client.return_value = mock_client
        yield mock_client

# --- Test Cases ---

def test_get_secret_success(mock_sm_client):
    """Test successful retrieval and parsing of a JSON secret."""
    secret_id = "arn:aws:secretsmanager:eu-north-1:123:secret:test-secret-json"
    expected_data = {"api_key": "12345", "endpoint": "https://example.com"}
    mock_sm_client.get_secret_value.return_value = {
        'SecretString': json.dumps(expected_data)
    }

    status, data = secrets_manager_service.get_secret(secret_id)

    assert status == secrets_manager_service.SECRET_SUCCESS
    assert data == expected_data
    mock_sm_client.get_secret_value.assert_called_once_with(SecretId=secret_id)

def test_get_secret_invalid_input():
    """Test calling get_secret with an empty secret_id."""
    status, data = secrets_manager_service.get_secret("")
    assert status == secrets_manager_service.SECRET_INVALID_INPUT
    assert data is None

def test_get_secret_not_found(mock_sm_client):
    """Test handling ResourceNotFoundException."""
    secret_id = "secret-does-not-exist"
    mock_sm_client.get_secret_value.side_effect = ClientError(
        error_response={'Error': {'Code': 'ResourceNotFoundException', 'Message': 'Not found'}},
        operation_name='GetSecretValue'
    )
    status, data = secrets_manager_service.get_secret(secret_id)
    assert status == secrets_manager_service.SECRET_NOT_FOUND
    assert data is None

def test_get_secret_transient_error(mock_sm_client):
    """Test handling InternalServiceError (mapped to transient)."""
    secret_id = "secret-transient-fail"
    mock_sm_client.get_secret_value.side_effect = ClientError(
        error_response={'Error': {'Code': 'InternalServiceError', 'Message': 'Server issue'}},
        operation_name='GetSecretValue'
    )
    status, data = secrets_manager_service.get_secret(secret_id)
    assert status == secrets_manager_service.SECRET_TRANSIENT_ERROR
    assert data is None

@pytest.mark.parametrize("error_code", [
    'DecryptionFailure',
    'AccessDeniedException',
    'InvalidParameterException',
    'InvalidRequestException',
    'SomeOtherAWSError' # Test the default case
])
def test_get_secret_permanent_error(mock_sm_client, error_code):
    """Test handling of various errors mapped to PERMANENT_ERROR."""
    secret_id = f"secret-permanent-fail-{error_code}"
    mock_sm_client.get_secret_value.side_effect = ClientError(
        error_response={'Error': {'Code': error_code, 'Message': 'Permanent issue'}},
        operation_name='GetSecretValue'
    )
    status, data = secrets_manager_service.get_secret(secret_id)
    assert status == secrets_manager_service.SECRET_PERMANENT_ERROR
    assert data is None

def test_get_secret_invalid_json(mock_sm_client):
    """Test handling when SecretString is not valid JSON."""
    secret_id = "secret-bad-json"
    mock_sm_client.get_secret_value.return_value = {
        'SecretString': "this is { not valid json"
    }
    status, data = secrets_manager_service.get_secret(secret_id)
    assert status == secrets_manager_service.SECRET_PERMANENT_ERROR
    assert data is None

def test_get_secret_json_not_dict(mock_sm_client):
    """Test handling when SecretString is valid JSON but not a dictionary."""
    secret_id = "secret-json-list"
    mock_sm_client.get_secret_value.return_value = {
        'SecretString': json.dumps(["list", "not", "dict"])
    }
    status, data = secrets_manager_service.get_secret(secret_id)
    assert status == secrets_manager_service.SECRET_PERMANENT_ERROR
    assert data is None

def test_get_secret_binary_secret(mock_sm_client):
    """Test handling when the secret is binary instead of string."""
    secret_id = "secret-binary"
    mock_sm_client.get_secret_value.return_value = {
        'SecretBinary': b'somebinarydata'
    }
    status, data = secrets_manager_service.get_secret(secret_id)
    assert status == secrets_manager_service.SECRET_PERMANENT_ERROR
    assert data is None

def test_get_secret_unexpected_exception(mock_sm_client):
    """Test handling of unexpected non-ClientError exceptions."""
    secret_id = "secret-fail-unexpected"
    mock_sm_client.get_secret_value.side_effect = ValueError("Something completely unexpected")
    status, data = secrets_manager_service.get_secret(secret_id)
    assert status == secrets_manager_service.SECRET_PERMANENT_ERROR
    assert data is None

def test_get_secret_client_init_failure():
    """Test handling when the boto3 client fails to initialize."""
    with patch('src.messaging_lambda.whatsapp.lambda_pkg.services.secrets_manager_service.boto3.client') as mock_boto_client:
        mock_boto_client.side_effect = RuntimeError("Init failed")
        # Need to reset the global for this test
        secrets_manager_service.secrets_manager = None
        status, data = secrets_manager_service.get_secret("some-id")
        secrets_manager_service.secrets_manager = None # Reset after test

    assert status == secrets_manager_service.SECRET_INIT_ERROR
    assert data is None

def test_client_initialization(reset_secrets_manager_client): # Use the reset fixture
    """Test that the client is initialized only once."""
    with patch('src.messaging_lambda.whatsapp.lambda_pkg.services.secrets_manager_service.boto3.client') as mock_boto_client:
        mock_boto_client.return_value = MagicMock()

        # Call the function multiple times
        secrets_manager_service.get_secret("id1")
        secrets_manager_service.get_secret("id2")
        secrets_manager_service.get_secret("id3")

        # Assert boto3.client was called only once
        mock_boto_client.assert_called_once() 