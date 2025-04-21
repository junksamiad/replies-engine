# webhook_handler/services/dynamodb_service.py
import os
import time
import datetime
import logging
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO').upper())

# --- Configuration ---
STAGE_TABLE_NAME = os.environ.get('STAGE_TABLE_NAME', 'conversations-stage-test')
LOCK_TABLE_NAME = os.environ.get('LOCK_TABLE_NAME', 'conversations-trigger-lock-test')
# Batch window (W) in seconds - SQS DelaySeconds
BATCH_WINDOW_SECONDS = int(os.environ.get('BATCH_WINDOW_SECONDS', '10'))
# Safety buffer for TTL calculations
TTL_BUFFER_SECONDS = int(os.environ.get('TTL_BUFFER_SECONDS', '60'))

# --- Boto3 Initialization & Error Code Lists ---
transient_ddb_errors = [
    'ProvisionedThroughputExceededException',
    'InternalServerError',
    'ThrottlingException',
    'RequestLimitExceeded'
]
config_ddb_errors = [
    'ResourceNotFoundException',
    'AccessDeniedException'
]
validation_ddb_errors = ['ValidationException']

conversations_table = None # Add variable for conversations table
CONVERSATIONS_TABLE_NAME = os.environ.get('CONVERSATIONS_TABLE_NAME', 'ai-multi-comms-conversations-dev') # Added conversations table name

try:
    dynamodb = boto3.resource('dynamodb')
    stage_table = dynamodb.Table(STAGE_TABLE_NAME)
    lock_table = dynamodb.Table(LOCK_TABLE_NAME)
    conversations_table = dynamodb.Table(CONVERSATIONS_TABLE_NAME) # Initialize conversations table
    logger.info(f"Initialized DynamoDB service for tables: {STAGE_TABLE_NAME}, {LOCK_TABLE_NAME}, {CONVERSATIONS_TABLE_NAME}")
except Exception as e:
    logger.critical(f"Failed to initialize DynamoDB resource or tables: {e}")
    # Depending on deployment strategy, might want to raise here to fail init
    raise

# Mapping from channel_type to GSI details
GSI_CONFIG = {
    'whatsapp': {
        'index_name': 'company-whatsapp-number-recipient-tel-index',
        'pk_name': 'gsi_company_whatsapp_number',
        'sk_name': 'gsi_recipient_tel',
        'credential_key': 'whatsapp_credentials_id'
    },
    'sms': {
        'index_name': 'company-sms-number-recipient-tel-index',
        'pk_name': 'gsi_company_sms_number',
        'sk_name': 'gsi_recipient_tel',
        'credential_key': 'sms_credentials_id'
    },
    'email': {
        'index_name': 'company-email-recipient-email-index',
        'pk_name': 'gsi_company_email',
        'sk_name': 'gsi_recipient_email',
        'credential_key': 'email_credentials_id'
    }
    # Add other channels here
}

