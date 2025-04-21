# webhook_handler/index.py

import json
import logging # Import logging
import os # Added os import
# Removed urllib.parse import as it's now in parsing_utils

# Placeholder for future modular functions
# from .core import validation_logic
# from .services import queue_service
# from .utils import parsing_utils

from .utils import parsing_utils # Import the module
from .core import validation
from .core import routing # Import the new routing module
from .services import dynamodb_service
from .services import sqs_service
from .services import secrets_manager_service # Import new service
from .utils import response_builder

# Twilio Validation Import
from twilio.request_validator import RequestValidator

# Import project modules
# Remove duplicate import: from .utils import parsing_utils

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
    'SECRET_FETCH_TRANSIENT_ERROR' # Assuming secrets manager might have transient issues
}

# Removed placeholder Queue constants - they now live in routing.py

def _determine_final_error_response(context_object_or_channel_type, error_code, error_message):
    """
    Determines the final HTTP response based on channel and error code.
    Handles partial context_object or just channel_type string during early failures.
    Applies Twilio-specific logic for retries.
    """
    logger.warning(f"Handling error: Code='{error_code}', Message='{error_message}'")

    # Determine channel type robustly
    channel_type = 'unknown'
    if isinstance(context_object_or_channel_type, dict):
        channel_type = context_object_or_channel_type.get('channel_type', 'unknown')
    elif isinstance(context_object_or_channel_type, str):
        channel_type = context_object_or_channel_type

    # Get the standard response suggestion
    suggested_response = response_builder.create_error_response(error_code, error_message)

    if channel_type in ['whatsapp', 'sms']:
        if error_code in TRANSIENT_ERROR_CODES:
            logger.warning(f"Raising exception for transient error '{error_code}' for API GW mapping.")
            raise Exception(f"Transient server error: {error_code} - {error_message}")
        else:
            logger.info(f"Mapping non-transient error '{error_code}' to 200 TwiML for Twilio.")
            if error_code == 'CONVERSATION_LOCKED':
                logger.info("Conversation locked, returning specific TwiML message.")
                return response_builder.create_twiml_error_response(
                    "I'm processing your previous message. Please wait for my response before sending more."
                )
            elif error_code == 'INVALID_SIGNATURE':
                 # Security: Log critical, return generic empty TwiML to prevent info leak
                 logger.critical("Invalid Twilio Signature - returning empty TwiML")
                 return response_builder.create_success_response_twiml()
            else:
                return response_builder.create_success_response_twiml()
    else:
        # Handle other channels: return standard error response
        # Could potentially map INVALID_SIGNATURE to 403 here if needed for non-Twilio
        logger.info(f"Returning standard error response for channel {channel_type} - {error_code}")
        return suggested_response

# Removed _determine_target_queue - it now lives in core/routing.py

