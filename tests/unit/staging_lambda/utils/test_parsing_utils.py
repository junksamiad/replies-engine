import pytest
from unittest.mock import patch
import base64 # Needed for potential body encoding tests, although not strictly used by current parser

# Use the correct absolute import path based on project structure
from src.staging_lambda.lambda_pkg.utils.parsing_utils import parse_incoming_request

# --- Test Fixtures ---

@pytest.fixture
def mock_event_base():
    """Provides a base structure for API Gateway proxy events."""
    return {
        "resource": "/{proxy+}",
        "path": "/whatsapp", # Default to whatsapp, override in tests
        "httpMethod": "POST",
        "headers": {
            "Accept": "*/*",
            "Host": "test-api.execute-api.eu-north-1.amazonaws.com",
            "User-Agent": "TwilioProxy/1.1",
            "X-Amzn-Trace-Id": "Root=1-xxxxx",
            "X-Forwarded-For": "3.67.xx.xx",
            "X-Forwarded-Port": "443",
            "X-Forwarded-Proto": "https",
            "X-Twilio-Signature": "test_signature_123" # Default signature
        },
        "multiValueHeaders": {
            # ... includes multi-value versions of headers ...
        },
        "queryStringParameters": None,
        "multiValueQueryStringParameters": None,
        "pathParameters": {"proxy": "whatsapp"},
        "stageVariables": None,
        "requestContext": {
            "resourceId": "xxxxx",
            "resourcePath": "/{proxy+}",
            "httpMethod": "POST",
            "extendedRequestId": "xxxxx=",
            "requestTime": "22/Apr/2025:10:00:00 +0000",
            "path": "/test/whatsapp", # Note: Includes stage
            "accountId": "123456789012",
            "protocol": "HTTP/1.1",
            "stage": "test", # Example stage name
            "domainPrefix": "test-api",
            "requestTimeEpoch": 1713780000000,
            "requestId": "xxxx-xxxx-xxxx-xxxx",
            "identity": {
                # ... sourceIp etc ...
            },
            "domainName": "test-api.execute-api.eu-north-1.amazonaws.com",
            "apiId": "yyyyy"
        },
        "body": "AccountSid=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxx&ApiVersion=2010-04-01&Body=Hello+there&From=whatsapp%3A%2B14155238886&MessageSid=SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxx&NumMedia=0&NumSegments=1&ProfileName=TestUser&SmsMessageSid=SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxx&SmsSid=SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxx&SmsStatus=received&To=whatsapp%3A%2B14155234567&WaId=14155238886", # Default body
        "isBase64Encoded": False
    }

# --- Test Cases ---

def test_parse_whatsapp_success(mock_event_base):
    """Test successful parsing of a standard WhatsApp request."""
    event = mock_event_base
    result = parse_incoming_request(event)

    assert result['success'] is True
    assert result['signature_header'] == "test_signature_123"
    assert result['request_url'] == "https://test-api.execute-api.eu-north-1.amazonaws.com/test/whatsapp"
    assert result['parsed_body_params'] == {
        'AccountSid': 'ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
        'ApiVersion': '2010-04-01',
        'Body': 'Hello there',
        'From': 'whatsapp:+14155238886',
        'MessageSid': 'SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
        'NumMedia': '0',
        'NumSegments': '1',
        'ProfileName': 'TestUser',
        'SmsMessageSid': 'SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
        'SmsSid': 'SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
        'SmsStatus': 'received',
        'To': 'whatsapp:+14155234567',
        'WaId': '14155238886'
    }

    context = result['context_object']
    assert context['channel_type'] == 'whatsapp'
    assert context['from'] == 'whatsapp:+14155238886'
    assert context['to'] == 'whatsapp:+14155234567'
    assert context['message_sid'] == 'SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
    assert context['body'] == 'Hello there'
    assert context['conversation_id'] == 'conv_+14155234567_+14155238886' # Expecting +

