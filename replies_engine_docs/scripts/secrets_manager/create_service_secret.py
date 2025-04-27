#!/usr/bin/env python3
import boto3
import argparse
import json
import os
import sys
from botocore.exceptions import ClientError
from typing import Tuple, Optional

# --- Configuration --- #
DEFAULT_REGION = "eu-north-1"
SECRET_PREFIX = "ai-multi-comms"
PLACEHOLDER_SECRET_VALUE = '{"key": "PLEASE_UPDATE", "value": "PLEASE_UPDATE"}'

# --- Helper Functions --- #
def format_name_part(name: str) -> str:
    """Converts a name part to lowercase and replaces spaces with hyphens."""
    return name.strip().lower().replace(' ', '-')

def get_validated_input(prompt: str, valid_options: list = None, allow_empty: bool = False) -> str:
    """Gets user input, validates against options if provided, handles empty input."""
    while True:
        value = input(prompt).strip()
        if not value and not allow_empty:
            print("Input cannot be empty. Please try again.")
            continue
        if value and valid_options and value.lower() not in [opt.lower() for opt in valid_options]:
            print(f"Invalid input. Please choose from: {', '.join(valid_options)}")
            continue
        return value

def get_secret_value_input() -> str:
     """Gets the secret value JSON string from the user with validation."""
     while True:
        print("\nEnter the secret value as a valid JSON string.")
        print("(e.g., '{\"api_key\": \"your_key\", \"user\": \"name\"}')")
        secret_value_str = input("Secret Value JSON: ").strip()
        if not secret_value_str:
             print("Secret value cannot be empty. Please try again.")
             continue
        try:
            json.loads(secret_value_str) # Validate JSON format
            return secret_value_str
        except json.JSONDecodeError as e:
            print(f"Error: Invalid JSON provided: {e}. Please try again.")

# --- Function to get details and construct name --- #
def prompt_for_secret_details() -> Tuple[str, str, str, str, str, str, str]:
    """Prompts user for details and constructs the secret name and description parts."""
    # 1. Get Channel
    channel = get_validated_input("1. Enter the channel (whatsapp, sms, email): ", valid_options=['whatsapp', 'sms', 'email'])

    # 2. Get Company Name
    company_name_raw = get_validated_input("2. Enter the Company Name (e.g., Adaptix Innovation): ")
    company_name_fmt = format_name_part(company_name_raw)

    # 3. Get Project Name
    project_name_raw = get_validated_input("3. Enter the Project Name (e.g., Integration Tests): ")
    project_name_fmt = format_name_part(project_name_raw)

    # 4. Get Service
    service_raw = get_validated_input("4. Enter the service provider (e.g., twilio, sendgrid): ")
    service_fmt = format_name_part(service_raw) # Format service name too

    # 5. Get Environment
    environment = get_validated_input("5. Enter the environment (dev, prod): ", valid_options=['dev', 'prod'])

    # Construct the Secret Name
    # Pattern: ai-multi-comms/{channel}-credentials/{company_name}/{project_name}/{service}-{environment}
    secret_name = f"{SECRET_PREFIX}/{channel.lower()}-credentials/{company_name_fmt}/{project_name_fmt}/{service_fmt}-{environment.lower()}"

    return secret_name, channel, company_name_raw, project_name_raw, service_raw, environment, region

# --- AWS Interaction Functions --- #

def create_or_update_secret(secret_name, secret_value_str, region, description):
    """Creates a new secret or updates an existing one in AWS Secrets Manager."""
    print(f"\nAttempting to create/update secret: '{secret_name}' in region: {region}")
    client = boto3.client('secretsmanager', region_name=region)

    # Safety check for JSON string input
    try:
        json.loads(secret_value_str)
    except json.JSONDecodeError as e:
        print(f"Internal Error: Provided secret value is not valid JSON: {e}")
        return False

    try:
        client.describe_secret(SecretId=secret_name)
        print(f"Secret '{secret_name}' already exists. Updating value...")
        update_args = {
            'SecretId': secret_name,
            'SecretString': secret_value_str
        }
        if description: # Only update description if provided
            update_args['Description'] = description
        client.update_secret(**update_args)
        print(f"Successfully updated secret: '{secret_name}'")
        return True
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            print(f"Secret '{secret_name}' not found. Creating new secret...")
            try:
                create_args = {
                    'Name': secret_name,
                    'SecretString': secret_value_str
                }
                if description: # Only add description if provided
                    create_args['Description'] = description
                client.create_secret(**create_args)
                print(f"Successfully created secret: '{secret_name}'")
                return True
            except ClientError as create_e:
                print(f"Error creating secret '{secret_name}': {create_e}")
                return False
            except Exception as create_ex:
                print(f"Unexpected error creating secret '{secret_name}': {create_ex}")
                return False
        else:
            print(f"AWS ClientError interacting with secret '{secret_name}': {e}")
            return False
    except Exception as e:
        print(f"An unexpected error occurred for secret '{secret_name}': {e}")
        return False

