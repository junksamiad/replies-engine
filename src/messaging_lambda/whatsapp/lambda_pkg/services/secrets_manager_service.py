# services/secrets_manager_service.py - Messaging Lambda (WhatsApp)

import boto3
import json
import logging
import os
from botocore.exceptions import ClientError
from typing import Dict, Any, Optional, Tuple # Added Tuple

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO').upper())

# --- Status Codes --- #
SECRET_SUCCESS = "SUCCESS"
SECRET_NOT_FOUND = "NOT_FOUND"
SECRET_TRANSIENT_ERROR = "TRANSIENT_ERROR"
SECRET_PERMANENT_ERROR = "PERMANENT_ERROR" # Includes access denied, decryption, bad request
SECRET_INVALID_INPUT = "INVALID_INPUT"
SECRET_INIT_ERROR = "INITIALIZATION_ERROR"
# --- End Status Codes --- #

secrets_manager = None

def _get_secrets_manager_client():
    """Initializes and returns the Secrets Manager client."""
    global secrets_manager
    if secrets_manager is None:
        region = os.environ.get('AWS_REGION', 'eu-north-1')
        logger.info(f"Initializing Secrets Manager client in region: {region}")
        try:
            secrets_manager = boto3.client('secretsmanager', region_name=region)
        except Exception as e:
             logger.critical(f"Failed to initialize secrets manager client: {e}")
             raise RuntimeError(f"Secrets Manager client init failed: {e}") # Raise custom error
    return secrets_manager

def get_secret(secret_id: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """
    Retrieves a secret from AWS Secrets Manager and parses it as JSON.

    Args:
        secret_id (str): The name or ARN of the secret.

    Returns:
        A tuple containing:
        - status_code (str): One of the SECRET_* status constants.
        - secret_data (Optional[Dict[str, Any]]): The parsed secret dictionary on success,
                                                 or None on failure.
    """
    if not secret_id:
        logger.error("get_secret called with empty secret_id.")
        return SECRET_INVALID_INPUT, None

    try:
        client = _get_secrets_manager_client()
    except RuntimeError as init_err:
         logger.error(f"Cannot get secret, client initialization failed: {init_err}")
         return SECRET_INIT_ERROR, None

    logger.info(f"Attempting to retrieve secret: {secret_id}")
    try:
        get_secret_value_response = client.get_secret_value(SecretId=secret_id)
        logger.debug(f"Successfully called GetSecretValue for: {secret_id}")

        if 'SecretString' in get_secret_value_response:
            secret_string = get_secret_value_response['SecretString']
            try:
                secret_data = json.loads(secret_string)
                if not isinstance(secret_data, dict):
                    logger.error(f"Parsed secret for {secret_id} is not a dictionary (type: {type(secret_data)}). Returning error.")
                    return SECRET_PERMANENT_ERROR, None # Treat non-dict JSON as permanent error
                logger.info(f"Successfully retrieved and parsed JSON secret for {secret_id}")
                return SECRET_SUCCESS, secret_data
            except json.JSONDecodeError as json_err:
                logger.error(f"Failed to parse JSON secret string for {secret_id}: {json_err}")
                return SECRET_PERMANENT_ERROR, None # Parsing error is permanent
        elif 'SecretBinary' in get_secret_value_response:
            logger.warning(f"Secret {secret_id} contains SecretBinary, not SecretString. Cannot parse as JSON.")
            return SECRET_PERMANENT_ERROR, None # Treat binary as permanent error for this use case
        else:
            logger.error(f"Unknown response format from GetSecretValue for {secret_id}. No SecretString or SecretBinary.")
            return SECRET_PERMANENT_ERROR, None

    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code')
        logger.error(f"Secrets Manager ClientError retrieving secret {secret_id}: {error_code} - {e}")

        if error_code == 'ResourceNotFoundException':
            return SECRET_NOT_FOUND, None
        elif error_code == 'InternalServiceError':
            # InternalServiceError is often transient
            return SECRET_TRANSIENT_ERROR, None
        elif error_code in ['DecryptionFailure', 'AccessDeniedException', 'InvalidParameterException', 'InvalidRequestException']:
            # These are generally permanent issues (permissions, config)
            return SECRET_PERMANENT_ERROR, None
        else:
            # Treat other specific AWS errors as potentially permanent unless known otherwise
            logger.exception(f"Unhandled Secrets Manager ClientError retrieving secret {secret_id}")
            return SECRET_PERMANENT_ERROR, None

    except Exception as e:
        logger.exception(f"An unexpected error occurred retrieving secret {secret_id}")
        return SECRET_PERMANENT_ERROR, None # Treat unexpected errors as permanent 