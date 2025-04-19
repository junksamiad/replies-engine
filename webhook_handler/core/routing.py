# webhook_handler/core/routing.py
import os

# Placeholder Queue Names/URLs - these should come from config/env vars
# TODO: Replace with environment variable lookups using os.environ.get
HANDOFF_QUEUE_URL = os.environ.get("HANDOFF_QUEUE_URL", "YOUR_HANDOFF_QUEUE_URL_PLACEHOLDER")
WHATSAPP_QUEUE_URL = os.environ.get("WHATSAPP_QUEUE_URL", "YOUR_WHATSAPP_QUEUE_URL_PLACEHOLDER")
SMS_QUEUE_URL = os.environ.get("SMS_QUEUE_URL", "YOUR_SMS_QUEUE_URL_PLACEHOLDER")
EMAIL_QUEUE_URL = os.environ.get("EMAIL_QUEUE_URL", "YOUR_EMAIL_QUEUE_URL_PLACEHOLDER")

def determine_target_queue(context_object):
    """Determines the target SQS queue URL based on routing rules."""
    print("Determining target queue...")
    channel_type = context_object.get('channel_type')

    # --- Check 1: Explicit handoff flag (Temporarily Commented Out) ---
    # Note: AI might set this later in the process. This check handles pre-set flags.
    # if context_object.get('hand_off_to_human') is True:
    #     print("Routing based on hand_off_to_human=True")
    #     return HANDOFF_QUEUE_URL
    # --- End Check 1 ---

    # 2. Check global auto_queue_reply_message flag
    if context_object.get('auto_queue_reply_message') is True:
        print("Routing based on auto_queue_reply_message=True")
        return HANDOFF_QUEUE_URL
    
    # 3. Check channel-specific auto_queue list
    # Assumes context_object was updated by check_conversation_exists 
    # and contains recipient_tel/recipient_email from the DB record.
    if channel_type in ['whatsapp', 'sms']:
        # Ensure the list exists and handle None case gracefully
        auto_queue_numbers = context_object.get('auto_queue_reply_message_from_number', []) or [] 
        recipient_tel = context_object.get('recipient_tel') 
        if recipient_tel and recipient_tel in auto_queue_numbers:
             print(f"Routing based on recipient_tel '{recipient_tel}' found in auto_queue_reply_message_from_number")
             return HANDOFF_QUEUE_URL
    elif channel_type == 'email':
        # Ensure the list exists and handle None case gracefully
        auto_queue_emails = context_object.get('auto_queue_reply_message_from_email', []) or [] 
        recipient_email = context_object.get('recipient_email')
        if recipient_email and recipient_email in auto_queue_emails:
             print(f"Routing based on recipient_email '{recipient_email}' found in auto_queue_reply_message_from_email")
             return HANDOFF_QUEUE_URL

    # 4. Default to channel-specific batch queue (Happy Path)
    print("Defaulting to channel-specific queue.")
    if channel_type == 'whatsapp':
        return WHATSAPP_QUEUE_URL
    elif channel_type == 'sms':
        return SMS_QUEUE_URL
    elif channel_type == 'email':
        return EMAIL_QUEUE_URL
    else:
        # Should not happen if validation passed, but handle defensively
        print(f"ERROR: Cannot determine queue for unknown channel_type: {channel_type}")
        return None # Indicate routing failure 