# Messaging Lambda Handler - WhatsApp

import json
import logging
import os

# Import services and utils
from .services import dynamodb_service
from .utils.sqs_heartbeat import SQSHeartbeat # Import the heartbeat class

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO').upper())

def handler(event, context):
    logger.info("WhatsApp Messaging Lambda triggered")
    logger.debug(f"Received event: {json.dumps(event)}")

    # Get necessary environment variables early (fail fast if missing)
    try:
        conversations_table_name = os.environ['CONVERSATIONS_TABLE']
        # Assuming this Lambda is specifically for WhatsApp queue
        # In a multi-queue single lambda, we might get this from event source ARN
        whatsapp_queue_url = os.environ['WHATSAPP_QUEUE_URL']
        sqs_heartbeat_interval_ms_str = os.environ.get("SQS_HEARTBEAT_INTERVAL_MS", "300000")
        sqs_heartbeat_interval_ms = int(sqs_heartbeat_interval_ms_str)
        if sqs_heartbeat_interval_ms <= 0:
            raise ValueError("SQS_HEARTBEAT_INTERVAL_MS must be positive")
        sqs_heartbeat_interval_sec = sqs_heartbeat_interval_ms / 1000

        logger.info(f"Using CONVERSATIONS_TABLE: {conversations_table_name}")
        logger.info(f"Using WHATSAPP_QUEUE_URL: {whatsapp_queue_url}")
        logger.info(f"Using SQS_HEARTBEAT_INTERVAL_SEC: {sqs_heartbeat_interval_sec}")

    except KeyError as e:
        logger.critical(f"Missing critical environment variable: {e}")
        # Cannot proceed without essential config, raise to indicate cold start failure
        raise EnvironmentError(f"Missing environment variable: {e}") from e
    except (ValueError, TypeError) as e:
        logger.critical(f"Invalid environment variable format: {e}")
        raise EnvironmentError(f"Invalid environment variable: {e}") from e

    # List to track failed message identifiers for SQS partial batch response
    batch_item_failures = []

    for record in event.get('Records', []):
        message_id = record.get('messageId', 'unknown')
        receipt_handle = record.get('receiptHandle')
        context_object = {} # Initialize the main context object for this record
        lock_status = None # Track if lock was acquired
        heartbeat = None   # Initialize heartbeat object reference

        try:
            logger.info(f"Processing message ID: {message_id}")

            # 1. Parse SQS message and store in context
            body_str = record.get('body')
            if not body_str:
                logger.error(f"Message {message_id} has empty body. Skipping.")
                batch_item_failures.append({"itemIdentifier": message_id})
                continue
            try:
                context_object['sqs_data'] = json.loads(body_str)
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse JSON body for message {message_id}: {e}")
                batch_item_failures.append({"itemIdentifier": message_id})
                continue

            # Extract key identifiers from SQS data for convenience
            conversation_id = context_object.get('sqs_data', {}).get('conversation_id')
            primary_channel = context_object.get('sqs_data', {}).get('primary_channel')

            if not conversation_id or not primary_channel:
                logger.error(f"Missing conversation_id or primary_channel in SQS data for message {message_id}. Body: {body_str[:200]}")
                batch_item_failures.append({"itemIdentifier": message_id})
                continue
            logger.info(f"Extracted from SQS: conversation_id={conversation_id}, primary_channel={primary_channel} for message {message_id}")

            # 2. Acquire Processing Lock
            lock_status = dynamodb_service.acquire_processing_lock(primary_channel, conversation_id)
            if lock_status == dynamodb_service.LOCK_EXISTS:
                logger.warning(f"Processing lock already held for {primary_channel}/{conversation_id}. Skipping message {message_id}.")
                continue
            elif lock_status == dynamodb_service.DB_ERROR:
                logger.error(f"Failed to acquire processing lock for {primary_channel}/{conversation_id} due to DB error. Failing message {message_id}.")
                batch_item_failures.append({"itemIdentifier": message_id})
                continue
            elif lock_status == dynamodb_service.LOCK_ACQUIRED:
                logger.info(f"Successfully acquired processing lock for {primary_channel}/{conversation_id}.")
            else:
                 logger.error(f"Unexpected lock status '{lock_status}' for {primary_channel}/{conversation_id}. Failing message {message_id}.")
                 batch_item_failures.append({"itemIdentifier": message_id})
                 continue

            # --- Step 3a: Start SQS Heartbeat --- #
            if not receipt_handle:
                logger.warning(f"Missing receiptHandle for message {message_id}, cannot start heartbeat.")
            else:
                try:
                    heartbeat = SQSHeartbeat(
                        queue_url=whatsapp_queue_url,
                        receipt_handle=receipt_handle,
                        interval_sec=int(sqs_heartbeat_interval_sec) # Ensure integer
                    )
                    heartbeat.start()
                    logger.info(f"SQS Heartbeat started for {message_id}")
                except Exception as hb_ex:
                    logger.exception(f"Failed to initialize or start SQS heartbeat for {message_id}: {hb_ex}. Processing will continue without heartbeat.")
                    heartbeat = None # Ensure heartbeat is None if start fails

            # --- Step 3: Query Staging Table --- #
            logger.info(f"Querying staging table for conversation {conversation_id}...")
            staged_items = dynamodb_service.query_staging_table(conversation_id)

            if staged_items is None:
                # Indicates a DB error occurred during the query
                logger.error(f"DB error querying staging table for {conversation_id}. Failing message {message_id}.")
                # No need to release lock here, finally block handles it
                batch_item_failures.append({"itemIdentifier": message_id})
                continue # Move to next record

            # --- Step 4: Handle Empty Batch --- #
            if not staged_items:
                logger.warning(f"No items found in staging table for conversation {conversation_id} (message {message_id}). Might be a late trigger or cleanup issue. Releasing lock and skipping.")
                # No need to release lock here, finally block handles it
                # We consider this successful processing of the *trigger message* itself
                continue # Move to next record

            # --- Step 5: Merge Batch Fragments --- #
            # Sort items by received_at timestamp, then message_sid as a tie-breaker
            try:
                staged_items.sort(key=lambda x: (x.get('received_at', ' '), x.get('message_sid', ' ')))
            except Exception as sort_ex:
                logger.exception(f"Error sorting staged items for {conversation_id}: {sort_ex}. Failing message {message_id}.")
                batch_item_failures.append({"itemIdentifier": message_id})
                continue

            # Concatenate the 'body' attributes
            combined_body = "\n".join(item.get('body', '') for item in staged_items)
            logger.info(f"Merged {len(staged_items)} fragments for conversation {conversation_id}. Total length: {len(combined_body)}")
            logger.debug(f"Combined body for {conversation_id}: {combined_body[:500]}...") # Log snippet

            # Extract primary_channel from the *first* staged item (should be consistent across items)
            # This is needed for the GetItem call in the next step
            extracted_primary_channel = staged_items[0].get('primary_channel')
            # --- ADDED: Extract message_sid of the first message --- #
            first_message_sid = staged_items[0].get('message_sid')
            # --- END ADDED --- #
            # --- ADDED: Consistency check --- #
            if primary_channel != extracted_primary_channel:
                 logger.error(f"Mismatch between SQS primary_channel ({primary_channel}) and staging table primary_channel ({extracted_primary_channel}) for {conversation_id}. Failing.")
                 batch_item_failures.append({"itemIdentifier": message_id})
                 continue
            # --- END ADDED --- #
            if not primary_channel or not first_message_sid: # Check original primary_channel and first_message_sid
                 logger.error(f"Missing primary_channel ({primary_channel}) or first_message_sid ({first_message_sid}) in first staged item for {conversation_id}. Failing message {message_id}.")
                 batch_item_failures.append({"itemIdentifier": message_id})
                 continue
            # --- We now have combined_body, primary_channel, and first_message_sid --- #

            # Store merged data
            context_object['staging_table_merged_data'] = {
                'combined_body': combined_body,
                'first_message_sid': first_message_sid
            }
            logger.info(f"Merged {len(staged_items)} fragments for conversation {conversation_id}. Stored in context_object.")

            # --- Step 6: Hydrate Canonical Conversation Row --- #
            logger.info(f"Hydrating conversation context for {conversation_id} using PK={primary_channel}...")
            # Overwrite context_object with the full record from DB
            context_object['conversations_db_data'] = dynamodb_service.get_conversation_item(primary_channel, conversation_id)

            if context_object['conversations_db_data'] is None:
                logger.error(f"Failed to hydrate conversation context for {conversation_id} (PK={primary_channel}). Cannot proceed. Failing message {message_id}.")
                # No need to release lock here, finally block handles it
                batch_item_failures.append({"itemIdentifier": message_id})
                continue # Move to next record

            logger.info(f"Successfully hydrated conversation context for {conversation_id}.")
            # context_object now holds the main conversation record's data

            # --- TODO: Implement further processing steps from LLD --- #
            # --- Next Steps: Fetch Secrets, Construct User Msg Map, Process AI --- #
            # 7. Atomic Append + Status Update (Add User Message with combined_body)
            # --- Placeholder for AI / Twilio / Further steps ---
            # 8. Fetch Updated Record for Downstream
            # 9. Send To Downstream Service/Queue
            # 10. Cleanup Staging & Trigger-Lock
            # 11. Release Processing Lock (Needs to be in finally block too)
            # 12. Stop Heartbeat & Check Errors (Done in finally block)

            # Placeholder success for now
            logger.info(f"Placeholder: Successfully processed message {message_id}")

        except Exception as e:
            # Catch-all for unexpected errors during processing of a single record
            logger.exception(f"Unhandled exception processing message {message_id}: {e}")
            batch_item_failures.append({"itemIdentifier": message_id})
            # No need to explicitly stop heartbeat here, finally block handles it.
            # No need to release lock here, finally block handles it.

        finally:
            # --- Final cleanup for this record, runs on success or exception --- # 
            heartbeat_exception = None
            if heartbeat and heartbeat.running:
                logger.info(f"Stopping heartbeat in finally block for message {message_id}...")
                heartbeat.stop()
                heartbeat_exception = heartbeat.check_for_errors()
                if heartbeat_exception:
                    logger.error(f"SQS Heartbeat for {message_id} reported an error: {heartbeat_exception}")
                    # Ensure this message is marked for failure if heartbeat failed
                    if not any(f['itemIdentifier'] == message_id for f in batch_item_failures):
                         batch_item_failures.append({"itemIdentifier": message_id})
            elif heartbeat:
                logger.debug(f"Heartbeat for {message_id} was already stopped or not running in finally block.")
            else:
                logger.debug(f"No active heartbeat to stop in finally block for {message_id}.")

            # --- Release processing lock if it was acquired --- #
            if lock_status == dynamodb_service.LOCK_ACQUIRED and primary_channel and conversation_id:
                # Determine the status to set: 'processing_error' if an exception occurred (and wasn't heartbeat error)
                # or a suitable idle status (e.g., 'pending_cleanup'?) if processing completed but maybe cleanup fails later.
                # For now, let's assume if we get here via exception path, we set error status.
                # If we get here via normal path (end of try), it will be updated later.
                final_status = "processing_error" if any(f['itemIdentifier'] == message_id for f in batch_item_failures) and not heartbeat_exception else "idle_after_processing" # Placeholder idle status
                
                # We need a release function, add TODO for now
                logger.warning(f"Attempting to release lock for {primary_channel}/{conversation_id} in finally block (status: {final_status})... (Release function TODO)")
                # dynamodb_service.release_processing_lock(primary_channel, conversation_id, new_status=final_status)

    # Return response indicating which items failed, if any
    response = {"batchItemFailures": batch_item_failures}
    logger.info(f"Lambda execution finished. Returning response: {response}")
    return response 