# services/twilio_service.py - Messaging Lambda (WhatsApp)

import logging
import os
from typing import Dict, Any, Optional, Tuple

from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO').upper())

# --- Status Codes --- #
TWILIO_SUCCESS = "SUCCESS"
TWILIO_TRANSIENT_ERROR = "TRANSIENT_ERROR" # Network, Twilio 5xx
TWILIO_NON_TRANSIENT_ERROR = "NON_TRANSIENT_ERROR" # Auth, Invalid number, Bad request (4xx)
TWILIO_INVALID_INPUT = "INVALID_INPUT" # Missing args to this function
# --- End Status Codes --- #

def send_whatsapp_reply(
    twilio_creds: Dict[str, str],
    recipient_number: str,
    sender_number: str,
    message_body: str
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """
    Sends a standard WhatsApp message via the Twilio API.

    Args:
        twilio_creds: Dict containing 'twilio_account_sid' and 'twilio_auth_token'.
        recipient_number: The user's phone number (e.g., +1...). No prefix needed.
        sender_number: The company's Twilio WhatsApp number (e.g., +1...). No prefix needed.
        message_body: The text content of the message to send.

    Returns:
        A tuple containing:
        - status_code (str): One of the TWILIO_* status constants.
        - result (Optional[Dict]): On SUCCESS, contains {'message_sid': str, 'body': str}.
                                   On failure, contains {'error_message': str} or None.
    """
    account_sid = twilio_creds.get('twilio_account_sid')
    auth_token = twilio_creds.get('twilio_auth_token')

    if not all([account_sid, auth_token, recipient_number, sender_number, message_body]):
        error_msg = "Missing required arguments for Twilio send_whatsapp_reply."
        logger.error(error_msg)
        return TWILIO_INVALID_INPUT, {"error_message": error_msg}

    # Add whatsapp: prefix
    formatted_recipient = f"whatsapp:{recipient_number}"
    formatted_sender = f"whatsapp:{sender_number}"

    logger.info(f"Attempting to send WhatsApp reply via Twilio.")
    logger.debug(f"  To: {formatted_recipient}")
    logger.debug(f"  From: {formatted_sender}")
    logger.debug(f"  Body: {message_body[:100]}...") # Log snippet

    try:
        client = Client(account_sid, auth_token)

        message = client.messages.create(
            from_=formatted_sender,
            to=formatted_recipient,
            body=message_body
        )

        logger.info(f"Twilio message created successfully. SID: {message.sid}, Status: {message.status}")
        # Consider checking message.status here if needed, although typically sync calls imply acceptance.

        result_payload = {
            "message_sid": message.sid,
            "body": message.body # Return the body sent
        }
        return TWILIO_SUCCESS, result_payload

    except TwilioRestException as e:
        error_msg = f"Twilio API error sending message: Status={e.status}, Code={e.code}, Message={e.msg}"
        logger.error(error_msg)
        # Basic mapping: 4xx errors are non-transient, 5xx are transient
        # See: https://www.twilio.com/docs/api/errors
        if 400 <= e.status < 500:
             # e.g., 21211 (Invalid 'To'), 21606 (From number not capable), 
             # 21408 (Permission denied), 20003 (Auth error), 21614 (Not registered number)
             # 63016 (Failed to send message - often permanent like blocked number)
            return TWILIO_NON_TRANSIENT_ERROR, {"error_message": error_msg}
        elif e.status >= 500:
            # e.g., 20500 (Twilio internal error)
            return TWILIO_TRANSIENT_ERROR, {"error_message": error_msg}
        else:
            # Unexpected status code
            logger.warning(f"Unhandled TwilioRestException status code: {e.status}")
            return TWILIO_NON_TRANSIENT_ERROR, {"error_message": error_msg} # Default to non-transient

    except Exception as e:
        # Catch any other unexpected exceptions
        error_msg = f"Unexpected error sending message via Twilio: {e}"
        logger.exception(error_msg)
        # Assume unexpected errors are potentially transient for retry
        return TWILIO_TRANSIENT_ERROR, {"error_message": error_msg} 