def get_credential_ref_for_validation(channel_type, from_id, to_id):
    """
    Queries the appropriate GSI based on channel type to find the conversation
    record and retrieve the channel_config for credential lookup.

    Args:
        channel_type (str): 'whatsapp', 'sms', or 'email'.
        from_id (str): The sender identifier (e.g., phone number, email address).
        to_id (str): The recipient identifier (e.g., company number, email address).

    Returns:
        dict: A dictionary containing:
              {'status': 'FOUND', 'credential_ref': 'secret_id_value', 'conversation_id': 'conv_id'} on success.
              {'status': 'NOT_FOUND'} if no matching record.
              {'status': 'MISSING_CREDENTIAL_CONFIG'} if record found but key missing.
              {'status': 'UNSUPPORTED_CHANNEL'} if channel_type is invalid.
              Other specific error codes on DB failure (e.g., 'DB_TRANSIENT_ERROR').
    """
    if channel_type not in GSI_CONFIG:
        logger.error(f"Unsupported channel_type provided for GSI lookup: {channel_type}")
        return {'status': 'UNSUPPORTED_CHANNEL'}

    config = GSI_CONFIG[channel_type]
    index_name = config['index_name']
    pk_name = config['pk_name']
    sk_name = config['sk_name']
    credential_key = config['credential_key']

    # Note: In GSI, the PK/SK names map to attributes in the main table.
    # The values used are the 'to_id' (company identifier) and 'from_id' (user identifier).
    gsi_pk_value = to_id # Company identifier is the GSI PK
    gsi_sk_value = from_id # User identifier is the GSI SK

    # --- ADD PREFIX STRIPPING LOGIC --- #
    if channel_type in ['whatsapp', 'sms']:
        logger.debug(f"Stripping prefixes for {channel_type} query...")
        prefix = f"{channel_type}:"
        if gsi_pk_value and gsi_pk_value.startswith(prefix):
            gsi_pk_value = gsi_pk_value[len(prefix):]
            logger.debug(f"Stripped PK value: {gsi_pk_value}")
        if gsi_sk_value and gsi_sk_value.startswith(prefix):
            gsi_sk_value = gsi_sk_value[len(prefix):]
            logger.debug(f"Stripped SK value: {gsi_sk_value}")
    # --- END PREFIX STRIPPING LOGIC --- #

    logger.info(f"Querying GSI '{index_name}' on table '{CONVERSATIONS_TABLE_NAME}' with {pk_name}={gsi_pk_value}, {sk_name}={gsi_sk_value}")

    try:
        response = conversations_table.query(
            IndexName=index_name,
            KeyConditionExpression=f'{pk_name} = :pk AND {sk_name} = :sk',
            ExpressionAttributeValues={
                ':pk': gsi_pk_value,
                ':sk': gsi_sk_value
            },
            ProjectionExpression='channel_config, conversation_id', # Fetch ONLY channel_config and conversation_id
            Limit=1
        )

        items = response.get('Items', [])

        if not items:
            logger.warning(f"No record found in GSI '{index_name}' for {pk_name}={gsi_pk_value}, {sk_name}={gsi_sk_value}")
            return {'status': 'NOT_FOUND'}

        item = items[0]
        conversation_id = item.get('conversation_id') # Get the main table SK
        channel_config = item.get('channel_config', {})
        credential_ref = channel_config.get(credential_key)

        if not credential_ref:
            logger.error(f"Record found for {conversation_id}, but missing '{credential_key}' in channel_config: {channel_config}")
            return {'status': 'MISSING_CREDENTIAL_CONFIG', 'conversation_id': conversation_id}

        logger.info(f"Found credential reference '{credential_ref}' for conversation {conversation_id}")
        return {
            'status': 'FOUND',
            'credential_ref': credential_ref,
            'conversation_id': conversation_id
        }

    except ClientError as e:
        aws_error_code = e.response.get('Error', {}).get('Code')
        logger.error(f"DynamoDB ClientError querying GSI '{index_name}' for {gsi_pk_value}/{gsi_sk_value}: {aws_error_code} - {e}")
        if aws_error_code in transient_ddb_errors:
            return {'status': 'DB_TRANSIENT_ERROR'}
        elif aws_error_code in config_ddb_errors:
            return {'status': 'DB_CONFIG_ERROR'}
        elif aws_error_code in validation_ddb_errors:
            return {'status': 'DB_VALIDATION_ERROR'}
        else:
            return {'status': 'DB_QUERY_ERROR'}
    except Exception as e:
        logger.exception(f"Unexpected error querying GSI '{index_name}' for {gsi_pk_value}/{gsi_sk_value}")
        return {'status': 'INTERNAL_ERROR'}

def get_full_conversation(primary_channel, conversation_id):
    """
    Retrieves the full conversation item from the main table using its composite PK.

    Args:
        primary_channel (str): The partition key value.
        conversation_id (str): The sort key value.

    Returns:
        dict: A dictionary containing:
              {'status': 'FOUND', 'data': full_item_dict} on success.
              {'status': 'NOT_FOUND'} if no matching record.
              Other specific error codes on DB failure (e.g., 'DB_TRANSIENT_ERROR').
    """
    if not primary_channel or not conversation_id:
        logger.error(f"get_full_conversation called with empty primary_channel ('{primary_channel}') or conversation_id ('{conversation_id}').")
        return {'status': 'INTERNAL_ERROR'}

    # --- ADD TYPE CHECK LOGGING --- #
    logger.info(f"Preparing GetItem. Key values: primary_channel='{primary_channel}' (Type: {type(primary_channel)}), conversation_id='{conversation_id}' (Type: {type(conversation_id)})")
    # --- END TYPE CHECK LOGGING --- #

    logger.info(f"Attempting to get full conversation item for PK={primary_channel}, SK={conversation_id} from {CONVERSATIONS_TABLE_NAME}")
    try:
        response = conversations_table.get_item(
            Key={
                'primary_channel': primary_channel,
                'conversation_id': conversation_id
                }
        )

        item = response.get('Item')

        if not item:
            logger.warning(f"No conversation record found for PK={primary_channel}, SK={conversation_id}")
            return {'status': 'NOT_FOUND'}

        logger.info(f"Successfully retrieved full conversation item for {conversation_id}")
        return {'status': 'FOUND', 'data': item}

    except ClientError as e:
        aws_error_code = e.response.get('Error', {}).get('Code')
        logger.error(f"DynamoDB ClientError getting item for {primary_channel}/{conversation_id}: {aws_error_code} - {e}")
        if aws_error_code in transient_ddb_errors:
            return {'status': 'DB_TRANSIENT_ERROR'}
        elif aws_error_code in config_ddb_errors:
            return {'status': 'DB_CONFIG_ERROR'}
        elif aws_error_code in validation_ddb_errors:
            return {'status': 'DB_VALIDATION_ERROR'}
        else:
            return {'status': 'DB_GET_ITEM_ERROR'}
    except Exception as e:
        logger.exception(f"Unexpected error getting item for {primary_channel}/{conversation_id}")
        return {'status': 'INTERNAL_ERROR'}

# --- Service Functions ---

