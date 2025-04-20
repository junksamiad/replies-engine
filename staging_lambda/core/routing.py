# webhook_handler/core/routing.py
import os
import logging

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO').upper())

# --- Queue URL Configuration ---
# Retrieve URLs from environment variables. Raise error if any are missing.

REQUIRED_ENV_VARS = [
    "HANDOFF_QUEUE_URL",
    "WHATSAPP_QUEUE_URL",
    "SMS_QUEUE_URL",
    "EMAIL_QUEUE_URL"
]

queue_urls = {}
missing_vars = []

for var_name in REQUIRED_ENV_VARS:
    url = os.environ.get(var_name)
    if not url:
        missing_vars.append(var_name)
    else:
        queue_urls[var_name] = url

if missing_vars:
    error_message = f"Missing required environment variables for SQS queues: {', '.join(missing_vars)}"
    logger.critical(error_message)
    # Raising the error here will prevent the Lambda from initializing if config is bad
    raise EnvironmentError(error_message)

# Assign to constants for clarity within the function
HANDOFF_QUEUE_URL = queue_urls["HANDOFF_QUEUE_URL"]
WHATSAPP_QUEUE_URL = queue_urls["WHATSAPP_QUEUE_URL"]
SMS_QUEUE_URL = queue_urls["SMS_QUEUE_URL"]
EMAIL_QUEUE_URL = queue_urls["EMAIL_QUEUE_URL"]

def determine_target_queue(context_object):
    """Determines the target SQS queue URL based on routing rules."""
    logger.info("Determining target queue...")
    channel_type = context_object.get('channel_type')

    # --- Check 1: Explicit handoff flag (Temporarily Commented Out) ---
    # Note: AI might set this later in the process. This check handles pre-set flags.
    # if context_object.get('hand_off_to_human') is True:
    #     print("Routing based on hand_off_to_human=True")
    #     return HANDOFF_QUEUE_URL
    # --- End Check 1 ---

    # 2. Check global auto_queue_reply_message flag
    if context_object.get('auto_queue_reply_message') is True:
        logger.info("Routing based on auto_queue_reply_message=True")
        return HANDOFF_QUEUE_URL
    
    # 3. Check channel-specific auto_queue list
    # Assumes context_object was updated by check_conversation_exists 
    # and contains recipient_tel/recipient_email from the DB record.
    if channel_type in ['whatsapp', 'sms']:
        # Ensure the list exists and handle None case gracefully
        auto_queue_numbers = context_object.get('auto_queue_reply_message_from_number', []) or [] 
        recipient_tel = context_object.get('recipient_tel') 
        if recipient_tel and recipient_tel in auto_queue_numbers:
             logger.info(f"Routing based on recipient_tel '{recipient_tel}' found in auto_queue_reply_message_from_number")
             return HANDOFF_QUEUE_URL
    elif channel_type == 'email':
        # Ensure the list exists and handle None case gracefully
        auto_queue_emails = context_object.get('auto_queue_reply_message_from_email', []) or [] 
        recipient_email = context_object.get('recipient_email')
        if recipient_email and recipient_email in auto_queue_emails:
             logger.info(f"Routing based on recipient_email '{recipient_email}' found in auto_queue_reply_message_from_email")
             return HANDOFF_QUEUE_URL

    # 4. Default to channel-specific batch queue (Happy Path)
    logger.info("Defaulting to channel-specific queue.")
    if channel_type == 'whatsapp':
        return WHATSAPP_QUEUE_URL
    elif channel_type == 'sms':
        return SMS_QUEUE_URL
    elif channel_type == 'email':
        return EMAIL_QUEUE_URL
    else:
        # Should not happen if validation passed, but handle defensively
        logger.error(f"Cannot determine queue for unknown channel_type: {channel_type}")
        return None # Indicate routing failure 