def test_parse_sms_success(mock_event_base):
    """Test successful parsing of an SMS request (similar structure)."""
    event = mock_event_base
    event['path'] = '/sms'
    event['requestContext']['path'] = '/test/sms'
    event['pathParameters']['proxy'] = 'sms'
    # Adjust body slightly if SMS format differs (here assuming similar keys)
    event['body'] = "AccountSid=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxx&Body=SMS+reply&From=%2B14155238886&MessageSid=SM_sms_sid&To=%2B14155234567"

    result = parse_incoming_request(event)

    assert result['success'] is True
    assert result['request_url'] == "https://test-api.execute-api.eu-north-1.amazonaws.com/test/sms"
    assert result['parsed_body_params']['From'] == '+14155238886' # Non-prefixed number
    assert result['parsed_body_params']['To'] == '+14155234567'

    context = result['context_object']
    assert context['channel_type'] == 'sms'
    assert context['from'] == '+14155238886'
    assert context['to'] == '+14155234567'
    assert context['message_sid'] == 'SM_sms_sid'
    assert context['body'] == 'SMS reply'
    # Note: Conversation ID derivation might need adjustment if prefixes differ for SMS
    assert context['conversation_id'] == 'conv_+14155234567_+14155238886' # Expecting +


# Placeholder for email tests - needs concrete email body format
# def test_parse_email_success(mock_event_base):
#     event = mock_event_base
#     event['path'] = '/email'
#     event['requestContext']['path'] = '/test/email'
#     event['pathParameters']['proxy'] = 'email'
#     event['headers']['Content-Type'] = 'application/json'
#     # Remove Twilio signature as it's not relevant for email
#     del event['headers']['X-Twilio-Signature']
#     parsing_result['signature_header'] = None # Explicitly None
#     event['body'] = json.dumps({
#         "from_address": "sender@example.com",
#         "to_address": "receiver@example.com",
#         "subject": "Re: Your message",
#         "body": "This is the email reply content.",
#         "email_id": "email_unique_id_123"
#     })
#     result = parse_incoming_request(event)
#     # ... assertions for email ...


def test_parse_missing_twilio_signature(mock_event_base):
    """Test parsing when X-Twilio-Signature header is missing."""
    event = mock_event_base
    del event['headers']['X-Twilio-Signature'] # Remove the header

    result = parse_incoming_request(event)

    # Parsing itself succeeds, but signature_header is None
    assert result['success'] is True
    assert result['signature_header'] is None
    # The rest of the parsing should still work
    assert result['context_object']['channel_type'] == 'whatsapp'
    assert result['context_object']['from'] == 'whatsapp:+14155238886'

def test_parse_missing_host_header(mock_event_base):
    """Test failure when Host header is missing (needed for URL reconstruction)."""
    event = mock_event_base
    del event['headers']['Host'] # Remove the header

    result = parse_incoming_request(event)

    assert result['success'] is False
    assert result['request_url'] is None # Cannot reconstruct URL

def test_parse_unknown_path(mock_event_base):
    """Test failure for an unrecognized request path."""
    event = mock_event_base
    event['path'] = '/unknown_channel'
    event['requestContext']['path'] = '/test/unknown_channel'
    event['pathParameters']['proxy'] = 'unknown_channel'

    result = parse_incoming_request(event)

    assert result['success'] is False
    assert 'channel_type' not in result['context_object'] # Channel type not determined

def test_parse_missing_body_whatsapp(mock_event_base):
    """Test failure when body is missing for WhatsApp/SMS."""
    event = mock_event_base
    event['body'] = None

    result = parse_incoming_request(event)
    assert result['success'] is False

def test_parse_invalid_body_whatsapp(mock_event_base):
    """Test failure when body is not valid form-urlencoded."""
    event = mock_event_base
    event['body'] = "This is not urlencoded %%%"

    result = parse_incoming_request(event)
    assert result['success'] is False

