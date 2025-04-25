#!/usr/bin/env python3
import boto3
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
import sys
from botocore.exceptions import ClientError

# --- Configuration ---
PROJECT_PREFIX = "ai-multi-comms" # Define prefix
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "eu-north-1")

# Specific file and table mappings for this script
RECORDS_TO_UPLOAD = [
    {
        "file": "company_data_dev_test_record.json",
        "table": f"{PROJECT_PREFIX}-company-data-dev"
    },
    {
        "file": "company_data_prod_test_record.json",
        "table": f"{PROJECT_PREFIX}-company-data-prod"
    }
]

# --- Helper Function to handle non-standard JSON types for DynamoDB ---
def replace_floats_with_decimal(obj):
    """Recursively replace float values with Decimal for DynamoDB compatibility."""
    if isinstance(obj, list):
        return [replace_floats_with_decimal(i) for i in obj]
    elif isinstance(obj, dict):
        return {k: replace_floats_with_decimal(v) for k, v in obj.items()}
    elif isinstance(obj, float):
        return Decimal(obj)
    else:
        return obj

def validate_record(record, filename):
    """Performs basic validation on the input record."""
    print(f"Validating record from {filename}...")
    required_keys = [
        "company_id", "project_id", "company_name", "project_name",
        "allowed_channels", "rate_limits",
        "project_status", "channel_config", "ai_config"
    ]
    missing_keys = [key for key in required_keys if key not in record]
    if missing_keys:
        raise ValueError(f"Missing required keys in {filename}: {', '.join(missing_keys)}")

    if not isinstance(record.get("allowed_channels"), list) or not record["allowed_channels"]:
        raise ValueError(f"'allowed_channels' must be a non-empty list in {filename}.")
    print(f"Basic validation passed for {filename}.")

def upload_single_record(file_path, table_name):
    """Reads a JSON file, validates, prepares, and uploads to a specific DynamoDB table."""
    filename = os.path.basename(file_path)
    try:
        with open(file_path, 'r') as f:
            print(f"Reading record from: {filename}")
            record_data = json.load(f)
    except FileNotFoundError:
        print(f"Error: Input file not found at {file_path}")
        return False # Indicate failure
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON from {filename}: {e}")
        return False # Indicate failure

    try:
        validate_record(record_data, filename)

        # Set timestamps (Overwrite existing ones if present in file)
        now_iso = datetime.now(timezone.utc).isoformat()
        record_data["created_at"] = now_iso
        record_data["updated_at"] = now_iso
        print(f"Timestamps set/updated to: {now_iso}")

        # Prepare for DynamoDB (handle floats -> Decimal)
        dynamodb_item = replace_floats_with_decimal(record_data)

        # Initialize DynamoDB client
        dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
        table = dynamodb.Table(table_name)

        print(f"Attempting to put item into DynamoDB table: {table_name}...")
        # Use ConditionExpression to prevent overwriting existing items
        response = table.put_item(
            Item=dynamodb_item,
            ConditionExpression="attribute_not_exists(company_id) AND attribute_not_exists(project_id)"
        )
        print(f"Successfully created record in {table_name}.")
        # print(f"Response: {response}") # Optional: print full response
        return True # Indicate success

    except ValueError as e:
        print(f"Validation Error for {filename}: {e}")
        return False # Indicate failure
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            print(f"Warning: Record with company_id={record_data.get('company_id')} / project_id={record_data.get('project_id')} already exists in {table_name}. Skipping.")
            return True # Consider already existing as a non-failure state for this script
        else:
            # Re-raise other Boto3 client errors
            print(f"Error interacting with DynamoDB table {table_name}: {e}")
            print("Please check AWS credentials/permissions and table existence.")
            return False # Indicate failure
    except Exception as e:
        # Catch other potential errors
        print(f"An unexpected error occurred while processing {filename} for table {table_name}: {e}")
        return False # Indicate failure

# Main execution block
if __name__ == "__main__":
    print(f"--- Starting Test Company Record Creation --- ")
    print(f"Using Region: {AWS_REGION}")
    script_dir = os.path.dirname(os.path.abspath(__file__))
    success_count = 0
    failure_count = 0

    for record_info in RECORDS_TO_UPLOAD:
        file_to_upload = record_info["file"]
        table_to_upload_to = record_info["table"]
        full_file_path = os.path.join(script_dir, file_to_upload)

        print(f"\nProcessing: {file_to_upload} -> {table_to_upload_to}")
        if upload_single_record(full_file_path, table_to_upload_to):
            success_count += 1
        else:
            failure_count += 1

    print("\n--- Script Finished ---")
    print(f"Summary: {success_count} record(s) processed successfully (or already existed), {failure_count} failed.")
    # Exit with non-zero code if any failures occurred
    if failure_count > 0:
        sys.exit(1)
    else:
        sys.exit(0) 