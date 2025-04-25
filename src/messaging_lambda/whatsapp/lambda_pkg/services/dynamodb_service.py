# services/dynamodb_service.py - Messaging Lambda (WhatsApp)

import os
import logging
import boto3
from botocore.exceptions import ClientError
from typing import Dict, Any, Tuple, Optional
from datetime import datetime, timezone
import json

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO').upper())

# Status constants for return values
LOCK_ACQUIRED = "ACQUIRED"
LOCK_EXISTS = "EXISTS"
DB_ERROR = "DB_ERROR"
# ADDED Module-level status codes for update function
DB_SUCCESS = "SUCCESS" 
DB_LOCK_LOST = "LOCK_LOST"

# Define the status value used for locking
PROCESSING_STATUS = "processing_reply"

# Initialize DynamoDB client/resource and table objects
conversations_table = None
conversations_stage_table = None
conversations_trigger_lock_table = None
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

    # Initialize conversations trigger lock table
    conversations_trigger_lock_table_name = os.environ.get('CONVERSATIONS_TRIGGER_LOCK_TABLE')
    if not conversations_trigger_lock_table_name:
        logger.critical("Missing environment variable: CONVERSATIONS_TRIGGER_LOCK_TABLE")
        raise EnvironmentError("CONVERSATIONS_TRIGGER_LOCK_TABLE environment variable not set.")
    conversations_trigger_lock_table = dynamodb_resource.Table(conversations_trigger_lock_table_name)
    logger.info(f"DynamoDB service initialized for trigger lock table: {conversations_trigger_lock_table_name}")

except EnvironmentError as env_err:
    logger.critical(f"DynamoDB Initialization Error: {env_err}")
    raise
except Exception as e:
    logger.critical(f"Failed to initialize DynamoDB resource/tables: {e}")
    conversations_table = None
    conversations_stage_table = None
    conversations_trigger_lock_table = None
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

