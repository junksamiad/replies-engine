# webhook_handler/index.py

import json
import logging # Import logging
# Removed urllib.parse import as it's now in parsing_utils

# Placeholder for future modular functions
# from .core import validation_logic
# from .services import queue_service
# from .utils import parsing_utils

from .utils.parsing_utils import create_context_object
from .core import validation
from .services import sqs_service # Placeholder
from .utils import response_builder

# Define logger for handler exceptions
logger = logging.getLogger()
logger.setLevel(logging.INFO) # Or DEBUG

# Define error codes that should trigger retries for Twilio (by raising Exception)
TRANSIENT_ERROR_CODES = {
    'DB_TRANSIENT_ERROR',
    # Add 'QUEUE_TRANSIENT_ERROR' here when implemented
}

def _determine_final_error_response(context_object, error_code, error_message):
    """
    Determines the final HTTP response based on channel and error code.
    Applies Twilio-specific logic: 
    - Raises Exception ONLY for known TRANSIENT_ERROR_CODES to allow retry.
    - Returns 200 OK TwiML for ALL OTHER errors to prevent retry.
    """
    # Log the underlying error details
    print(f"Handling error: Code='{error_code}', Message='{error_message}'")
    
    # Get the standard response suggestion (mainly for status code mapping)
    # We might not use the full response directly for Twilio non-transient
    suggested_response = response_builder.create_error_response(error_code, error_message)
    
    # Ensure channel_type is available, default to 'unknown' if context is partial/missing
    channel_type = context_object.get('channel_type', 'unknown') if context_object else 'unknown'

    if channel_type in ['whatsapp', 'sms']:
        # Handle Twilio: Raise Exception for transient, return 200 TwiML otherwise
        if error_code in TRANSIENT_ERROR_CODES:
            print(f"Raising exception for transient error '{error_code}' for API GW mapping.")
            # Raise exception to allow API Gateway's Integration Response to map to 5xx
            raise Exception(f"Transient server error: {error_code} - {error_message}")
        else:
            # For ALL other non-transient errors (4xx or unexpected 5xx), return 200 TwiML
            print(f"Mapping non-transient error '{error_code}' to 200 TwiML for Twilio.")
            return response_builder.create_success_response_twiml()
    else:
        # Handle other channels (e.g., email): return standard error response
        print(f"Returning standard error response for channel {channel_type} - {error_code}")
        return suggested_response

def handler(event, context):
    """Main Lambda handler function."""
    print(f"Received event: {json.dumps(event)}")
    context_object = None # Initialize context_object
    try:
        context_object = create_context_object(event)

        if context_object is None:
            print("Failed to create valid context object.")
            path = event.get('path', '')
            temp_context_for_response = {'channel_type': 'whatsapp' if path == '/whatsapp' else 'sms' if path == '/sms' else 'email' if path == '/email' else 'unknown'}
            # PARSING_ERROR is non-transient
            return _determine_final_error_response(
                temp_context_for_response, 
                'PARSING_ERROR', 
                "Failed to parse incoming request or create context."
            )

        # --- Core Validation Steps --- 
        existence_check = validation.check_conversation_exists(context_object)
        
        if not existence_check['valid']:
            error_code = existence_check.get('error_code', 'INTERNAL_ERROR')
            error_message = existence_check.get('message', 'Unknown validation error')
            return _determine_final_error_response(context_object, error_code, error_message)
        
        context_object = existence_check['data']

        # --- Further Validation Placeholder ---
        # further_validation_result = validation.validate_further(context_object)
        # if not further_validation_result['valid']:
        #    error_code = further_validation_result.get('error_code', 'VALIDATION_FAILED')
        #    error_message = further_validation_result.get('message', 'Further validation failed')
        #    return _determine_final_error_response(context_object, error_code, error_message)
        # context_object = further_validation_result['data']

        # --- Routing/Queueing Placeholder ---
        # try:
        #    queue_url = validation.determine_routing(context_object)
        #    sqs_service.send_message(queue_url, context_object)
        # except Exception as e:
        #    print(f"ERROR during routing/queueing: {e}")
        #    # Determine if QUEUE_ERROR is transient or not
        #    return _determine_final_error_response(context_object, 'QUEUE_ERROR', f"Failed process message routing or queueing: {e}")

        # --- Acknowledge Success --- 
        print("Processing successful. Sending success acknowledgment.")
        if context_object['channel_type'] in ['whatsapp', 'sms']:
            return response_builder.create_success_response_twiml()
        else:
            return response_builder.create_success_response_json(message=f"{context_object['channel_type'].capitalize()} received")

    except Exception as e:
        # General exception handler for unexpected errors 
        # (INCLUDING transient ones intentionally raised by _determine_final_error_response)
        print(f"FATAL ERROR in handler: {e}")
        logger.exception("Unhandled exception caught in webhook handler") # Log full stack trace
        
        # Check if the exception message indicates it was an intentionally raised transient error
        # This helps differentiate between expected transient failures and actual code bugs
        if "Transient server error:" in str(e):
            # Re-raise the specific exception to let API Gateway handle it as 5xx
            # This ensures Twilio retries ONLY for these known transient cases
            raise e 
        else:
            # This is likely an unexpected code bug or unhandled scenario
            # Determine channel type for response if possible
            channel_type = context_object.get('channel_type', 'unknown') if context_object else 'unknown'
            if not channel_type or channel_type == 'unknown':
                 path = event.get('path', '')
                 channel_type = 'whatsapp' if path == '/whatsapp' else 'sms' if path == '/sms' else 'email' if path == '/email' else 'unknown'

            if channel_type in ['whatsapp', 'sms']:
                # Safety net: Return 200 TwiML for unexpected errors to prevent Twilio retries
                print("Caught unexpected exception, returning 200 TwiML to Twilio to prevent retries on code error.")
                return response_builder.create_success_response_twiml()
            else:
                # For other channels, return a standard 500 error
                return response_builder.create_error_response('INTERNAL_ERROR', 'An unexpected server error occurred', 500)

# Commented out __main__ block for clarity - use pytest for testing
# if __name__ == '__main__':
#     # ... (example event data can be moved to test files) ...
#     pass

# Removed example usage block as testing should be separate 