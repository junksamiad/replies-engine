# Messaging Lambda Handler - WhatsApp

import json
import logging
import os
import datetime # Need datetime
import time # Import time for duration calculation

# Import services and utils
from .services import dynamodb_service
from .services import secrets_manager_service # Import secrets service
from .core import openai_service # Import AI service
from .services import twilio_service # Import Twilio service
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
        primary_channel = None # Keep track for finally block
        conversation_id = None # Keep track for finally block
        processing_start_time = time.time() # Capture start time

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

            # --- Step 8: Fetch Secrets --- #
            logger.info(f"Fetching secrets for conversation {conversation_id}...")
            context_object['secrets'] = {} # Initialize secrets dict
            fetch_error_status = None # Track if any fetch fails permanently or transiently
            error_details = ""

            # Extract refs from hydrated DB data
            db_data = context_object.get('conversations_db_data', {})
            ai_config = db_data.get('ai_config', {}) # This map is now the channel-specific config
            channel_config = db_data.get('channel_config', {}) # This map contains channel config
            openai_secret_ref = ai_config.get('api_key_reference')
            twilio_secret_ref = channel_config.get('whatsapp_credentials_id')

            # --- Fetch OpenAI Secret --- #
            if not openai_secret_ref:
                logger.error("Missing OpenAI secret reference in conversation config.")
                fetch_error_status = secrets_manager_service.SECRET_INVALID_INPUT # Treat missing ref as permanent error
                error_details = "Missing OpenAI secret reference"
            else:
                openai_status, openai_secret = secrets_manager_service.get_secret(openai_secret_ref)
                if openai_status == secrets_manager_service.SECRET_SUCCESS:
                    context_object['secrets']['openai'] = openai_secret
                    logger.info(f"Successfully fetched OpenAI secret ({openai_secret_ref})")
                elif openai_status == secrets_manager_service.SECRET_TRANSIENT_ERROR:
                    logger.warning(f"Transient error fetching OpenAI secret: {openai_secret_ref}")
                    fetch_error_status = openai_status # Mark as transient
                    error_details = f"Transient error fetching OpenAI secret {openai_secret_ref}"
                else: # Permanent errors (NOT_FOUND, PERMANENT_ERROR, INIT_ERROR, INVALID_INPUT)
                    logger.error(f"Permanent error ({openai_status}) fetching OpenAI secret: {openai_secret_ref}")
                    fetch_error_status = openai_status # Mark as permanent
                    error_details = f"Permanent error ({openai_status}) fetching OpenAI secret {openai_secret_ref}"

            # --- Fetch Twilio Secret (only if OpenAI fetch didn't already fail permanently/transiently) --- #
            if fetch_error_status is None:
                if not twilio_secret_ref:
                    logger.error("Missing Twilio secret reference in conversation config.")
                    fetch_error_status = secrets_manager_service.SECRET_INVALID_INPUT
                    error_details = "Missing Twilio secret reference"
                else:
                    twilio_status, twilio_secret = secrets_manager_service.get_secret(twilio_secret_ref)
                    if twilio_status == secrets_manager_service.SECRET_SUCCESS:
                        context_object['secrets']['twilio'] = twilio_secret
                        logger.info(f"Successfully fetched Twilio secret ({twilio_secret_ref})")
                    elif twilio_status == secrets_manager_service.SECRET_TRANSIENT_ERROR:
                        logger.warning(f"Transient error fetching Twilio secret: {twilio_secret_ref}")
                        fetch_error_status = twilio_status
                        error_details = f"Transient error fetching Twilio secret {twilio_secret_ref}"
                    else: # Permanent errors
                        logger.error(f"Permanent error ({twilio_status}) fetching Twilio secret: {twilio_secret_ref}")
                        fetch_error_status = twilio_status
                        error_details = f"Permanent error ({twilio_status}) fetching Twilio secret {twilio_secret_ref}"

            # --- Handle Fetch Outcome --- #
            if fetch_error_status == secrets_manager_service.SECRET_TRANSIENT_ERROR:
                # Raise exception to trigger SQS retry
                logger.warning(f"Secrets fetch failed with transient error for conversation {conversation_id}. Raising exception for retry.")
                raise Exception(f"Transient Secrets Fetch Error: {error_details}")
            elif fetch_error_status is not None:
                # Permanent error occurred, fail the SQS message
                logger.error(f"Halting processing for message {message_id} due to permanent secret fetch error ({fetch_error_status}). Details: {error_details}")
                batch_item_failures.append({"itemIdentifier": message_id})
                continue # Move to next SQS record
            else:
                 # All secrets fetched successfully
                 logger.info(f"Successfully fetched and stored all required secrets for {conversation_id}.")

            # --- Step 9: Process with AI --- #
            logger.info(f"Starting AI processing for conversation {conversation_id}...")

            # Extract necessary inputs for AI service
            ai_input_thread_id = context_object.get('conversations_db_data', {}).get('thread_id')
            ai_input_assistant_id = ai_config.get('assistant_id_replies')
            ai_input_user_message = context_object.get('staging_table_merged_data', {}).get('combined_body')
            openai_creds = context_object.get('secrets', {}).get('openai')
            ai_input_api_key = openai_creds.get('ai_api_key') if openai_creds else None

            # Validate required AI inputs
            if not ai_input_thread_id:
                logger.error(f"Missing required openai_thread_id for conversation {conversation_id}. Cannot proceed with AI reply.")
                batch_item_failures.append({"itemIdentifier": message_id})
                continue
            if not ai_input_assistant_id:
                logger.error(f"Missing required assistant_id_replies in config for conversation {conversation_id}. Cannot proceed.")
                batch_item_failures.append({"itemIdentifier": message_id})
                continue
            if not ai_input_user_message:
                logger.error(f"Missing combined_body for AI input for conversation {conversation_id}. Cannot proceed.")
                batch_item_failures.append({"itemIdentifier": message_id})
                continue
            if not ai_input_api_key:
                logger.error(f"Missing OpenAI API key after secret fetch for conversation {conversation_id}. Cannot proceed.")
                batch_item_failures.append({"itemIdentifier": message_id})
                continue

            # Call the AI service function
            ai_status, ai_result_payload = openai_service.process_reply_with_ai(
                thread_id=ai_input_thread_id,
                assistant_id=ai_input_assistant_id,
                user_message_content=ai_input_user_message,
                api_key=ai_input_api_key
            )

            # Handle AI processing results
            if ai_status == openai_service.AI_SUCCESS:
                # Store successful AI response in context object
                context_object['open_ai_response'] = ai_result_payload
                logger.info(f"Successfully completed AI processing for conversation {conversation_id}. Response content length: {len(ai_result_payload.get('response_content', ''))}")
                logger.debug(f"AI Response details: {ai_result_payload}")
            elif ai_status == openai_service.AI_TRANSIENT_ERROR:
                # Raise an exception to trigger SQS retry for transient errors
                error_msg = ai_result_payload.get("error_message", "Unknown transient AI error") if ai_result_payload else "Unknown transient AI error"
                logger.warning(f"AI processing failed with transient error for conversation {conversation_id}: {error_msg}. Raising exception for retry.")
                raise Exception(f"Transient AI Error: {error_msg}") # Raise exception for SQS retry
            else: # Covers AI_NON_TRANSIENT_ERROR and AI_INVALID_INPUT
                # Log the specific non-transient error and mark message for failure (DLQ)
                error_msg = ai_result_payload.get("error_message", "Unknown non-transient AI error") if ai_result_payload else f"Unknown non-transient AI error ({ai_status})"
                logger.error(f"AI processing failed with non-transient error ({ai_status}) for conversation {conversation_id}: {error_msg}. Failing message {message_id}.")
                batch_item_failures.append({"itemIdentifier": message_id})
                continue # Move to the next SQS record

            # --- Step 10: Send Reply via Channel Provider (Twilio) --- #
            logger.info(f"Sending reply via Twilio for conversation {conversation_id}...")

            # Extract necessary inputs for Twilio service
            twilio_creds = context_object.get('secrets', {}).get('twilio')
            recipient_num = primary_channel # Already extracted
            sender_num = channel_config.get('company_whatsapp_number')
            
            # Get the raw response string from AI
            raw_reply_content = context_object.get('open_ai_response', {}).get('response_content')

            # --- ADDED: Parse JSON and Extract Content --- #
            final_reply_body = None
            if raw_reply_content:
                try:
                    parsed_response = json.loads(raw_reply_content)
                    if isinstance(parsed_response, dict) and 'content' in parsed_response:
                        final_reply_body = parsed_response['content']
                        logger.info("Successfully parsed AI response and extracted content.")
                        logger.debug(f"Extracted final reply body: {final_reply_body[:200]}...")
                    else:
                        logger.error(f"Parsed AI response is not a dict or missing 'content' key. Raw: {raw_reply_content[:500]}")
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse AI response as JSON: {e}. Raw: {raw_reply_content[:500]}")
                except Exception as e:
                    logger.exception(f"Unexpected error parsing AI response: {e}. Raw: {raw_reply_content[:500]}")
            else:
                 logger.error("Missing AI response content ('response_content') in context object.")
            # --- END ADDED --- #

            # Validate required Twilio inputs
            if not twilio_creds or 'twilio_account_sid' not in twilio_creds or 'twilio_auth_token' not in twilio_creds:
                 logger.error(f"Missing or incomplete Twilio credentials in context for {conversation_id}. Cannot send reply.")
                 batch_item_failures.append({"itemIdentifier": message_id})
                 continue
            if not sender_num:
                 logger.error(f"Missing Twilio sender number (company_whatsapp_number) in config for {conversation_id}. Cannot send reply.")
                 batch_item_failures.append({"itemIdentifier": message_id})
                 continue
            # --- MODIFIED: Check final_reply_body instead of raw_reply_content --- #
            if not final_reply_body:
                 logger.error(f"Missing or failed to extract final reply body from AI response for {conversation_id}. Cannot send reply.")
                 batch_item_failures.append({"itemIdentifier": message_id})
                 continue
            # recipient_num (primary_channel) was validated earlier

            # Call the Twilio service function
            # --- MODIFIED: Use final_reply_body --- #
            twilio_status, twilio_result_payload = twilio_service.send_whatsapp_reply(
                twilio_creds=twilio_creds,
                recipient_number=recipient_num,
                sender_number=sender_num,
                message_body=final_reply_body
            )

            # Handle Twilio processing results
            if twilio_status == twilio_service.TWILIO_SUCCESS:
                context_object['twilio_response'] = twilio_result_payload
                logger.info(f"Successfully sent Twilio reply for {conversation_id}.") # Simplified log
            elif twilio_status == twilio_service.TWILIO_TRANSIENT_ERROR:
                error_msg = twilio_result_payload.get("error_message", "Unknown transient Twilio error") if twilio_result_payload else "Unknown transient Twilio error"
                logger.warning(f"Twilio send failed with transient error for {conversation_id}: {error_msg}. Raising exception for retry.")
                raise Exception(f"Transient Twilio Error: {error_msg}")
            else:
                error_msg = twilio_result_payload.get("error_message", "Unknown non-transient Twilio error") if twilio_result_payload else f"Unknown non-transient Twilio error ({twilio_status})"
                logger.error(f"Twilio send failed with non-transient error ({twilio_status}) for {conversation_id}: {error_msg}. Failing message {message_id}.")
                batch_item_failures.append({"itemIdentifier": message_id})
                continue

            # --- Step 11: Construct Final Message Maps --- #
            logger.info(f"Constructing message maps for final DB update for {conversation_id}.")
            try:
                # Generate distinct timestamps
                user_msg_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
                # Brief pause or ensure clock moves if needed, though unlikely necessary
                # time.sleep(0.001) # Generally not needed
                assistant_msg_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()

                # User Message Map
                user_message_map = {
                    "message_id": context_object['staging_table_merged_data']['first_message_sid'],
                    "timestamp": user_msg_ts, # Use first timestamp
                    "role": "user",
                    "content": context_object['staging_table_merged_data']['combined_body']
                }

                # Assistant Message Map
                assistant_message_map = {
                    "message_id": context_object['twilio_response']['message_sid'],
                    "timestamp": assistant_msg_ts, # Use second timestamp
                    "role": "assistant",
                    "content": context_object['twilio_response']['body'], # Body sent by Twilio
                    "prompt_tokens": context_object['open_ai_response']['prompt_tokens'],
                    "completion_tokens": context_object['open_ai_response']['completion_tokens'],
                    "total_tokens": context_object['open_ai_response']['total_tokens']
                }
                logger.debug(f"User message map (ts: {user_msg_ts}): {user_message_map}")
                logger.debug(f"Assistant message map (ts: {assistant_msg_ts}): {assistant_message_map}")
            except KeyError as ke:
                logger.error(f"Missing key when constructing message maps for {conversation_id}: {ke}. Failing message {message_id}.")
                batch_item_failures.append({"itemIdentifier": message_id})
                continue
            except Exception as map_ex:
                 logger.exception(f"Unexpected error constructing message maps for {conversation_id}: {map_ex}. Failing message {message_id}.")
                 batch_item_failures.append({"itemIdentifier": message_id})
                 continue

            # --- Pre-Update Check: Hand Off to Human --- #
            needs_handoff = db_data.get('hand_off_to_human', False)
            handoff_reason = db_data.get('hand_off_to_human_reason') # Get current reason (might be None)
            task_complete_status = db_data.get('task_complete', 0) # Get current status (default 0)
            
            if needs_handoff:
                logger.warning(f"HANDOFF DETECTED for conversation {conversation_id} before final DB update. Proceeding with update, but further logic needed.")
                pass

            # --- Calculate Processing Time --- #
            processing_end_time = time.time()
            processing_duration_ms = int((processing_end_time - processing_start_time) * 1000)
            logger.debug(f"Total processing time for record {message_id}: {processing_duration_ms} ms")

            # --- Step 12: Final Atomic Update --- #
            logger.info(f"Performing final atomic update for conversation {conversation_id}.")
            update_status, update_error_msg = dynamodb_service.update_conversation_after_reply(
                primary_channel_pk=primary_channel,
                conversation_id_sk=conversation_id,
                user_message_map=user_message_map,
                assistant_message_map=assistant_message_map,
                new_status="reply_sent", # TODO: Make status dynamic later if needed
                # Pass the new optional fields
                processing_time_ms=processing_duration_ms,
                task_complete=task_complete_status, # Pass current value
                hand_off_to_human=needs_handoff, # Pass current value
                hand_off_to_human_reason=handoff_reason # Pass current value
            )

            if update_status == dynamodb_service.DB_SUCCESS:
                logger.info(f"Final DB update successful for {conversation_id}.")
                # Proceed to cleanup...
            elif update_status == dynamodb_service.DB_LOCK_LOST:
                logger.critical(f"CRITICAL: Final update failed for {conversation_id} because lock was lost after message was sent! Manual investigation needed. Error: {update_error_msg}")
                continue 
            else: # DB_ERROR
                logger.critical(f"CRITICAL: Final DB update failed for {conversation_id} after message was sent! Error: {update_error_msg}. Manual investigation needed.")
                continue

            # --- Step 13: Cleanup Staging & Trigger-Lock --- #
            # Only runs if Step 12 was successful
            logger.info(f"Performing cleanup for conversation {conversation_id}.")
            # Prepare keys for staging table cleanup
            keys_to_delete_staging = []
            if staged_items: # Ensure staged_items exists
                 keys_to_delete_staging = [
                     {'conversation_id': item.get('conversation_id'), 'message_sid': item.get('message_sid')} 
                     for item in staged_items 
                     if item.get('conversation_id') and item.get('message_sid') # Ensure keys are present
                 ]

            if not keys_to_delete_staging:
                 logger.warning(f"No valid keys extracted from staged_items for cleanup of conversation {conversation_id}")
                 # Decide if this is an error or just informational

            # Call cleanup functions
            cleanup_staging_success = dynamodb_service.cleanup_staging_table(keys_to_delete_staging)
            cleanup_lock_success = dynamodb_service.cleanup_trigger_lock(conversation_id)

            # Log warnings on failure, but don't fail the overall process
            if not cleanup_staging_success:
                 logger.warning(f"Cleanup of staging table failed for {conversation_id}. TTL will handle.")
            if not cleanup_lock_success:
                 logger.warning(f"Cleanup of trigger lock failed for {conversation_id}. TTL will handle.")
            
            if cleanup_staging_success and cleanup_lock_success:
                 logger.info(f"Cleanup successful for {conversation_id}.")

            # Step 14 (Release Lock) is implicitly handled by Step 12 setting status != processing_reply
            # Step 15 (Lambda Message Success) is handled by reaching end of try block

            # Remove placeholder
            # logger.info(f"Placeholder: Successfully processed message {message_id}")

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
                # Check if an error occurred that requires releasing the lock
                # An error occurred if the message_id is in batch_item_failures AND it wasn't just a heartbeat error
                processing_failed = any(f['itemIdentifier'] == message_id for f in batch_item_failures)
                
                if processing_failed and not heartbeat_exception:
                    logger.warning(f"Attempting to release lock for {primary_channel}/{conversation_id} (setting status to retry) due to processing exception...")
                    release_success = dynamodb_service.release_lock_for_retry(primary_channel, conversation_id)
                    if not release_success:
                         logger.error(f"FAILED TO RELEASE LOCK for {primary_channel}/{conversation_id} in finally block!")
                    # No need to change final_status variable here as we are setting directly to 'retry'
                elif processing_failed and heartbeat_exception:
                     logger.warning(f"Processing failed for {message_id}, but likely due to heartbeat failure. Attempting to release lock (setting status to retry)...")
                     release_success = dynamodb_service.release_lock_for_retry(primary_channel, conversation_id)
                     if not release_success:
                          logger.error(f"FAILED TO RELEASE LOCK for {primary_channel}/{conversation_id} in finally block after heartbeat failure!")
                else:
                     # Lock was acquired, but no processing failure reported (successful run)
                     # Lock was already released implicitly by Step 12 setting status to 'reply_sent'
                     logger.debug(f"Processing successful for {message_id}, lock implicitly released by final update.")
            elif lock_status is not None:
                # Lock was never acquired (e.g., LOCK_EXISTS or DB_ERROR on acquire attempt)
                logger.debug(f"Lock was not acquired for {message_id} (status: {lock_status}), no release needed in finally.")
            # else: lock_status is None if parsing failed very early

    # Return response indicating which items failed, if any
    response = {"batchItemFailures": batch_item_failures}
    logger.info(f"Lambda execution finished. Returning response: {response}")
    return response 