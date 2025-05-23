import boto3
import json
import logging
import os
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO').upper())

secrets_manager = None

def _get_secrets_manager_client():
    """Initializes and returns the Secrets Manager client."""
    global secrets_manager
    if secrets_manager is None:
        logger.info("Initializing Secrets Manager client.")
        secrets_manager = boto3.client('secretsmanager', region_name=os.environ.get('AWS_REGION', 'eu-north-1'))
    return secrets_manager

def get_twilio_auth_token(secret_id):
    """
    Retrieves a secret from AWS Secrets Manager and extracts the Twilio Auth Token.

    Args:
        secret_id (str): The name or ARN of the secret containing the Twilio credentials.

    Returns:
        str: The Twilio Auth Token, or None if retrieval fails or the token is not found.
    """
    client = _get_secrets_manager_client()
    logger.info(f"Attempting to retrieve secret: {secret_id}")

    try:
        get_secret_value_response = client.get_secret_value(SecretId=secret_id)
        logger.debug("Successfully retrieved secret value.")

        if 'SecretString' in get_secret_value_response:
            secret_string = get_secret_value_response['SecretString']
            secret_data = json.loads(secret_string)
            auth_token = secret_data.get('twilio_auth_token')

            if auth_token:
                logger.info(f"Successfully extracted twilio_auth_token from secret {secret_id}")
                return auth_token
            else:
                logger.error(f"'twilio_auth_token' key not found within secret string for {secret_id}")
                return None
        else:
            # Handle binary secret if necessary, though unlikely for auth tokens
            logger.warning(f"Secret {secret_id} does not contain a SecretString.")
            return None

    except ClientError as e: # Catch generic ClientError
        error_code = e.response.get('Error', {}).get('Code') # Get code from response
        logger.error(f"Secrets Manager ClientError retrieving secret {secret_id}: {error_code} - {e}")

        # Check specific codes
        if error_code == 'ResourceNotFoundException':
            return None
        elif error_code == 'InvalidParameterException':
            logger.error(f"Invalid parameter for secret {secret_id}: {e}") # Log specific message
            return None
        elif error_code == 'InvalidRequestException':
            logger.error(f"Invalid request for secret {secret_id}: {e}")
            return None
        elif error_code == 'DecryptionFailure':
            logger.error(f"Secrets Manager decryption failure for {secret_id}: {e}")
            return None
        elif error_code == 'InternalServiceError':
            logger.error(f"Secrets Manager internal service error for {secret_id}: {e}")
            # Consider this potentially transient? Might need retry logic depending on requirements.
            return None
        else:
             # Log unhandled AWS error codes
             logger.error(f"Unhandled Secrets Manager ClientError code '{error_code}' for secret {secret_id}: {e}")
             return None
    except json.JSONDecodeError:
        logger.error(f"Failed to parse JSON secret string for {secret_id}")
        return None
    except Exception as e:
        logger.exception(f"An unexpected error occurred retrieving secret {secret_id}")
        return None 