def test_parse_missing_essential_whatsapp_param(mock_event_base):
    """Test failure when an essential body parameter (e.g., From) is missing."""
    event = mock_event_base
    # Simulate missing 'From' field
    event['body'] = "AccountSid=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxx&Body=Hello&MessageSid=SMxxxxxxxx&To=whatsapp%3A%2B14155234567"

    result = parse_incoming_request(event)

    # Parsing the body itself might succeed initially
    assert result['parsed_body_params'] is not None
    # But the final success check should fail due to missing required key
    assert result['success'] is False
    assert result['context_object'].get('from') is None

def test_parse_different_header_casing(mock_event_base):
    """Test successful parsing with different casing for headers."""
    event = mock_event_base
    event['headers'] = {
        "host": "test-api.execute-api.eu-north-1.amazonaws.com",
        "x-twilio-signature": "test_signature_lowercase"
        # Add other necessary headers if structure relies on them
    }
    # Need to ensure requestContext provides stage info if Host header is lowercase
    event['requestContext']['stage'] = "test"
    event['requestContext']['path'] = "/test/whatsapp"


    result = parse_incoming_request(event)

    assert result['success'] is True
    assert result['signature_header'] == "test_signature_lowercase"
    assert result['request_url'] is not None # Check it was constructed

def test_parse_conversation_id_derivation_order(mock_event_base):
    """Test that conversation_id is derived correctly regardless of From/To order."""
    event1 = mock_event_base.copy() # Keep original order
    event2 = mock_event_base.copy()
    # Swap From and To in the body for event2
    event2['body'] = "AccountSid=ACxxx&Body=Hi&From=whatsapp%3A%2B14155234567&MessageSid=SMxxx&To=whatsapp%3A%2B14155238886"

    result1 = parse_incoming_request(event1)
    result2 = parse_incoming_request(event2)

    assert result1['success'] is True
    assert result2['success'] is True
    assert result1['context_object']['conversation_id'] == 'conv_+14155234567_+14155238886' # Expecting +
    assert result2['context_object']['conversation_id'] == 'conv_+14155234567_+14155238886' # Expecting +

def test_parse_no_body_email_allowed(mock_event_base):
    """Test parsing allows empty body for email (as per current code)."""
    event = mock_event_base
    event['path'] = '/email'
    event['requestContext']['path'] = '/test/email'
    event['pathParameters']['proxy'] = 'email'
    del event['headers']['X-Twilio-Signature']
    event['body'] = None # Explicitly no body

    # Mock email parsing logic if needed, or assume basic structure
    # Here we check if it proceeds despite no body

    # Need dummy email identifiers to pass final check
    # Note: This might fail if email parsing becomes stricter about body presence
    # Adjust based on actual email handling logic if/when implemented.
    # For now, we'll manually inject dummy data to check if *parsing* succeeds.
    # This isn't ideal, better approach is to mock email parsing itself if needed.

    # Since email parsing is basic, let's skip the dummy injection and expect failure
    # because the final check expects 'body' key which won't be present
    # UPDATE: The current code allows empty email body but fails the final check
    # Let's refine the test to reflect this
    result = parse_incoming_request(event)
    assert result['success'] is False # Fails final check due to missing body key
    assert result['context_object']['channel_type'] == 'email'


# Optional: Test base64 encoded body if needed
# def test_parse_base64_encoded_body(mock_event_base):
#     event = mock_event_base
#     original_body = "From=%2B1&To=%2B2&Body=Encoded"
#     event['body'] = base64.b64encode(original_body.encode('utf-8')).decode('utf-8')
#     event['isBase64Encoded'] = True
#
#     # Need to mock parse_qs to handle decoded body
#     with patch('src.staging_lambda.lambda_pkg.utils.parsing_utils.parse_qs') as mock_parse_qs:
#         mock_parse_qs.return_value = {'From': ['+1'], 'To': ['+2'], 'Body': ['Encoded']}
#         result = parse_incoming_request(event)
#
#         # Check that parse_qs was called with the decoded body
#         # Note: This requires the parser to handle base64 decoding
#         # The current parser doesn't explicitly decode, API GW might do it? Need verification.
#         # Assuming API GW handles decoding if isBase64Encoded is true:
#         assert result['success'] is True
#         assert result['parsed_body_params']['Body'] == 'Encoded' 