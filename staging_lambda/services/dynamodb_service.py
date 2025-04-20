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

try:
    dynamodb = boto3.resource('dynamodb')
    stage_table = dynamodb.Table(STAGE_TABLE_NAME)
    lock_table = dynamodb.Table(LOCK_TABLE_NAME)
    logger.info(f"Initialized DynamoDB service for tables: {STAGE_TABLE_NAME}, {LOCK_TABLE_NAME}")
except Exception as e:
    logger.critical(f"Failed to initialize DynamoDB resource or tables: {e}")
    # Depending on deployment strategy, might want to raise here to fail init
    raise

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