def write_to_stage_table(context_object):
    """
    Writes the essential message fragment details to the staging table.
    Returns a status code string: 'SUCCESS', 'STAGE_DB_TRANSIENT_ERROR',
    'STAGE_DB_CONFIG_ERROR', 'STAGE_DB_VALIDATION_ERROR', 'STAGE_WRITE_ERROR', or 'INTERNAL_ERROR'.
    """
    if not context_object:
        logger.error("write_to_stage_table called with empty context_object.")
        return 'INTERNAL_ERROR'

    conversation_id = context_object.get('conversation_id')
    # Use message_sid as the sort key for staging
    message_sid = context_object.get('message_sid')

    if not conversation_id or not message_sid:
        logger.error(f"Missing conversation_id or message_sid in context_object for staging write: {context_object}")
        # Return a specific code for bad input if needed, or treat as internal error?
        return 'INTERNAL_ERROR' # Or a new code like 'BAD_INPUT'

    try:
        current_time_epoch = int(time.time())
        expires_at = current_time_epoch + BATCH_WINDOW_SECONDS + TTL_BUFFER_SECONDS
        # Use consistent timestamp for received_at and TTL calculation base
        received_at_iso = datetime.datetime.fromtimestamp(current_time_epoch).isoformat()

        stage_item = {
            'conversation_id': conversation_id, # PK
            'message_sid': message_sid, # SK
            'primary_channel': context_object.get('primary_channel'), # Company channel identifier
            'body': context_object.get('body'),
            'received_at': received_at_iso,
            'expires_at': expires_at
        }

        # Remove None values for optional fields
        stage_item = {k: v for k, v in stage_item.items() if v is not None}

        logger.debug(f"Attempting to write to stage table ({STAGE_TABLE_NAME}): {stage_item}")
        stage_table.put_item(Item=stage_item)
        logger.info(f"Successfully staged message {message_sid} for conversation {conversation_id}")
        return 'SUCCESS'

    except ClientError as e:
        aws_error_code = e.response.get('Error', {}).get('Code')
        logger.error(f"DynamoDB ClientError writing to stage table {STAGE_TABLE_NAME} for {conversation_id}/{message_sid}: {aws_error_code} - {e}")
        if aws_error_code in transient_ddb_errors:
            return 'STAGE_DB_TRANSIENT_ERROR'
        elif aws_error_code in config_ddb_errors:
            return 'STAGE_DB_CONFIG_ERROR'
        elif aws_error_code in validation_ddb_errors:
            return 'STAGE_DB_VALIDATION_ERROR'
        else:
            return 'STAGE_WRITE_ERROR' # Generic non-transient DDB error
    except Exception as e:
        logger.exception(f"Unexpected error writing to stage table {STAGE_TABLE_NAME} for {conversation_id}/{message_sid}")
        return 'INTERNAL_ERROR'


def acquire_trigger_lock(conversation_id):
    """
    Attempts to acquire the trigger scheduling lock for a conversation.
    Returns a status code string: 'ACQUIRED', 'EXISTS', 'TRIGGER_DB_TRANSIENT_ERROR',
    'TRIGGER_DB_CONFIG_ERROR', 'TRIGGER_DB_VALIDATION_ERROR', 'TRIGGER_LOCK_WRITE_ERROR', or 'INTERNAL_ERROR'.
    """
    if not conversation_id:
        logger.error("acquire_trigger_lock called with empty conversation_id.")
        return 'INTERNAL_ERROR' # Or 'BAD_INPUT'

    try:
        current_time_epoch = int(time.time())
        # TTL ensures lock eventually expires even if MessagingLambda fails cleanup
        expires_at = current_time_epoch + BATCH_WINDOW_SECONDS + TTL_BUFFER_SECONDS

        lock_item = {
            'conversation_id': conversation_id,
            'expires_at': expires_at
        }

        logger.debug(f"Attempting to acquire trigger lock for {conversation_id} in {LOCK_TABLE_NAME}")
        lock_table.put_item(
            Item=lock_item,
            ConditionExpression='attribute_not_exists(conversation_id)'
        )
        logger.info(f"Successfully acquired trigger lock for conversation {conversation_id}")
        return 'ACQUIRED'

    except ClientError as e:
        aws_error_code = e.response.get('Error', {}).get('Code')
        if aws_error_code == 'ConditionalCheckFailedException':
            logger.info(f"Trigger lock already exists for conversation {conversation_id}. Condition check failed.")
            return 'EXISTS'
        else:
            logger.error(f"DynamoDB ClientError acquiring trigger lock for {conversation_id}: {aws_error_code} - {e}")
            # Map specific AWS errors to our internal codes
            if aws_error_code in transient_ddb_errors:
                return 'TRIGGER_DB_TRANSIENT_ERROR'
            elif aws_error_code in config_ddb_errors:
                return 'TRIGGER_DB_CONFIG_ERROR'
            elif aws_error_code in validation_ddb_errors:
                return 'TRIGGER_DB_VALIDATION_ERROR'
            else:
                return 'TRIGGER_LOCK_WRITE_ERROR'
    except Exception as e:
        logger.exception(f"Unexpected error acquiring trigger lock for {conversation_id}")
        return 'INTERNAL_ERROR' 