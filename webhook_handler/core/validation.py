# webhook_handler/core/validation.py

import os
import boto3
from botocore.exceptions import ClientError

# Initialize DynamoDB client (consider moving to a shared services module later)
dynamodb = boto3.resource('dynamodb')
TABLE_NAME = os.environ.get('CONVERSATIONS_TABLE_NAME', 'ai-multi-comms-conversations-dev') # Default to dev table
table = dynamodb.Table(TABLE_NAME)

def _strip_prefix(identifier):
    """Removes prefixes like 'whatsapp:', 'sms:' etc."""
    if identifier and ':' in identifier:
        return identifier.split(':', 1)[1]
    return identifier

def _get_gsi_config(channel_type):
    """Returns the GSI name and key names for the given channel."""
    if channel_type == 'whatsapp':
        return {
            'index_name': 'company-whatsapp-number-recipient-tel-index',
            'pk_name': 'company_whatsapp_number',
            'sk_name': 'recipient_tel'
        }
    elif channel_type == 'sms':
        return {
            'index_name': 'company-sms-number-recipient-tel-index',
            'pk_name': 'company_sms_number',
            'sk_name': 'recipient_tel'
        }
    elif channel_type == 'email':
        return {
            'index_name': 'company-email-recipient-email-index',
            'pk_name': 'company_email',
            'sk_name': 'recipient_email'
        }
    else:
        return None

def check_conversation_exists(context_object):
    """Validates if a conversation record exists based on channel and identifiers."""
    channel_type = context_object.get('channel_type')
    gsi_config = _get_gsi_config(channel_type)

    if not gsi_config:
        print(f"ERROR: No GSI configuration found for channel type: {channel_type}")
        return {'valid': False, 'error_code': 'CONFIGURATION_ERROR', 'message': "Internal configuration error for channel"}

    # --- Prepare Key Values --- 
    pk_value = None
    sk_value = None

    if channel_type in ['whatsapp', 'sms']:
        # Map 'from' (sender) to the company number (PK), 'to' (recipient) to recipient tel (SK)
        # Strip prefixes like 'whatsapp:'
        pk_value = _strip_prefix(context_object.get('from'))
        sk_value = _strip_prefix(context_object.get('to'))
    elif channel_type == 'email':
         # Map 'from_address' (sender) to company email (PK), 'to_address' to recipient email (SK)
         # Assuming no prefix stripping needed for email addresses
         pk_value = context_object.get('from_address')
         sk_value = context_object.get('to_address')
    # Add other channel mappings here...

    if not pk_value or not sk_value:
        print(f"ERROR: Missing key values for GSI query. PK: {pk_value}, SK: {sk_value}")
        return {'valid': False, 'error_code': 'MISSING_REQUIRED_FIELD', 'message': "Missing key identifiers for GSI query"}

    print(f"Querying GSI {gsi_config['index_name']} with PK={pk_value}, SK={sk_value}")

    # --- Perform DynamoDB Query --- 
    try:
        response = table.query(
            IndexName=gsi_config['index_name'],
            KeyConditionExpression=
                boto3.dynamodb.conditions.Key(gsi_config['pk_name']).eq(pk_value) & 
                boto3.dynamodb.conditions.Key(gsi_config['sk_name']).eq(sk_value)
            # Add Limit=1? If we expect only one match.
        )

        if response.get('Count', 0) > 0:
            # Record found!
            conversation_record = response['Items'][0]
            print(f"Conversation record found: {conversation_record.get('conversation_id')}")
            # Update the context object with retrieved data
            context_object.update(conversation_record)
            return {'valid': True, 'data': context_object}
        else:
            # Record not found
            print(f"No conversation record found for PK={pk_value}, SK={sk_value}")
            return {'valid': False, 'error_code': 'CONVERSATION_NOT_FOUND', 'message': "Conversation record not found"}

    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code')
        print(f"DynamoDB ClientError: {error_code} - {e}")
        # Check for common transient errors
        transient_errors = ['ProvisionedThroughputExceededException', 
                            'InternalServerError', 
                            'ServiceUnavailable', # Less common for DynamoDB query
                            'ThrottlingException']
        if error_code in transient_errors:
            return {'valid': False, 'error_code': 'DB_TRANSIENT_ERROR', 'message': "Database temporarily unavailable"}
        else:
            return {'valid': False, 'error_code': 'DB_QUERY_ERROR', 'message': "Database query error"}
    except Exception as e:
        # Catch-all for other unexpected errors
        print(f"ERROR: Unexpected error during DynamoDB query: {e}")
        return {'valid': False, 'error_code': 'INTERNAL_ERROR', 'message': "Internal server error during DB query"}

# Placeholder for further validation steps
def validate_further(context_object):
    print("Performing further validation steps...")
    # Check project_status, allowed_channels, conversation_status == 'processing_reply' etc.
    # based on fields added to context_object by check_conversation_exists
    is_valid = True # Replace with actual checks
    error_code = None
    message = None
    if not is_valid:
        error_code = 'VALIDATION_FAILED' # Or more specific code
        message = "Further validation failed" 
    return {'valid': is_valid, 'data': context_object, 'error_code': error_code, 'message': message} 