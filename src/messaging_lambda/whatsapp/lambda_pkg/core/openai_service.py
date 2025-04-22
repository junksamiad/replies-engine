# core/openai_service.py - Messaging Lambda (WhatsApp)

import openai
import logging
import os
import time
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO').upper())

# --- Status Codes --- #
AI_SUCCESS = "SUCCESS"
AI_TRANSIENT_ERROR = "TRANSIENT_ERROR"       # Timeout, Rate Limit, Server Error
AI_NON_TRANSIENT_ERROR = "NON_TRANSIENT_ERROR" # Auth, Not Found, Bad Request, Failed Run
AI_INVALID_INPUT = "INVALID_INPUT"           # Missing required args to this function
# --- End Status Codes --- #

# Define polling parameters - REMOVED ENV VARS
# POLLING_INTERVAL_SECONDS = int(os.environ.get('OPENAI_POLLING_INTERVAL', '1')) # Default 1 second - REMOVED
# RUN_TIMEOUT_SECONDS = int(os.environ.get('OPENAI_RUN_TIMEOUT', '540')) # Default 9 minutes - REMOVED

def process_reply_with_ai(
    thread_id: str,
    assistant_id: str,
    user_message_content: str,
    api_key: str
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """
    Adds a user message to an existing OpenAI thread, runs the specified assistant,
    and retrieves the assistant's latest reply.

    Args:
        thread_id: The existing OpenAI thread ID.
        assistant_id: The OpenAI assistant ID configured for handling replies.
        user_message_content: The combined text from the user.
        api_key: The OpenAI API key.

    Returns:
        A tuple containing:
        - status_code (str): One of the AI_* status constants.
        - result (Optional[Dict]): On SUCCESS, contains response details
          ('response_content', 'prompt_tokens', 'completion_tokens', 'total_tokens').
          On failure, contains {'error_message': 'details...'} or None.
    """
    logger.info(f"Starting OpenAI processing for thread_id: {thread_id}, assistant_id: {assistant_id}")

    if not all([thread_id, assistant_id, user_message_content, api_key]):
        error_msg = "Missing required arguments for OpenAI processing."
        logger.error(error_msg)
        return AI_INVALID_INPUT, {"error_message": error_msg}

    # Initialize OpenAI Client
    try:
        client = openai.OpenAI(api_key=api_key)
        logger.debug("OpenAI client initialized.")
    except Exception as e:
        error_msg = f"Failed to initialize OpenAI client: {e}"
        logger.exception(error_msg)
        # Treat init failure as non-transient
        return AI_NON_TRANSIENT_ERROR, {"error_message": error_msg}

    run_id = None # Initialize run_id
    run = None # Initialize run object
    try:
        # 1. Add the new user message to the existing thread
        logger.info(f"Adding user message to thread {thread_id}")
        logger.debug(f"User message content: {user_message_content[:200]}...")
        message = client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=user_message_content
        )
        logger.info(f"Successfully added message {message.id} to thread {thread_id}")

        # 2. Run the assistant on the thread
        logger.info(f"Running assistant {assistant_id} on thread {thread_id}")
        run = client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=assistant_id
        )
        run_id = run.id
        logger.info(f"Created run {run_id} with status {run.status}")

        # 3. Poll for the run status
        # Use hardcoded values for timeout and interval
        polling_timeout_seconds = 540 # Hardcoded 9 minutes
        polling_interval_seconds = 1  # Hardcoded 1 second
        logger.info(f"Polling run {run_id} status (timeout: {polling_timeout_seconds}s)... ")
        start_time = time.time()
        while True:
            elapsed_time = time.time() - start_time
            # Use the hardcoded timeout value
            if elapsed_time > polling_timeout_seconds:
                error_msg = f"Polling timeout exceeded for run {run_id} after {polling_timeout_seconds} seconds."
                logger.error(error_msg)
                try: client.beta.threads.runs.cancel(thread_id=thread_id, run_id=run_id)
                except Exception: logger.warning(f"Failed to cancel timed-out run {run_id}")
                return AI_TRANSIENT_ERROR, {"error_message": error_msg} # Timeout is transient
            
            run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run_id)
            logger.debug(f"Run {run_id} status: {run.status}")

            if run.status == 'completed':
                logger.info(f"Run {run_id} completed successfully.")
                break
            elif run.status in ['failed', 'cancelled', 'expired']:
                error_msg = f"Run {run_id} ended with terminal status: {run.status}. Details: {run.last_error}"
                logger.error(error_msg)
                return AI_NON_TRANSIENT_ERROR, {"error_message": error_msg}
            elif run.status == 'requires_action':
                error_msg = f"Run {run_id} requires action, but function calling is not implemented."
                logger.error(error_msg)
                return AI_NON_TRANSIENT_ERROR, {"error_message": error_msg}
            
            # Use the hardcoded interval value
            time.sleep(polling_interval_seconds)

        # 4. Retrieve the latest messages from the thread
        logger.info(f"Retrieving messages from thread {thread_id} after run {run_id}.")
        messages_response = client.beta.threads.messages.list(thread_id=thread_id, order='desc')
        thread_messages = messages_response.data

        if not thread_messages:
             error_msg = f"No messages found in thread {thread_id} after run {run_id} completed."
             logger.error(error_msg)
             return AI_NON_TRANSIENT_ERROR, {"error_message": error_msg}
        logger.info(f"Retrieved {len(thread_messages)} messages from thread {thread_id}.")

        # 5. Extract the relevant assistant response message
        assistant_message_content = None
        for m in thread_messages:
            if m.run_id == run_id and m.role == 'assistant':
                 if m.content and len(m.content) > 0 and hasattr(m.content[0], 'text'):
                     assistant_message_content = m.content[0].text.value
                     logger.info(f"Found assistant message {m.id} from run {run_id}.")
                     break
                 else:
                      logger.warning(f"Assistant message {m.id} from run {run_id} found but has no text content.")
                      break

        if assistant_message_content is None:
            error_msg = f"No assistant message with text content found associated with run {run_id} in thread {thread_id}."
            logger.error(error_msg + f" Messages dump: {thread_messages}")
            return AI_NON_TRANSIENT_ERROR, {"error_message": error_msg}
        logger.debug(f"Extracted assistant content: {assistant_message_content[:200]}...")

        # 6. Extract token usage from the final run object (must exist if completed)
        prompt_tokens = run.usage.prompt_tokens if run and run.usage else 0
        completion_tokens = run.usage.completion_tokens if run and run.usage else 0
        total_tokens = run.usage.total_tokens if run and run.usage else 0

        logger.info(f"OpenAI processing successful for thread {thread_id}. Tokens: P{prompt_tokens}/C{completion_tokens}/T{total_tokens}")
        result_payload = {
            "response_content": assistant_message_content,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens
        }
        return AI_SUCCESS, result_payload

    # --- Exception Handling --- #
    except openai.RateLimitError as e:
        error_msg = f"OpenAI Rate Limit Error processing thread {thread_id}, run {run_id}: {e}"
        logger.error(error_msg)
        return AI_TRANSIENT_ERROR, {"error_message": error_msg}
    except (openai.APIConnectionError, openai.Timeout, openai.InternalServerError) as e:
        error_msg = f"OpenAI Transient API Error processing thread {thread_id}, run {run_id}: {e}"
        logger.error(error_msg)
        return AI_TRANSIENT_ERROR, {"error_message": error_msg}
    except (openai.AuthenticationError, openai.PermissionDeniedError, openai.NotFoundError, openai.BadRequestError, openai.UnprocessableEntityError) as e:
        error_msg = f"OpenAI Non-Transient API Error processing thread {thread_id}, run {run_id}: {e}"
        logger.error(error_msg)
        return AI_NON_TRANSIENT_ERROR, {"error_message": error_msg}
    except openai.APIError as e:
        # Catch any other OpenAI specific errors - treat as non-transient by default
        error_msg = f"Unhandled OpenAI API Error processing thread {thread_id}, run {run_id}: {e}"
        logger.error(error_msg)
        return AI_NON_TRANSIENT_ERROR, {"error_message": error_msg}
    except Exception as e:
        error_msg = f"Unexpected error during OpenAI processing for thread {thread_id}, run {run_id}: {e}"
        logger.exception(error_msg)
        return AI_NON_TRANSIENT_ERROR, {"error_message": error_msg} 