# services/dynamodb_service.py - Messaging Lambda (WhatsApp)

import os
import logging
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO').upper())

# Status constants for return values
LOCK_ACQUIRED = "ACQUIRED"
LOCK_EXISTS = "EXISTS"
DB_ERROR = "DB_ERROR"

# Define the status value used for locking
PROCESSING_STATUS = "processing_reply"

# Initialize DynamoDB client/resource and table objects
conversations_table = None
conversations_stage_table = None
try:
    dynamodb_resource = boto3.resource('dynamodb')

    # Initialize main conversations table
    conversations_table_name = os.environ.get('CONVERSATIONS_TABLE')
    if not conversations_table_name:
        logger.critical("Missing environment variable: CONVERSATIONS_TABLE")
        raise EnvironmentError("CONVERSATIONS_TABLE environment variable not set.")
    conversations_table = dynamodb_resource.Table(conversations_table_name)
    logger.info(f"DynamoDB service initialized for table: {conversations_table_name}")

    # Initialize conversations stage table
    conversations_stage_table_name = os.environ.get('CONVERSATIONS_STAGE_TABLE')
    if not conversations_stage_table_name:
        logger.critical("Missing environment variable: CONVERSATIONS_STAGE_TABLE")
        raise EnvironmentError("CONVERSATIONS_STAGE_TABLE environment variable not set.")
    conversations_stage_table = dynamodb_resource.Table(conversations_stage_table_name)
    logger.info(f"DynamoDB service initialized for staging table: {conversations_stage_table_name}")

except EnvironmentError as env_err:
    logger.critical(f"DynamoDB Initialization Error: {env_err}")
    # Let the error propagate or handle as needed; table objects remain None
    raise
except Exception as e:
    logger.critical(f"Failed to initialize DynamoDB resource/tables: {e}")
    # Ensure table objects are None if init fails unexpectedly
    conversations_table = None
    conversations_stage_table = None
    # raise # Optional: Fail fast during init

def acquire_processing_lock(primary_channel: str, conversation_id: str) -> str:
    """
    Attempts to acquire a processing lock on the conversation item using a conditional update.

    Args:
        primary_channel: The Partition Key (e.g., user's identifier).
        conversation_id: The Sort Key.

    Returns:
        str: Status code (LOCK_ACQUIRED, LOCK_EXISTS, DB_ERROR).
    """
    if not conversations_table:
        logger.error("DynamoDB main conversations table not initialized. Cannot acquire lock.")
        return DB_ERROR

    logger.info(f"Attempting to acquire lock for {primary_channel}/{conversation_id}")
    try:
        conversations_table.update_item(
            Key={
                'primary_channel': primary_channel,
                'conversation_id': conversation_id
            },
            UpdateExpression="SET conversation_status = :proc_status",
            ConditionExpression="attribute_not_exists(conversation_status) OR conversation_status <> :proc_status",
            ExpressionAttributeValues={':proc_status': PROCESSING_STATUS}
        )
        logger.info(f"Successfully acquired lock for {primary_channel}/{conversation_id}")
        return LOCK_ACQUIRED

    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            logger.warning(f"Lock already exists for {primary_channel}/{conversation_id} (ConditionalCheckFailedException)")
            return LOCK_EXISTS
        else:
            logger.exception(f"DynamoDB ClientError acquiring lock for {primary_channel}/{conversation_id}: {e}")
            return DB_ERROR
    except Exception as e:
        logger.exception(f"Unexpected error acquiring lock for {primary_channel}/{conversation_id}: {e}")
        return DB_ERROR

def query_staging_table(conversation_id: str) -> list | None:
    """
    Queries the conversations-stage table for all message fragments for a given conversation ID.
    Uses a strongly consistent read.

    Args:
        conversation_id: The conversation ID (Partition Key).

    Returns:
        A list of item dictionaries (message fragments), or None if an error occurs.
        Returns an empty list if no items are found.
    """
    if not conversations_stage_table:
        logger.error("DynamoDB staging table object not initialized. Cannot query staging table.")
        return None # Indicate error

    logger.info(f"Querying staging table for conversation_id: {conversation_id}")
    try:
        response = conversations_stage_table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key('conversation_id').eq(conversation_id),
            ConsistentRead=True # Ensure we read the latest writes from StagingLambda
        )
        items = response.get('Items', [])
        logger.info(f"Found {len(items)} items in staging table for conversation_id: {conversation_id}")
        return items

    except ClientError as e:
        logger.exception(f"DynamoDB ClientError querying staging table for {conversation_id}: {e}")
        return None # Indicate error
    except Exception as e:
        logger.exception(f"Unexpected error querying staging table for {conversation_id}: {e}")
        return None # Indicate error

def get_conversation_item(primary_channel: str, conversation_id: str) -> dict | None:
    """
    Fetches the full conversation item from the main ConversationsTable.
    Uses a strongly consistent read.

    Args:
        primary_channel: The Partition Key.
        conversation_id: The Sort Key.

    Returns:
        The conversation item dictionary if found, None if not found or an error occurs.
    """
    if not conversations_table:
        logger.error("DynamoDB main conversations table not initialized. Cannot get item.")
        return None

    logger.info(f"Fetching conversation item for PK={primary_channel}, SK={conversation_id}")
    try:
        response = conversations_table.get_item(
            Key={
                'primary_channel': primary_channel,
                'conversation_id': conversation_id
            },
            ConsistentRead=True
        )
        item = response.get('Item')
        if not item:
            logger.error(f"Conversation item not found for PK={primary_channel}, SK={conversation_id}")
            return None # Explicitly return None for not found
        else:
            logger.info(f"Successfully fetched conversation item for PK={primary_channel}, SK={conversation_id}")
            return item

    except ClientError as e:
        logger.exception(f"DynamoDB ClientError getting item for {primary_channel}/{conversation_id}: {e}")
        return None # Indicate error
    except Exception as e:
        logger.exception(f"Unexpected error getting item for {primary_channel}/{conversation_id}: {e}")
        return None # Indicate error

# --- TODO: Add other DynamoDB interaction functions below --- #
# - hydrate_conversation_row (GetItem)
# - atomic_append_user_message (UpdateItem)
# - cleanup_staging_table (BatchWriteItem)
# - cleanup_trigger_lock (DeleteItem)
# - release_processing_lock (UpdateItem)
# - update_status_on_failure (UpdateItem) 