def handler(event, context):
    """Main Lambda handler function with Late Validation flow."""
    logger.info(f"Received event: {json.dumps(event)}")
    parsing_result = None
    context_object = None
    credential_ref = None
    retrieved_auth_token = None
    conversation_id = "UNKNOWN"

    try:
        # --- Step 1: Parsing (Enhanced) ---
        parsing_result = parsing_utils.parse_incoming_request(event)
        if not parsing_result or not parsing_result.get('success'):
            logger.error("Failed during initial request parsing.")
            path = event.get('path', '')
            channel_type = 'whatsapp' if path == '/whatsapp' else 'sms' if path == '/sms' else 'email' if path == '/email' else 'unknown'
            return _determine_final_error_response(
                channel_type,
                'PARSING_ERROR',
                "Failed to parse incoming request."
            )

        # Extract key components needed early
        context_object = parsing_result.get('context_object', {})
        channel_type = context_object.get('channel_type')
        from_id = context_object.get('from') or context_object.get('from_address')
        to_id = context_object.get('to') or context_object.get('to_address')
        conversation_id = context_object.get('conversation_id') # Derived in parser
        # Store the incoming message SID before overwriting context
        incoming_message_sid = context_object.get('message_sid') or context_object.get('email_id')

        logger.info(f"Processing initial request for conversation {conversation_id} (Channel: {channel_type}, SID: {incoming_message_sid})")

        if not channel_type or not from_id or not to_id:
             logger.error("Essential identifiers missing after parsing.")
             return _determine_final_error_response(channel_type or 'unknown', 'PARSING_ERROR', "Missing essential identifiers.")

        # --- Step 2: Get Credential Reference --- (Minimal DB Query)
        logger.debug(f"Looking up credential reference for {channel_type} from {from_id} to {to_id}")
        credential_lookup = dynamodb_service.get_credential_ref_for_validation(channel_type, from_id, to_id)

        lookup_status = credential_lookup.get('status')
        if lookup_status != 'FOUND':
            error_message = f"Credential lookup failed with status: {lookup_status}"
            # Map specific DB failures to appropriate response handling
            return _determine_final_error_response(channel_type, lookup_status, error_message)

        credential_ref = credential_lookup.get('credential_ref')
        # We also get the definitive conversation_id from the lookup
        conversation_id = credential_lookup.get('conversation_id', conversation_id)
        logger.info(f"Found credential reference for conversation {conversation_id}: {credential_ref}")

        # --- Step 3: Fetch Specific Auth Token --- (Secrets Manager Call)
        retrieved_auth_token = secrets_manager_service.get_twilio_auth_token(credential_ref)
        if not retrieved_auth_token:
            logger.error(f"Failed to retrieve secret '{credential_ref}' from Secrets Manager for conversation {conversation_id}.")
            # Assume non-transient for now unless secrets service returns specific transient error
            return _determine_final_error_response(channel_type, 'SECRET_FETCH_FAILED', "Failed to retrieve necessary credentials")

        # --- Step 4: VALIDATE SIGNATURE --- (Using retrieved token)
        logger.info(f"Validating Twilio signature for message {incoming_message_sid}")
        validator = RequestValidator(retrieved_auth_token)
        signature_header = parsing_result.get('signature_header')
        request_url = parsing_result.get('request_url')
        parsed_body_params = parsing_result.get('parsed_body_params')

        # Crucial check: Ensure all components for validation were successfully parsed
        if not signature_header:
             logger.error(f"Missing X-Twilio-Signature header; cannot validate request for conversation {conversation_id}.")
             # Treat missing signature as invalid
             return _determine_final_error_response(channel_type, 'INVALID_SIGNATURE', 'Missing required signature header')
        if not request_url or not parsed_body_params:
             logger.error(f"Missing URL or parsed body; cannot validate request for conversation {conversation_id}.")
             return _determine_final_error_response(channel_type, 'INTERNAL_ERROR', 'Parsing failed to provide validation components')

        is_valid = validator.validate(
            request_url,
            parsed_body_params,
            signature_header
        )

        if not is_valid:
            # Logged as CRITICAL in _determine_final_error_response if needed
            return _determine_final_error_response(channel_type, 'INVALID_SIGNATURE', 'Invalid Twilio Signature')

        logger.info(f"Twilio signature successfully validated for conversation {conversation_id}.")
        # --- Validation Complete --- #

        # Store the channel_type derived from the path BEFORE fetching full context - NO LONGER NEEDED
        # parsed_channel_type = context_object.get('channel_type')
        # Store the incoming message SID before overwriting context - NO LONGER NEEDED
        # incoming_message_sid = context_object.get('message_sid') or context_object.get('email_id')

        # --- Step 5: Fetch & Merge Full Context --- (Main DB Query)
        # Use from_id (user identifier) from initial parsing as primary_channel key
        # Need to strip prefix if necessary (assuming from_id might still have it)
        primary_channel_key = from_id
        if channel_type in ['whatsapp', 'sms'] and from_id:
             prefix = f"{channel_type}:"
             if from_id.startswith(prefix):
                 primary_channel_key = from_id[len(prefix):]

        if not primary_channel_key:
             logger.error(f"Cannot determine primary channel key (from_id) for GetItem for {conversation_id}")
             return _determine_final_error_response(channel_type, 'INTERNAL_ERROR', "Cannot determine primary key for context lookup")

        # conversation_id came definitively from the GSI lookup
        logger.info(f"Fetching full context for validated conversation PK={primary_channel_key}, SK={conversation_id}")
        context_lookup = dynamodb_service.get_full_conversation(primary_channel_key, conversation_id)
        context_status = context_lookup.get('status')

        if context_status != 'FOUND':
            logger.error(f"Failed to fetch full context for {conversation_id} after successful validation lookup. Status: {context_status}")
            return _determine_final_error_response(channel_type, context_status or 'DB_GET_ITEM_ERROR', "Failed to retrieve full conversation context")

        # --- MERGE data from DB into existing context_object --- #
        db_data = context_lookup.get('data', {})
        context_object.update(db_data) # Merge DB data into the context from initial parse
        logger.debug(f"Successfully merged DB data into context object for {conversation_id}")

        # --- Step 6+: Existing Logic (Now uses the merged context object) ---
        logger.info(f"Proceeding with validated & contextualized message for conversation {conversation_id}")

        # --- Rule Validation (Uses channel_type from initial parse, other fields from DB merge) ---
        rules_check = validation.validate_conversation_rules(context_object)
        if not rules_check['valid']:
            return _determine_final_error_response(context_object, rules_check.get('error_code', 'VALIDATION_FAILED'), rules_check.get('message'))

        # --- Routing ---
        target_queue_url = routing.determine_target_queue(context_object)
        if not target_queue_url:
            logger.error(f"Failed to determine target queue URL for conversation: {conversation_id}")
            return _determine_final_error_response(context_object, 'ROUTING_ERROR', "Could not determine routing queue")

        # --- Staging ---
        logger.info(f"Attempting to write to stage table for conversation: {conversation_id}")
        stage_write_status = dynamodb_service.write_to_stage_table(context_object)
        if stage_write_status != 'SUCCESS':
            logger.error(f"Failed to write message to stage table for conversation: {conversation_id}. Status: {stage_write_status}")
            return _determine_final_error_response(context_object, stage_write_status, "Failed to stage message details")

        # --- Locking & Queuing ---
        should_send_sqs_message = False
        if target_queue_url == routing.HANDOFF_QUEUE_URL:
            logger.info(f"Routing message directly to handoff queue for conversation: {conversation_id}")
            should_send_sqs_message = True
        else:
            logger.info(f"Attempting to acquire trigger lock for conversation: {conversation_id}")
            lock_status = dynamodb_service.acquire_trigger_lock(conversation_id)
            if lock_status == 'ACQUIRED':
                logger.info(f"Trigger lock ACQUIRED for {conversation_id}, will send SQS trigger.")
                should_send_sqs_message = True
            elif lock_status == 'EXISTS':
                logger.info(f"Trigger lock already EXISTS for {conversation_id}, skipping SQS send.")
                should_send_sqs_message = False
            else:
                logger.error(f"Failed to acquire trigger lock for conversation: {conversation_id}. Status: {lock_status}")
                return _determine_final_error_response(context_object, lock_status, "Failed to check/acquire trigger lock")

        if should_send_sqs_message:
            logger.info(f"Attempting to send message to SQS queue: {target_queue_url}")
            sqs_send_status = sqs_service.send_message_to_queue(target_queue_url, context_object)
            if sqs_send_status != 'SUCCESS':
                logger.error(f"Failed to send message to SQS queue {target_queue_url} for conversation: {conversation_id}. Status: {sqs_send_status}")
                return _determine_final_error_response(context_object, sqs_send_status, "Failed to queue message")
        else:
            logger.info(f"Skipping SQS trigger send for {conversation_id} as lock already existed.")
            pass

        # --- Step 8: Acknowledge Success ---
        logger.info(f"Processing complete for conversation {conversation_id}. Sending success acknowledgment.")
        if context_object.get('channel_type') in ['whatsapp', 'sms']:
            return response_builder.create_success_response_twiml()
        else:
            return response_builder.create_success_response_json(message=f"{context_object.get('channel_type', 'Message').capitalize()} received")

    except Exception as e:
        # General exception handler
        logger.exception("Unhandled exception caught in webhook handler")
        # Try to determine channel type for response if possible
        fallback_channel_type = 'unknown'
        if parsing_result and parsing_result.get('context_object'):
            fallback_channel_type = parsing_result['context_object'].get('channel_type', 'unknown')
        elif event:
             path = event.get('path', '')
             fallback_channel_type = 'whatsapp' if path == '/whatsapp' else 'sms' if path == '/sms' else 'email' if path == '/email' else 'unknown'

        if "Transient server error:" in str(e):
            raise e # Re-raise known transient errors for API GW mapping
        else:
             # Return appropriate generic error based on channel
            return _determine_final_error_response(fallback_channel_type, 'INTERNAL_ERROR', 'An unexpected server error occurred')

# Commented out __main__ block for clarity - use pytest for testing
# if __name__ == '__main__':
#     # ... (example event data can be moved to test files) ...
#     pass

# Removed example usage block as testing should be separate 