# webhook_handler/index.py

import json
import logging # Import logging
import os # Added os import
# Removed urllib.parse import as it's now in parsing_utils

# Placeholder for future modular functions
# from .core import validation_logic
# from .services import queue_service
# from .utils import parsing_utils

from .utils.parsing_utils import create_context_object
from .core import validation
from .core import routing # Import the new routing module
from .services import dynamodb_service
from .services import sqs_service
from .utils import response_builder

# Setup logging
logger = logging.getLogger(__name__) # Use __name__
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO').upper()) # Use env var

# Define error codes that should trigger retries for Twilio (by raising Exception)
# These now directly match the transient codes returned by the service layer
TRANSIENT_ERROR_CODES = {
    'DB_TRANSIENT_ERROR',
    'STAGE_DB_TRANSIENT_ERROR',
    'TRIGGER_DB_TRANSIENT_ERROR',
    'SQS_TRANSIENT_ERROR',
}

# Removed placeholder Queue constants - they now live in routing.py

def _determine_final_error_response(context_object, error_code, error_message):
    """
    Determines the final HTTP response based on channel and error code.
    Applies Twilio-specific logic: 
    - Raises Exception ONLY for known TRANSIENT_ERROR_CODES to allow retry.
    - Returns 200 OK TwiML for ALL OTHER errors to prevent retry.
    """
    logger.warning(f"Handling error: Code='{error_code}', Message='{error_message}'") # Log as warning
    
    # Get the standard response suggestion (mainly for status code mapping)
    # We might not use the full response directly for Twilio non-transient
    suggested_response = response_builder.create_error_response(error_code, error_message)
    
    # Ensure channel_type is available, default to 'unknown' if context is partial/missing
    channel_type = context_object.get('channel_type', 'unknown') if context_object else 'unknown'

    if channel_type in ['whatsapp', 'sms']:
        # Handle Twilio: Raise Exception for transient, return 200 TwiML otherwise
        if error_code in TRANSIENT_ERROR_CODES:
            logger.warning(f"Raising exception for transient error '{error_code}' for API GW mapping.")
            # Raise exception to allow API Gateway's Integration Response to map to 5xx
            raise Exception(f"Transient server error: {error_code} - {error_message}")
        else:
            # For ALL other non-transient errors (4xx or unexpected 5xx), return 200 TwiML
            logger.info(f"Mapping non-transient error '{error_code}' to 200 TwiML for Twilio.")
            # --- Special handling for CONVERSATION_LOCKED --- 
            if error_code == 'CONVERSATION_LOCKED':
                # Per LLD 3.1 Step 5: Send specific message
                logger.info("Conversation locked, returning specific TwiML message.")
                return response_builder.create_twiml_error_response(
                    "I'm processing your previous message. Please wait for my response before sending more."
                )
            else:
                 # For other non-transient errors, return empty TwiML
                return response_builder.create_success_response_twiml()
    else:
        # Handle other channels (e.g., email): return standard error response
        logger.info(f"Returning standard error response for channel {channel_type} - {error_code}")
        return suggested_response

# Removed _determine_target_queue - it now lives in core/routing.py

