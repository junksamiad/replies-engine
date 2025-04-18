"""
Response Builder Utility for Webhook Handler

Provides helper functions to create standardized API Gateway Lambda Proxy responses.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional

logger = logging.getLogger()
# Configure basic logging if needed, or rely on Lambda default
# logging.basicConfig(level=logging.INFO)

# Standard headers including CORS - Adjust origins/methods as needed for this API
COMMON_HEADERS = {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*', # Restrict in production!
    'Access-Control-Allow-Headers': 'Content-Type,X-Twilio-Signature,Accept,User-Agent',
    'Access-Control-Allow-Methods': 'OPTIONS,POST'
}

# --- Success Responses --- 

def create_success_response_json(data: Optional[Dict[str, Any]] = None, message: str = "Success") -> Dict[str, Any]:
    """
    Creates a standard success response (HTTP 200 OK) with a JSON body.

    Args:
        data: Optional dictionary containing success data to include.
        message: A descriptive success message.

    Returns:
        API Gateway Lambda Proxy Integration response dictionary.
    """
    body = {
        'status': 'success',
        'message': message
    }
    if data:
        body['data'] = data
    
    return {
        'statusCode': 200,
        'headers': COMMON_HEADERS,
        'body': json.dumps(body)
    }

def create_success_response_twiml(twiml_body: str = "<?xml version='1.0' encoding='UTF-8'?><Response></Response>") -> Dict[str, Any]:
    """
    Creates a standard success response (HTTP 200 OK) with a TwiML body.

    Args:
        twiml_body: The TwiML string to return. Defaults to empty <Response/>.

    Returns:
        API Gateway Lambda Proxy Integration response dictionary.
    """
    # Note: CORS headers might not be strictly needed for Twilio but are included for consistency
    headers = COMMON_HEADERS.copy()
    headers['Content-Type'] = 'text/xml'
    
    return {
        'statusCode': 200,
        'headers': headers,
        'body': twiml_body
    }

# --- Error Responses --- 

def create_error_response(error_code: str, error_message: str, status_code_hint: int = 500) -> Dict[str, Any]:
    """
    Creates a standard error response structure with appropriate HTTP status code.
    This function determines the status code but the calling handler might override it 
    (e.g., returning 200 TwiML to Twilio for certain 4xx errors).

    Args:
        error_code: An internal error code string (e.g., 'INVALID_INPUT').
        error_message: A descriptive error message for logging/debugging.
        status_code_hint: A suggested HTTP status code if error_code not mapped.

    Returns:
        A dictionary containing the suggested 'statusCode' and the formatted 'body'.
        The calling handler should add appropriate headers.
    """
    # Map internal error codes to suggested HTTP status codes
    status_code_mapping = {
        # 4xx Client Errors
        'INVALID_INPUT': 400,
        'MISSING_REQUIRED_FIELD': 400,
        'UNKNOWN_CHANNEL': 400,
        'PARSING_ERROR': 400,
        'CONVERSATION_NOT_FOUND': 404,
        'PROJECT_INACTIVE': 403,
        'CHANNEL_NOT_ALLOWED': 403,
        'CONVERSATION_LOCKED': 409, # Conflict - locked by another process
        'VALIDATION_FAILED': 400, # Generic validation failure

        # 5xx Server Errors
        'DB_QUERY_ERROR': 500,
        'DB_TRANSIENT_ERROR': 503,
        'QUEUE_ERROR': 500,
        'INTERNAL_ERROR': 500,
        'CONFIGURATION_ERROR': 500
    }

    # Determine the status code
    status_code = status_code_mapping.get(error_code, status_code_hint)
    
    body = {
        'status': 'error',
        'error_code': error_code,
        'message': error_message, # Keep message generic for client?
    }
    
    if status_code >= 500:
         logger.error(f"Server error response generated: {error_code} - {error_message}")
    else:
         logger.warning(f"Client error response generated: {error_code} - {error_message}")

    # Return structure for handler to finalize
    return {
        'statusCode': status_code,
        'headers': COMMON_HEADERS, # Provide common headers
        'body': json.dumps(body) 
    }

# --- Specific Helper --- 

def create_twiml_error_response(message: str) -> Dict[str, Any]:
    """
    Creates a TwiML response containing an error message for Twilio.
    NOTE: This sends the error message back to the user via Twilio.
          Only use when appropriate, otherwise use empty 200 TwiML.

    Args:
        message: The error message to include in the TwiML <Message> tag.

    Returns:
        API Gateway Lambda Proxy Integration response dictionary (HTTP 200 OK).
    """
    twiml_body = f"<?xml version='1.0' encoding='UTF-8'?><Response><Message>{message}</Message></Response>"
    headers = COMMON_HEADERS.copy()
    headers['Content-Type'] = 'text/xml'
    
    return {
        'statusCode': 200, # Always 200 for TwiML errors to prevent retries
        'headers': headers,
        'body': twiml_body
    } 