import json
import pytest

# Use the correct absolute import path based on project structure
from src.staging_lambda.lambda_pkg.utils import response_builder

# --- Test Cases for Success Responses ---

def test_create_success_response_json_no_data():
    """Test basic JSON success response."""
    response = response_builder.create_success_response_json(message="Operation successful")

    assert response['statusCode'] == 200
    assert response['headers']['Content-Type'] == 'application/json'
    assert 'Access-Control-Allow-Origin' in response['headers'] # Check CORS header presence
    body = json.loads(response['body'])
    assert body['status'] == 'success'
    assert body['message'] == "Operation successful"
    assert 'data' not in body

def test_create_success_response_json_with_data():
    """Test JSON success response with additional data."""
    test_data = {"id": 123, "value": "test"}
    response = response_builder.create_success_response_json(data=test_data)

    assert response['statusCode'] == 200
    body = json.loads(response['body'])
    assert body['status'] == 'success'
    assert body['message'] == "Success" # Default message
    assert body['data'] == test_data

def test_create_success_response_twiml_default():
    """Test default empty TwiML success response."""
    response = response_builder.create_success_response_twiml()

    assert response['statusCode'] == 200
    assert response['headers']['Content-Type'] == 'text/xml'
    assert 'Access-Control-Allow-Origin' in response['headers']
    assert response['body'] == "<?xml version='1.0' encoding='UTF-8'?><Response></Response>"

def test_create_success_response_twiml_custom():
    """Test TwiML success response with custom body."""
    custom_twiml = "<?xml version='1.0' encoding='UTF-8'?><Response><Message>Custom</Message></Response>"
    response = response_builder.create_success_response_twiml(twiml_body=custom_twiml)

    assert response['statusCode'] == 200
    assert response['headers']['Content-Type'] == 'text/xml'
    assert response['body'] == custom_twiml

# --- Test Cases for Error Responses ---

@pytest.mark.parametrize(
    "error_code, message, expected_status_code",
    [
        ('INVALID_INPUT', "Bad input", 400),
        ('MISSING_REQUIRED_FIELD', "Field missing", 400),
        ('UNKNOWN_CHANNEL', "No such channel", 400),
        ('PARSING_ERROR', "Cannot parse", 400),
        ('VALIDATION_FAILED', "Failed rules", 400),
        ('PROJECT_INACTIVE', "Project closed", 403),
        ('CHANNEL_NOT_ALLOWED', "Channel blocked", 403),
        ('CONVERSATION_NOT_FOUND', "Not found", 404),
        ('CONVERSATION_LOCKED', "Locked", 409),
        ('DB_QUERY_ERROR', "DB query failed", 500),
        ('QUEUE_ERROR', "SQS failed", 500),
        ('INTERNAL_ERROR', "Something broke", 500),
        ('CONFIGURATION_ERROR', "Bad config", 500),
        ('DB_TRANSIENT_ERROR', "DB temp issue", 503),
        ('UNKNOWN_ERROR_CODE', "Unknown issue", 500), # Test default status code hint
    ]
)
def test_create_error_response_codes(error_code, message, expected_status_code):
    """Test mapping of various error codes to HTTP status codes."""
    response = response_builder.create_error_response(error_code, message)

    assert response['statusCode'] == expected_status_code
    assert response['headers']['Content-Type'] == 'application/json'
    assert 'Access-Control-Allow-Origin' in response['headers']
    body = json.loads(response['body'])
    assert body['status'] == 'error'
    assert body['error_code'] == error_code
    assert body['message'] == message

def test_create_error_response_default_hint():
    """Test default status code hint when error code is not mapped."""
    response_4xx = response_builder.create_error_response("CUSTOM_CLIENT_ERR", "Custom msg", status_code_hint=418)
    response_5xx = response_builder.create_error_response("CUSTOM_SERVER_ERR", "Custom msg", status_code_hint=501)

    assert response_4xx['statusCode'] == 418
    assert response_5xx['statusCode'] == 501

def test_create_twiml_error_response():
    """Test TwiML error response structure (always 200 OK)."""
    error_message = "This is a TwiML error message."
    response = response_builder.create_twiml_error_response(error_message)

    assert response['statusCode'] == 200 # Crucially, this is 200
    assert response['headers']['Content-Type'] == 'text/xml'
    assert 'Access-Control-Allow-Origin' in response['headers']
    expected_body = f"<?xml version='1.0' encoding='UTF-8'?><Response><Message>{error_message}</Message></Response>"
    assert response['body'] == expected_body 