def update_conversation_after_reply(
    primary_channel_pk: str,
    conversation_id_sk: str,
    user_message_map: Dict[str, Any],
    assistant_message_map: Dict[str, Any],
    new_status: str = "reply_sent",
    processing_time_ms: Optional[int] = None,
    task_complete: Optional[int] = None,
    hand_off_to_human: Optional[bool] = None,
    hand_off_to_human_reason: Optional[str] = None,
    updated_openai_thread_id: Optional[str] = None
) -> Tuple[str, Optional[str]]: # Return status code and error message
    """
    Performs the final update after AI processing and Twilio send.
    Atomically appends BOTH the user message and the assistant message to the history.
    Updates status, timestamps, and potentially other fields.
    Crucially uses a ConditionExpression to ensure the lock is still held.

    Args:
        primary_channel_pk: The Partition Key.
        conversation_id_sk: The Sort Key.
        user_message_map: The dictionary representing the user message.
        assistant_message_map: The dictionary representing the assistant message.
        new_status: The final status to set for the conversation.
        processing_time_ms: Optional duration in milliseconds.
        task_complete: Optional task completion status (0 or 1).
        hand_off_to_human: Optional handoff flag.
        hand_off_to_human_reason: Optional reason for handoff.
        updated_openai_thread_id: Optional updated thread ID (if applicable).

    Returns:
        A tuple: (status_code, error_message)
        Status codes: DB_SUCCESS, DB_LOCK_LOST (ConditionalCheckFailed), DB_ERROR
    """
    # Status codes defined here for clarity within function scope - REMOVED LOCAL DEFINITIONS
    # DB_SUCCESS = "SUCCESS"
    # DB_LOCK_LOST = "LOCK_LOST"
    # DB_ERROR = "DB_ERROR" - This one is already a module constant

    if not conversations_table:
        logger.error("DynamoDB conversations table not initialized. Cannot update record.")
        return DB_ERROR, "DynamoDB table not initialized"

    logger.info(f"Attempting final update for conversation {conversation_id_sk}")

    # Prepare the update expression
    update_expression_parts = []
    expression_attribute_values = {}
    expression_attribute_names = {}

    # Always update status and timestamp
    update_expression_parts.append("#status = :new_status")
    update_expression_parts.append("#updated = :ts")
    expression_attribute_names["#status"] = "conversation_status"
    expression_attribute_names["#updated"] = "updated_at"
    expression_attribute_values[":new_status"] = new_status
    expression_attribute_values[":ts"] = datetime.now(timezone.utc).isoformat()

    # Append both messages
    update_expression_parts.append("#msgs = list_append(if_not_exists(#msgs, :empty_list), :new_msgs)")
    expression_attribute_names["#msgs"] = "message_history"
    expression_attribute_values[":new_msgs"] = [user_message_map, assistant_message_map]
    expression_attribute_values[":empty_list"] = []

    # --- Conditionally add other updates --- #
    if updated_openai_thread_id:
        update_expression_parts.append("#tid = :tid")
        expression_attribute_names["#tid"] = "openai_thread_id"
        expression_attribute_values[":tid"] = updated_openai_thread_id

    if processing_time_ms is not None:
        update_expression_parts.append("#proc_time = :proc_time")
        expression_attribute_names["#proc_time"] = "initial_processing_time_ms" # Corrected attribute name
        expression_attribute_values[":proc_time"] = processing_time_ms

    if task_complete is not None: # Allows setting 0 or 1
        update_expression_parts.append("#task_comp = :task_comp")
        expression_attribute_names["#task_comp"] = "task_complete"
        expression_attribute_values[":task_comp"] = task_complete

    if hand_off_to_human is not None:
        update_expression_parts.append("#handoff = :handoff")
        expression_attribute_names["#handoff"] = "hand_off_to_human"
        expression_attribute_values[":handoff"] = hand_off_to_human

    # Only set reason if handoff is True or reason is explicitly provided
    if hand_off_to_human_reason is not None:
        update_expression_parts.append("#handoff_reason = :handoff_reason")
        expression_attribute_names["#handoff_reason"] = "hand_off_to_human_reason"
        expression_attribute_values[":handoff_reason"] = hand_off_to_human_reason
    elif hand_off_to_human: # If handoff is True but no reason given, explicitly set reason to None/Null
        update_expression_parts.append("#handoff_reason = :null_reason")
        expression_attribute_names["#handoff_reason"] = "hand_off_to_human_reason"
        expression_attribute_values[":null_reason"] = None # Let Boto3 handle None

    # --- Construct final expression --- #
    final_update_expression = "SET " + ", ".join(update_expression_parts)

    # --- Define Condition Expression (Check lock) --- #
    condition_expression = "#status = :lock_status" # Check that status IS processing_reply
    expression_attribute_values[":lock_status"] = PROCESSING_STATUS # Use the constant defined above

    logger.debug(f"Final Update Expression: {final_update_expression}")
    logger.debug(f"Condition Expression: {condition_expression}")
    logger.debug(f"Expression Attribute Values: {json.dumps(expression_attribute_values, default=str)}") # Use json.dumps for logging potentially complex values
    logger.debug(f"Expression Attribute Names: {expression_attribute_names}")

    try:
        conversations_table.update_item(
            Key={
                'primary_channel': primary_channel_pk,
                'conversation_id': conversation_id_sk
            },
            UpdateExpression=final_update_expression,
            ConditionExpression=condition_expression,
            ExpressionAttributeNames=expression_attribute_names,
            ExpressionAttributeValues=expression_attribute_values,
            ReturnValues="NONE"
        )
        logger.info(f"Successfully performed final update for conversation {conversation_id_sk}.")
        return DB_SUCCESS, None

    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code == 'ConditionalCheckFailedException':
            logger.warning(f"Final update failed for {conversation_id_sk} because lock was lost (ConditionalCheckFailedException). Status likely changed.")
            return DB_LOCK_LOST, "ConditionalCheckFailedException - Lock lost or status changed before final update."
        else:
            error_msg = f"DynamoDB ClientError during final update for {conversation_id_sk}: {e}"
            logger.error(error_msg) # Log as error, but caller handles criticality
            return DB_ERROR, error_msg
    except Exception as e:
        error_msg = f"Unexpected error during final update for {conversation_id_sk}: {e}"
        logger.exception(error_msg)
        return DB_ERROR, error_msg

# --- Cleanup Functions --- #

