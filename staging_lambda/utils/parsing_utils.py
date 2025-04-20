# webhook_handler/utils/parsing_utils.py

import json
from urllib.parse import parse_qs

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