def handler(event, context):
    """Main Lambda handler function."""
    logger.info(f"Received event: {json.dumps(event)}")
    context_object = None # Initialize context_object
    try:
        # --- Step 1: Parsing --- 
        context_object = create_context_object(event)
        if context_object is None:
            logger.error("Failed to create valid context object.")
            path = event.get('path', '')
            temp_context_for_response = {'channel_type': 'whatsapp' if path == '/whatsapp' else 'sms' if path == '/sms' else 'email' if path == '/email' else 'unknown'}
            # PARSING_ERROR is non-transient
            return _determine_final_error_response(
                temp_context_for_response, 
                'PARSING_ERROR', 
                "Failed to parse incoming request or create context."
            )

        conversation_id = context_object.get('conversation_id') # Get early for logging
        logger.info(f"Processing message for conversation: {conversation_id}")

        # --- Step 2 & 3: Validation --- 
        existence_check = validation.check_conversation_exists(context_object)
        if not existence_check['valid']:
            # Pass the error code directly from the validation result
            return _determine_final_error_response(context_object, existence_check.get('error_code', 'INTERNAL_ERROR'), existence_check.get('message'))
        context_object = existence_check['data'] # Update context with DB data

        # Note: validate_conversation_rules already returns error codes
        rules_check = validation.validate_conversation_rules(context_object)
        if not rules_check['valid']:
            # Pass the error code directly from the validation result
            return _determine_final_error_response(context_object, rules_check.get('error_code', 'VALIDATION_FAILED'), rules_check.get('message'))

        # --- Step 4: Routing --- 
        target_queue_url = routing.determine_target_queue(context_object)
        if not target_queue_url:
            logger.error(f"Failed to determine target queue URL for conversation: {conversation_id}")
            return _determine_final_error_response(context_object, 'ROUTING_ERROR', "Could not determine routing queue")

        # --- Step 5: Write to Stage Table --- 
        logger.info(f"Attempting to write to stage table for conversation: {conversation_id}")
        stage_write_status = dynamodb_service.write_to_stage_table(context_object)
        if stage_write_status != 'SUCCESS':
            logger.error(f"Failed to write message to stage table for conversation: {conversation_id}. Status: {stage_write_status}")
            # Pass the specific error code from the service function
            return _determine_final_error_response(context_object, stage_write_status, "Failed to stage message details")

        # --- Step 6 & 7: Attempt Lock and Queue Message (Conditional) --- 
        should_send_sqs_message = False # Flag to control actual SQS send

        if target_queue_url == routing.HANDOFF_QUEUE_URL:
            logger.info(f"Routing message directly to handoff queue for conversation: {conversation_id}")
            should_send_sqs_message = True
        else:
            # Routing to a channel queue - need to check lock
            logger.info(f"Attempting to acquire trigger lock for conversation: {conversation_id}")
            lock_status = dynamodb_service.acquire_trigger_lock(conversation_id)

            if lock_status == 'ACQUIRED':
                logger.info(f"Trigger lock ACQUIRED for {conversation_id}, will send SQS trigger.")
                should_send_sqs_message = True
            elif lock_status == 'EXISTS':
                logger.info(f"Trigger lock already EXISTS for {conversation_id}, skipping SQS send.")
                should_send_sqs_message = False # Do not send trigger
            else: # lock_status includes specific error codes now
                logger.error(f"Failed to acquire trigger lock for conversation: {conversation_id}. Status: {lock_status}")
                # Pass the specific error code from the service function
                return _determine_final_error_response(context_object, lock_status, "Failed to check/acquire trigger lock")

        # --- Step 7: Send Message (If Needed) --- 
        if should_send_sqs_message:
            logger.info(f"Attempting to send message to SQS queue: {target_queue_url}")
            sqs_send_status = sqs_service.send_message_to_queue(target_queue_url, context_object)
            if sqs_send_status != 'SUCCESS':
                logger.error(f"Failed to send message to SQS queue {target_queue_url} for conversation: {conversation_id}. Status: {sqs_send_status}")
                # Pass the specific error code from the service function
                return _determine_final_error_response(context_object, sqs_send_status, "Failed to queue message")
        else:
            # Lock existed, message not sent to channel queue
            logger.info(f"Skipping SQS trigger send for {conversation_id} as lock already existed.")
            # This path is considered successful for the webhook response
            pass

        # --- Step 8: Acknowledge Success --- 
        logger.info(f"Processing complete for conversation {conversation_id}. Sending success acknowledgment.")
        if context_object['channel_type'] in ['whatsapp', 'sms']:
            return response_builder.create_success_response_twiml()
        else:
            return response_builder.create_success_response_json(message=f"{context_object['channel_type'].capitalize()} received")

    except Exception as e:
        # General exception handler for unexpected errors 
        # (INCLUDING transient ones intentionally raised by _determine_final_error_response)
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
                logger.error("Caught unexpected exception, returning 200 TwiML to Twilio to prevent retries on code error.")
                return response_builder.create_success_response_twiml()
            else:
                # For other channels, return a standard 500 error
                logger.error("Caught unexpected exception, returning 500 Internal Error.")
                return response_builder.create_error_response('INTERNAL_ERROR', 'An unexpected server error occurred', 500)

# Commented out __main__ block for clarity - use pytest for testing
# if __name__ == '__main__':
#     # ... (example event data can be moved to test files) ...
#     pass

# Removed example usage block as testing should be separate 