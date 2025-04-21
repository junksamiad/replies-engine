# webhook_handler/services/sqs_service.py
import os
import json
import logging
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO').upper())

# --- Configuration ---
# Batch window (W) in seconds - SQS DelaySeconds
BATCH_WINDOW_SECONDS = int(os.environ.get('BATCH_WINDOW_SECONDS', '10'))
HANDOFF_QUEUE_URL = os.environ.get("HANDOFF_QUEUE_URL") # Required

# Ensure handoff queue URL is configured
if not HANDOFF_QUEUE_URL:
    error_message = "Missing required environment variable: HANDOFF_QUEUE_URL"
    logger.critical(error_message)
    raise EnvironmentError(error_message)

# --- Boto3 Initialization & Error Code Lists ---
# Define potential transient SQS errors (add more as identified)
transient_sqs_errors = [
    'ServiceUnavailable',
    'InternalFailure',
    'ThrottlingException', # Less common for SendMessage but possible
    # Consider specific network-related errors if boto3/botocore surfaces them
]
# Define potential configuration/parameter SQS errors
config_sqs_errors = [
    'QueueDoesNotExist',
    'AccessDenied', # Or AccessDeniedException depending on boto version/context
]
parameter_sqs_errors = [
    'InvalidParameterValue',
    'InvalidParameterCombination',
    'InvalidMessageContents', # If body fails validation
    'MessageNotInflight', # Unlikely for send_message
    # Add others like InvalidAttributeName if manipulating attributes
]

# --- Boto3 Initialization ---
try:
    sqs = boto3.client('sqs')
    logger.info("Initialized SQS client service.")
except Exception as e:
    logger.critical(f"Failed to initialize SQS client: {e}")
    raise

# --- Service Functions ---

def send_message_to_queue(target_queue_url, context_object):
    """
    Sends a message to the specified SQS queue.
    Returns a status code string: 'SUCCESS', 'SQS_TRANSIENT_ERROR',
    'SQS_CONFIG_ERROR', 'SQS_PARAMETER_ERROR', 'SQS_SEND_ERROR', or 'INTERNAL_ERROR'.

    Determines message format and delay based on whether the target is
    the Human Handoff queue or a Channel Queue.

    Args:
        target_queue_url (str): The URL of the target SQS queue.
        context_object (dict): The context object.

    Returns:
        str: Status code string indicating the result of the operation.
    """
    if not target_queue_url or not context_object:
        logger.error(f"send_message_to_queue called with invalid args. URL: {target_queue_url}, Context: {context_object is not None}")
        return 'INTERNAL_ERROR' # Or 'BAD_INPUT'

    conversation_id = context_object.get('conversation_id')
    if not conversation_id:
        logger.error("Missing conversation_id in context_object for SQS send.")
        return 'INTERNAL_ERROR' # Or 'BAD_INPUT'

    message_body = ""
    delay_seconds = 0

    if target_queue_url == HANDOFF_QUEUE_URL:
        # Send full context immediately to Handoff Queue
        try:
            # Ensure context is JSON serializable (basic check)
            # Remove sensitive or large fields if necessary before sending
            # e.g., context_for_handoff = {k:v for k,v in context_object.items() if k not in ['large_field']}
            message_body = json.dumps(context_object)
            delay_seconds = 0
            logger.info(f"Sending full context for {conversation_id} to Handoff Queue: {HANDOFF_QUEUE_URL}")
        except TypeError as e:
            logger.error(f"Context object for {conversation_id} is not JSON serializable for Handoff Queue: {e}")
            return 'INTERNAL_ERROR' # JSON issue is likely an internal problem
    else:
        # Send minimal trigger message with delay to Channel Queue
        primary_channel = None
        from_id = context_object.get('from') # Used by whatsapp/sms
        from_address = context_object.get('from_address') # Used by email
        channel_type = context_object.get('channel_type')

        if channel_type in ['whatsapp', 'sms'] and from_id:
            prefix = f"{channel_type}:"
            if from_id.startswith(prefix):
                primary_channel = from_id[len(prefix):]
            else:
                logger.warning(f"Expected prefix '{prefix}' not found on from_id '{from_id}' for channel type {channel_type}. Using full value.")
                primary_channel = from_id # Fallback, might be unexpected
        elif channel_type == 'email' and from_address:
            primary_channel = from_address
        # Add elif blocks for other channel types as needed

        if not primary_channel:
            logger.error(f"Could not determine primary_channel for SQS message body for {conversation_id}. Context keys: from='{from_id}', from_address='{from_address}', channel_type='{channel_type}'.")
            return 'INTERNAL_ERROR' # Cannot proceed without primary channel

        # Construct message body with both IDs
        message_body_dict = {
            "conversation_id": conversation_id,
            "primary_channel": primary_channel
        }
        message_body = json.dumps(message_body_dict)
        delay_seconds = BATCH_WINDOW_SECONDS
        logger.info(f"Sending trigger for {conversation_id}/{primary_channel} to Channel Queue: {target_queue_url} with delay {delay_seconds}s")

    try:
        response = sqs.send_message(
            QueueUrl=target_queue_url,
            MessageBody=message_body,
            DelaySeconds=delay_seconds
        )
        logger.info(f"Successfully sent message (ID: {response.get('MessageId')}) for conversation {conversation_id} to {target_queue_url}")
        return 'SUCCESS'

    except ClientError as e:
        aws_error_code = e.response.get('Error', {}).get('Code')
        logger.error(f"SQS ClientError sending message for {conversation_id} to {target_queue_url}: {aws_error_code} - {e}")
        # Map specific AWS errors to our internal codes
        if aws_error_code in transient_sqs_errors:
            return 'SQS_TRANSIENT_ERROR'
        elif aws_error_code in config_sqs_errors:
            return 'SQS_CONFIG_ERROR'
        elif aws_error_code in parameter_sqs_errors:
            return 'SQS_PARAMETER_ERROR'
        else:
            return 'SQS_SEND_ERROR' # Generic non-transient SQS error
    except Exception as e:
        logger.exception(f"Unexpected error sending message for {conversation_id} to {target_queue_url}")
        return 'INTERNAL_ERROR' 