import boto3
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
import sys

# --- Configuration ---
# Get target environment (dev or prod), default to dev
DEPLOY_ENV = os.environ.get("DEPLOY_ENV", "dev").lower()
if DEPLOY_ENV not in ['dev', 'prod']:
    print(f"Error: Invalid DEPLOY_ENV specified: {DEPLOY_ENV}. Must be 'dev' or 'prod'.")
    sys.exit(1)

# Construct table name based on environment
PROJECT_PREFIX = "ai-multi-comms" # Define prefix
TABLE_NAME = f"{PROJECT_PREFIX}-company-data-{DEPLOY_ENV}"

# Use environment variable or default for region
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "eu-north-1")

# --- Helper Function to handle non-standard JSON types for DynamoDB ---
def replace_floats_with_decimal(obj):
    """Recursively replace float values with Decimal for DynamoDB compatibility."""
    if isinstance(obj, list):
        return [replace_floats_with_decimal(i) for i in obj]
    elif isinstance(obj, dict):
        return {k: replace_floats_with_decimal(v) for k, v in obj.items()}
    elif isinstance(obj, float):
        # Use context to handle potential precision issues if needed
        # return Decimal(str(obj)) # Safest way
        return Decimal(obj) # Often sufficient
    else:
        return obj

def validate_record(record):
    """Performs basic validation on the input record."""
    required_keys = [
        "company_id", "project_id", "company_name", "project_name",
        "allowed_channels", "rate_limits",
        "project_status", "channel_config", "ai_config" # Added ai_config based on sample
    ]
    missing_keys = [key for key in required_keys if key not in record]
    if missing_keys:
        raise ValueError(f"Missing required keys in input file: {', '.join(missing_keys)}")

    if not isinstance(record.get("allowed_channels"), list) or not record["allowed_channels"]:
        raise ValueError("'allowed_channels' must be a non-empty list.")

    # Add more specific validations as needed (e.g., project_status values)
    print("Basic validation passed.")

def create_dynamodb_record(record_path):
    """Reads a JSON file, validates, prepares, and uploads to DynamoDB."""
    try:
        with open(record_path, 'r') as f:
            print(f"Reading record from: {record_path}")
            record_data = json.load(f)
    except FileNotFoundError:
        print(f"Error: Input file not found at {record_path}")
        return
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON from {record_path}: {e}")
        return

    try:
        validate_record(record_data)

        # Set timestamps
        now_iso = datetime.now(timezone.utc).isoformat()
        record_data["created_at"] = now_iso
        record_data["updated_at"] = now_iso
        print(f"Timestamps set: {now_iso}")

        # Prepare for DynamoDB (handle floats -> Decimal)
        dynamodb_item = replace_floats_with_decimal(record_data)

        # Initialize DynamoDB client
        dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
        table = dynamodb.Table(TABLE_NAME)

        print(f"Attempting to put item into DynamoDB table: {TABLE_NAME}...")
        # Use ConditionExpression to prevent overwriting existing items
        response = table.put_item(
            Item=dynamodb_item,
            ConditionExpression="attribute_not_exists(company_id) AND attribute_not_exists(project_id)"
        )
        print("Successfully created record in DynamoDB.")
        print(f"Response: {response}")

    except ValueError as e:
        print(f"Validation Error: {e}")
    except boto3.exceptions.botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            print("Error: Record with this company_id/project_id combination already exists.")
        else:
            # Re-raise other Boto3 client errors
            print(f"Error interacting with DynamoDB: {e}")
            print("Please check AWS credentials/permissions are correct.")
    except Exception as e:
        # Catch other potential errors
        print(f"An unexpected error occurred: {e}")

# Main execution block
if __name__ == "__main__":
    # Get the directory where the script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))
    # Construct the path to the JSON file in the same directory
    input_json_file = os.path.join(script_dir, "recruitment_company_data_record_example_dev.json")

    print(f"--- Starting Company Record Creation for environment: {DEPLOY_ENV.upper()} ---")
    print(f"Target Table: {TABLE_NAME}")
    print(f"Using Region: {AWS_REGION}")
    print(f"Using Input File: {input_json_file}")
    create_dynamodb_record(input_json_file)
    print("--- Script Finished ---") 