def cleanup_staging_table(keys_to_delete: list[dict]) -> bool:
    """
    Deletes items from the conversations-stage table using BatchWriteItem.

    Args:
        keys_to_delete: A list of key dictionaries, e.g.,
                        [{'conversation_id': '...', 'message_sid': '...'}, ...]

    Returns:
        True if the batch operation was submitted successfully (ignoring unprocessed items),
        False if there was a major error submitting the request.
    """
    if not conversations_stage_table:
        logger.error("DynamoDB staging table object not initialized. Cannot perform cleanup.")
        return False
    if not keys_to_delete:
        logger.info("No keys provided for staging table cleanup. Skipping.")
        return True # Nothing to do is considered success

    stage_table_name = conversations_stage_table.name
    logger.info(f"Attempting to batch delete {len(keys_to_delete)} items from {stage_table_name}")

    # BatchWriteItem can handle up to 25 requests at a time
    try:
        with conversations_stage_table.batch_writer() as batch:
            for key in keys_to_delete:
                # Ensure keys are present
                if 'conversation_id' in key and 'message_sid' in key:
                    batch.delete_item(Key=key)
                else:
                    logger.warning(f"Skipping invalid key in batch delete: {key}")
        # batch_writer handles retries for unprocessed items automatically
        logger.info(f"Batch delete submitted successfully for {stage_table_name}.")
        return True
    except ClientError as e:
        logger.error(f"DynamoDB ClientError during staging table cleanup batch write: {e}")
        return False
    except Exception as e:
        logger.exception(f"Unexpected error during staging table cleanup: {e}")
        return False

def cleanup_trigger_lock(conversation_id: str) -> bool:
    """
    Deletes the trigger lock item from the conversations-trigger-lock table.

    Args:
        conversation_id: The conversation ID (Partition Key).

    Returns:
        True if the delete was successful or the item didn't exist, False on error.
    """
    if not conversations_trigger_lock_table:
        logger.error("DynamoDB trigger lock table object not initialized. Cannot perform cleanup.")
        return False

    lock_table_name = conversations_trigger_lock_table.name
    logger.info(f"Attempting to delete trigger lock for {conversation_id} from {lock_table_name}")

    try:
        # Use DeleteItem - it succeeds even if the item doesn't exist
        conversations_trigger_lock_table.delete_item(
            Key={
                'conversation_id': conversation_id
            }
        )
        logger.info(f"Successfully submitted delete request for trigger lock {conversation_id}.")
        return True
    except ClientError as e:
        logger.error(f"DynamoDB ClientError deleting trigger lock for {conversation_id}: {e}")
        return False
    except Exception as e:
        logger.exception(f"Unexpected error deleting trigger lock for {conversation_id}: {e}")
        return False

def release_lock_for_retry(primary_channel: str, conversation_id: str) -> bool:
    """
    Releases the processing lock by setting the status to 'retry' when an error occurs
    during processing after the lock was acquired.

    Args:
        primary_channel: The Partition Key.
        conversation_id: The Sort Key.

    Returns:
        True if the update was submitted successfully, False otherwise.
    """
    if not conversations_table:
        logger.error("DynamoDB main conversations table not initialized. Cannot release lock.")
        return False

    logger.warning(f"Releasing lock for {primary_channel}/{conversation_id} by setting status to 'retry' due to processing error.")
    try:
        conversations_table.update_item(
            Key={
                'primary_channel': primary_channel,
                'conversation_id': conversation_id
            },
            UpdateExpression="SET conversation_status = :retry_status, updated_at = :ts",
            ExpressionAttributeValues={
                ':retry_status': "retry",
                ':ts': datetime.now(timezone.utc).isoformat()
            },
            ReturnValues="NONE"
        )
        logger.info(f"Successfully updated status to 'retry' for {primary_channel}/{conversation_id}.")
        return True
    except ClientError as e:
        logger.exception(f"DynamoDB ClientError releasing lock (setting status to retry) for {primary_channel}/{conversation_id}: {e}")
        return False
    except Exception as e:
        logger.exception(f"Unexpected error releasing lock (setting status to retry) for {primary_channel}/{conversation_id}: {e}")
        return False
