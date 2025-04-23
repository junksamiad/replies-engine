# webhook_handler/utils/parsing_utils.py

import json
import logging # Import logging
import os # Add os import
from urllib.parse import parse_qs, urlencode

logger = logging.getLogger(__name__) # Use __name__
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO').upper()) # Use env var

def parse_incoming_request(event):
    """Parses event, extracts data, reconstructs URL, gets signature, returns structured dict."""
    # Initialize the dictionary to hold all parsed results
    parsing_result = {
        'success': False,
        'context_object': {},
        'signature_header': None,
        'request_url': None,
        'parsed_body_params': None
    }
    context_object = parsing_result['context_object'] # Shortcut

    headers = event.get('headers', {}) # Case-insensitive lookup below
    request_context = event.get('requestContext', {})
    raw_body = event.get('body', '')
    request_path = event.get('path', '')

    # 1. Extract Signature Header (Handle case variations)
    sig_header_name = next((k for k in headers if k.lower() == 'x-twilio-signature'), None)
    if sig_header_name:
        parsing_result['signature_header'] = headers[sig_header_name]
        logger.debug("Extracted X-Twilio-Signature header.")
    else:
        logger.warning("Missing X-Twilio-Signature header in request.")
        # Note: We still proceed but validation will fail later if header is missing

    # 2. Determine Channel Type
    if request_path == '/whatsapp':
        context_object['channel_type'] = 'whatsapp'
    elif request_path == '/sms':
        context_object['channel_type'] = 'sms'
    elif request_path == '/email':
        context_object['channel_type'] = 'email'
    else:
        logger.error(f"Unknown request path: {request_path}")
        return parsing_result # Return failure

    # 3. Parse Request Body
    parsed_body = {}
    if context_object['channel_type'] in ['whatsapp', 'sms']:
        if not raw_body:
            logger.error("Missing request body for WhatsApp/SMS")
            return parsing_result # Return failure
        try:
            parsed_qs_dict = parse_qs(raw_body)
            parsed_body = {k: v[0] for k, v in parsed_qs_dict.items()} # Twilio sends single values
            parsing_result['parsed_body_params'] = parsed_body # Store the dict for validation
        except Exception as e:
            logger.exception("Error parsing form-urlencoded body")
            return parsing_result # Return failure
    elif context_object['channel_type'] == 'email':
        if not raw_body:
            print("WARN: Missing body for Email")
            parsed_body = {}
        else:
            try:
                parsed_body = json.loads(raw_body)
            except json.JSONDecodeError as e:
                print(f"ERROR parsing email JSON body: {e}")
                return parsing_result # Return failure

    # 4. Reconstruct Request URL (Critical for Validation)
    # Need Host header (handle case), stage, and path.
    # Twilio signs the URL *including* the port if it's non-standard (80/443).
    # API Gateway usually provides Host without port for standard ports.
    host_header_name = next((k for k in headers if k.lower() == 'host'), None)
    host = headers.get(host_header_name, '') if host_header_name else ''
    stage = request_context.get('stage', '')
    # Use 'path' directly as it includes the leading slash

    if host:
        # Assume HTTPS as required by API Gateway best practices
        # NOTE: Twilio docs state port is dropped for HTTPS unless non-standard.
        # API Gateway's Host header typically doesn't include standard ports.
        # If validation fails, double-check if Twilio *is* including :443
        base_url = f"https://{host}/{stage}{request_path}"
        # If the original request had query parameters (unlikely for Twilio webhooks, but possible)
        # they would need to be sorted alphabetically and appended here.
        # query_params = event.get('queryStringParameters')
        # if query_params:
        #     sorted_query = urlencode(sorted(query_params.items()))
        #     base_url += "?" + sorted_query

        parsing_result['request_url'] = base_url
        logger.debug(f"Reconstructed URL for validation: {base_url}")
    else:
        logger.error("Missing Host header, cannot reconstruct URL for validation.")
        return parsing_result # Return failure

    # 5. Populate Context Object (Basic Identifiers)
    # Only populate essential identifiers needed before full context load
    if context_object['channel_type'] in ['whatsapp', 'sms']:
        context_object['from'] = parsed_body.get('From')
        context_object['to'] = parsed_body.get('To')
        context_object['message_sid'] = parsed_body.get('MessageSid') # Useful for idempotency/logging
        context_object['account_sid'] = parsed_body.get('AccountSid') # Might be useful
        context_object['body'] = parsed_body.get('Body') # The actual message content
        # Derive conversation_id consistently (e.g., sorted numbers)
        # Ensure 'from' and 'to' exist before splitting (should be guaranteed by validation later)
        from_num_part = context_object['from'].split(':')[-1] if context_object.get('from') else ''
        to_num_part = context_object['to'].split(':')[-1] if context_object.get('to') else ''
        # Handle potential empty strings if splitting failed
        if from_num_part and to_num_part:
            context_object['conversation_id'] = f"conv_{'_'.join(sorted([from_num_part, to_num_part]))}"
        else:
            # Fallback or error if numbers aren't present/valid - should ideally be caught by later validation
            logger.warning("Could not derive conversation_id due to missing from/to numbers.")
            context_object['conversation_id'] = None # Ensure it's None if derivation fails

    elif context_object['channel_type'] == 'email':
        # Populate essential email identifiers
        context_object['from_address'] = parsed_body.get('from_address') # Use keys from your email parser
        context_object['to_address'] = parsed_body.get('to_address')
        context_object['email_id'] = parsed_body.get('email_id')
        context_object['conversation_id'] = f"conv_{context_object['from_address']}_{context_object['to_address']}" # Example

    # 6. Basic Validation (Essential IDs only)
    if context_object['channel_type'] in ['whatsapp', 'sms']:
        # UPDATED: Add 'body' to required keys for whatsapp/sms
        required_keys = ['from', 'to', 'message_sid', 'body', 'conversation_id']
    elif context_object['channel_type'] == 'email':
        # Add 'body' here too if required for email
        required_keys = ['from_address', 'to_address', 'email_id', 'body', 'conversation_id']
    else:
        required_keys = []

    if not all(context_object.get(k) for k in required_keys):
        logger.error(f"Missing essential identifiers after parsing: {context_object}")
        return parsing_result # Return failure

    logger.info(f"Initial parsing successful for conversation {context_object.get('conversation_id')}")
    parsing_result['success'] = True
    return parsing_result

# Removed old create_context_object function as it's replaced by parse_incoming_request 