# webhook_handler/core/validation.py

import os
import boto3
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key
from decimal import Decimal # Import Decimal for DynamoDB Numbers

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
            'pk_name': 'gsi_company_whatsapp_number',
            'sk_name': 'gsi_recipient_tel'
        }
    elif channel_type == 'sms':
        return {
            'index_name': 'company-sms-number-recipient-tel-index',
            'pk_name': 'gsi_company_sms_number',
            'sk_name': 'gsi_recipient_tel'
        }
    elif channel_type == 'email':
        return {
            'index_name': 'company-email-recipient-email-index',
            'pk_name': 'gsi_company_email',
            'sk_name': 'gsi_recipient_email'
        }
    else:
        return None

def check_conversation_exists(context_object):
    """Validates if an *active* conversation record exists and retrieves the latest one."""
    channel_type = context_object.get('channel_type')
    gsi_config = _get_gsi_config(channel_type)

    if not gsi_config:
        print(f"ERROR: No GSI configuration found for channel type: {channel_type}")
        return {'valid': False, 'error_code': 'CONFIGURATION_ERROR', 'message': "Internal configuration error for channel"}

    # --- Prepare Key Values --- 
    pk_value = None
    sk_value = None

    if channel_type in ['whatsapp', 'sms']:
        pk_value = _strip_prefix(context_object.get('from'))
        sk_value = _strip_prefix(context_object.get('to'))
    elif channel_type == 'email':
         pk_value = context_object.get('from_address')
         sk_value = context_object.get('to_address')

    if not pk_value or not sk_value:
        print(f"ERROR: Missing key values for GSI query. PK: {pk_value}, SK: {sk_value}")
        return {'valid': False, 'error_code': 'MISSING_REQUIRED_FIELD', 'message': "Missing key identifiers for GSI query"}

    print(f"Querying GSI {gsi_config['index_name']} with PK={pk_value}, SK={sk_value} and filtering for active tasks")

    # --- Perform DynamoDB Query --- 
    try:
        response = table.query(
            IndexName=gsi_config['index_name'],
            KeyConditionExpression=
                Key(gsi_config['pk_name']).eq(pk_value) & 
                Key(gsi_config['sk_name']).eq(sk_value),
            FilterExpression="task_complete = :task_not_complete_val",
            ExpressionAttributeValues={
                ":task_not_complete_val": Decimal(0)
            }
        )

        items = response.get('Items', [])
        item_count = len(items)

        if item_count == 0:
            # No *active* record found
            print(f"No *active* conversation record found for PK={pk_value}, SK={sk_value}")
            return {'valid': False, 'error_code': 'CONVERSATION_NOT_FOUND', 'message': "Active conversation record not found"}
        
        conversation_record = None
        if item_count == 1:
            # Exactly one active record found
            conversation_record = items[0]
            print(f"Single active conversation record found: {conversation_record.get('conversation_id')}")
        else: # item_count > 1
            # Multiple active records found - sort by creation_timestamp descending
            print(f"WARNING: Found {item_count} active records matching query. Selecting the latest created.")
            items.sort(key=lambda x: x.get('created_at', ''), reverse=True)
            conversation_record = items[0]
            print(f"Selected latest active conversation record: {conversation_record.get('conversation_id')}")

        # Update the context object with retrieved data
        context_object.update(conversation_record)
        return {'valid': True, 'data': context_object}

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

# Renamed placeholder and added validation logic
def validate_conversation_rules(context_object):
    """Performs business rule validation on the retrieved conversation record."""
    print("Validating conversation rules...")

    # 1. Check Project Status
    project_status = context_object.get('project_status')
    if project_status != 'active':
        print(f"Validation Failed: Project status is '{project_status}', not 'active'.")
        return {'valid': False, 'error_code': 'PROJECT_INACTIVE', 'message': f"Project is not active (status: {project_status})"}

    # 2. Check Allowed Channel
    channel_type = context_object.get('channel_type')
    allowed_channels = context_object.get('allowed_channels', []) # Default to empty list
    if channel_type not in allowed_channels:
        print(f"Validation Failed: Channel '{channel_type}' not in allowed_channels {allowed_channels}.")
        return {'valid': False, 'error_code': 'CHANNEL_NOT_ALLOWED', 'message': f"Channel '{channel_type}' is not allowed for this conversation"}

    # 3. Check Conversation Lock Status
    conversation_status = context_object.get('conversation_status')
    if conversation_status == 'processing_reply':
        print(f"Validation Failed: Conversation status is 'processing_reply' (locked).")
        return {'valid': False, 'error_code': 'CONVERSATION_LOCKED', 'message': "Conversation is currently processing a previous reply"}

    # If all checks pass
    print("Conversation rules validation successful.")
    return {'valid': True, 'data': context_object} # Pass context through 