# webhook_handler/index.py

import json
from urllib.parse import parse_qs

# Placeholder for future modular functions
# from .core import validation_logic
# from .services import queue_service
# from .utils import parsing_utils

def create_context_object(event):
    """Parses event, extracts data, maps known keys to snake_case, builds context dict. Returns None on failure."""
    context_object = {}
    request_path = event.get('path', '')

    # Determine channel
    if request_path == '/whatsapp':
        context_object['channel_type'] = 'whatsapp'
    elif request_path == '/sms':
        context_object['channel_type'] = 'sms'
    elif request_path == '/email': # Assuming email path for now
        context_object['channel_type'] = 'email'
    else:
        print(f"ERROR: Unknown request path {request_path}")
        return None

    # Parse body
    raw_body = event.get('body', '')
    parsed_body = {}
    if context_object['channel_type'] in ['whatsapp', 'sms']:
        if not raw_body:
            print("ERROR: Missing body for WhatsApp/SMS")
            return None
        try:
            parsed_qs_dict = parse_qs(raw_body)
            parsed_body = {k: v[0] for k, v in parsed_qs_dict.items()}
        except Exception as e:
            print(f"ERROR parsing form-urlencoded body: {e}")
            return None
    elif context_object['channel_type'] == 'email':
        if not raw_body:
             print("WARN: Missing body for Email")
             parsed_body = {}
        else:
            try:
                 parsed_body = json.loads(raw_body)
            except json.JSONDecodeError as e:
                 print(f"ERROR parsing email JSON body: {e}")
                 return None
    # ... Add more channel parsing logic ...

    # Populate context object with snake_case keys (hardcoded for known fields)
    if context_object['channel_type'] in ['whatsapp', 'sms']:
        context_object['from'] = parsed_body.get('From')
        context_object['to'] = parsed_body.get('To')
        context_object['body'] = parsed_body.get('Body')
        context_object['message_sid'] = parsed_body.get('MessageSid')
        context_object['account_sid'] = parsed_body.get('AccountSid')
        # Add any other known Twilio fields here if needed
    elif context_object['channel_type'] == 'email':
        # Assuming email parser provides snake_case keys directly
        context_object['from_address'] = parsed_body.get('from_address')
        context_object['to_address'] = parsed_body.get('to_address')
        context_object['email_body'] = parsed_body.get('email_body')
        context_object['email_id'] = parsed_body.get('email_id')
    # ... Add more channel mappings ...

    # Basic validation of essential snake_case fields
    # Adjust required fields based on expected snake_case keys
    if context_object['channel_type'] in ['whatsapp', 'sms']:
         required_keys = ['from', 'to', 'body', 'message_sid', 'account_sid']
    elif context_object['channel_type'] == 'email':
         # Adjust for expected snake_case email keys if different
         required_keys = ['from_address', 'to_address', 'email_body', 'email_id'] # Assuming these are already snake_case
    else:
         required_keys = [] # Should not happen if channel type determined earlier

    if not all(context_object.get(k) for k in required_keys):
         print(f"ERROR: Missing essential fields after parsing: {context_object}")
         # Consider logging which specific keys are missing
         return None

    print(f"Successfully created context_object: {context_object}")
    return context_object

def handler(event, context):
    """Main Lambda handler function."""
    print(f"Received event: {json.dumps(event)}")

    context_object = create_context_object(event)

    if context_object is None:
        print("Failed to create valid context object. Returning error response.")
        # Return generic TwiML error for now
        return {
            'statusCode': 400,
            'headers': {'Content-Type': 'text/xml'},
            'body': '<?xml version="1.0" encoding="UTF-8"?><Response><Message>Failed to process request.</Message></Response>'
        }

    # --- Placeholder for further processing --- 
    # 1. Core Validation (DynamoDB checks etc.) using context_object
    #    validation_result = validation_logic.validate_conversation(context_object)
    #    if not validation_result['valid']:
    #        # Handle validation failure, return appropriate TwiML/error
    #        return validation_result['response']

    # 2. Routing Logic
    #    queue_url = routing_logic.determine_queue(context_object)

    # 3. Send to SQS (using services module)
    #    try:
    #        queue_service.send_to_sqs(queue_url, context_object)
    #    except Exception as e:
    #        # Handle queueing failure, return 5xx error
    #        print(f"ERROR queueing message: {e}")
    #        # Return error (important: decide if 500 or 200 TwiML)
    #        # For now, return success TwiML to prevent Twilio retries on queue fail
    #        return {
    #            'statusCode': 200, 
    #            'headers': {'Content-Type': 'text/xml'},
    #            'body': '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
    #        }

    # 4. Acknowledge Success (Default for Twilio)
    print("Processing steps completed (placeholders). Sending success acknowledgment.")
    if context_object['channel_type'] in ['whatsapp', 'sms']:
        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'text/xml'},
            'body': '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
        }
    else: # Example for email
         return {
            'statusCode': 200,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'status': 'received'})
         }

# Example usage (for local testing)
if __name__ == '__main__':
    # Example Twilio event
    example_event_twilio = {
        "path": "/whatsapp",
        "httpMethod": "POST",
        "headers": {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Twilio-Signature": "test_sig"
        },
        "queryStringParameters": None,
        "pathParameters": None,
        "requestContext": {},
        "body": "From=whatsapp%3A%2B14155238886&To=whatsapp%3A%2B15005550006&Body=Hello+there%21&MessageSid=SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxx&AccountSid=ACyyyyyyyyyyyyyyyyyyyyyyyyyyyyy",
        "isBase64Encoded": False
    }
    response = handler(example_event_twilio, None)
    print(f"\nHandler Response:\n{json.dumps(response, indent=2)}")

    # Example Email event (hypothetical JSON body)
    example_event_email = {
        "path": "/email",
        "httpMethod": "POST",
        "headers": {
            "Content-Type": "application/json"
        },
        "body": json.dumps({
            "from_address": "sender@example.com", # Already snake_case
            "to_address": "receiver@example.com", # Already snake_case
            "email_body": "This is the email content.", # Already snake_case
            "email_id": "email_zzzzzzzzzzzzzzzz" # Already snake_case
        })
    }
    response_email = handler(example_event_email, None)
    print(f"\nEmail Handler Response:\n{json.dumps(response_email, indent=2)}") 