def delete_secret_aws(secret_name: str, region: str) -> bool:
    """Deletes a secret from AWS Secrets Manager."""
    print(f"\nAttempting to delete secret: '{secret_name}' in region: {region}")
    client = boto3.client('secretsmanager', region_name=region)
    try:
        # Use standard delete (allows recovery by default)
        # To force immediate deletion use: ForceDeleteWithoutRecovery=True
        response = client.delete_secret(SecretId=secret_name)
        print(f"Successfully initiated deletion for secret: '{secret_name}'")
        print(f"(Note: Secret scheduled for deletion on: {response.get('DeletionDate')})")
        return True
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            print(f"Error: Secret '{secret_name}' not found. Cannot delete.")
            return False
        else:
            print(f"AWS ClientError deleting secret '{secret_name}': {e}")
            return False
    except Exception as e:
        print(f"An unexpected error occurred while deleting secret '{secret_name}': {e}")
        return False

# --- Workflow Functions --- #

def create_update_secret_flow():
    """Handles the interactive flow for creating or updating a secret."""
    (secret_name, channel, company_name_raw,
     project_name_raw, service_raw, environment, region) = prompt_for_secret_details()

    print(f"\nConstructed Secret Name: {secret_name}")

    # --- Generate Description ---
    # Old format: f"Credentials for {company_name_raw} / {project_name_raw} {channel.lower()} via {service_raw.lower()} ({environment.lower()})"
    description = f"{service_raw.capitalize()} {channel.capitalize()} Credentials for {company_name_fmt}/{project_name_fmt} ({environment.capitalize()})"
    print(f"Generated Description: {description}")

    # Determine Secret Value
    use_placeholder = get_validated_input("\nUse placeholder value? (yes/no): ", valid_options=['yes', 'no'])

    if use_placeholder.lower() == 'yes':
        secret_value_str = PLACEHOLDER_SECRET_VALUE
        print(f"Using placeholder value: {secret_value_str}")
    else:
        secret_value_str = get_secret_value_input() # Get and validate user JSON

    # Determine Region (might have been determined in prompt_for_secret_details)
    # region = os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION)
    print(f"Using AWS Region: {region}")


    # Call the AWS function
    print("\n--- Proceeding with AWS Create/Update Operation --- ")
    return create_or_update_secret(secret_name, secret_value_str, region, description)


def delete_secret_flow():
    """Handles the interactive flow for deleting a secret."""
    print("\nEnter details to identify the secret to delete:")
    (secret_name, _, _, _, _, _, region) = prompt_for_secret_details()

    print(f"\nSecret Name identified for deletion: {secret_name}")
    print(f"Region: {region}")

    confirmation = get_validated_input(f"\n*** WARNING ***\nAre you sure you want to delete '{secret_name}'? This cannot be undone immediately. (yes/no): ", valid_options=['yes', 'no'])

    if confirmation.lower() == 'yes':
        # Call the AWS function
        print("\n--- Proceeding with AWS Delete Operation --- ")
        return delete_secret_aws(secret_name, region)
    else:
        print("Deletion cancelled by user.")
        return True # Treat cancellation as non-failure for exit code

# --- Main Execution Block --- #
if __name__ == "__main__":
    print("--- Interactive Secret Creator/Updater/Deleter ---")
    print("This script helps construct the secret name based on standard conventions.")
    print("\nTip: To ensure your secret value has the correct keys (e.g., 'twilio_account_sid', 'twilio_auth_token'),")
    print("     you might want to check an existing secret first using:")
    print("     aws secretsmanager get-secret-value --secret-id <example_secret_name> --region <region>\n")

    action = get_validated_input("Choose action: [1] Create/Update Secret [2] Delete Secret : ", valid_options=['1', '2'])

    success = False
    if action == '1':
        success = create_update_secret_flow()
    elif action == '2':
        success = delete_secret_flow()
    # else: # Should not happen due to validation
    #     print("Invalid action selected.")

    if success:
        print("\n--- Script Finished Successfully (or cancelled) ---")
        sys.exit(0)
    else:
        print("\n--- Script Finished With Errors ---")
        sys.exit(1)

# --- Removed Argparse ---
# if __name__ == "__main__":
#     parser = argparse.ArgumentParser(description="Create or update a secret in AWS Secrets Manager.")
#     parser.add_argument("--secret-name", required=True, help="The name or ARN of the secret.")
#     parser.add_argument("--secret-value", required=True, help="The secret value as a JSON string (e.g., '{\"key\": \"value\"}').")
#     parser.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION), help=f"AWS Region (default: {DEFAULT_REGION} or AWS_DEFAULT_REGION env var).")
#     parser.add_argument("--description", default="", help="Optional description for the secret.")
#
#     args = parser.parse_args()
#
#     print("--- Starting Secret Creation/Update --- ")
#     if create_or_update_secret(args.secret_name, args.secret_value, args.region, args.description):
#         print("--- Script Finished Successfully ---")
#         sys.exit(0)
#     else:
#         print("--- Script Finished With Errors ---")
#         sys.exit(1) 