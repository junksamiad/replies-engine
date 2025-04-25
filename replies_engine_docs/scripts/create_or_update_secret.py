#!/usr/bin/env python3
import boto3
import argparse
import json
import os
import sys
from botocore.exceptions import ClientError

# --- Configuration --- #
DEFAULT_REGION = "eu-north-1"

def create_or_update_secret(secret_name, secret_value_str, region, description):
    """Creates a new secret or updates an existing one in AWS Secrets Manager."""
    print(f"Attempting to create/update secret: '{secret_name}' in region: {region}")
    client = boto3.client('secretsmanager', region_name=region)

    # Validate JSON string input
    try:
        # Ensure it's valid JSON, but store it as a string
        json.loads(secret_value_str)
    except json.JSONDecodeError as e:
        print(f"Error: Provided secret value is not valid JSON: {e}")
        return False

    try:
        # Check if secret exists
        client.describe_secret(SecretId=secret_name)
        # If it exists, update it
        print(f"Secret '{secret_name}' already exists. Updating value...")
        update_args = {
            'SecretId': secret_name,
            'SecretString': secret_value_str
        }
        if description:
            update_args['Description'] = description

        client.update_secret(**update_args)
        print(f"Successfully updated secret: '{secret_name}'")
        return True

    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            # If it doesn't exist, create it
            print(f"Secret '{secret_name}' not found. Creating new secret...")
            try:
                create_args = {
                    'Name': secret_name,
                    'SecretString': secret_value_str
                }
                if description:
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
            # Handle other errors during describe_secret or update_secret
            print(f"AWS ClientError interacting with secret '{secret_name}': {e}")
            return False
    except Exception as e:
        print(f"An unexpected error occurred for secret '{secret_name}': {e}")
        return False

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create or update a secret in AWS Secrets Manager.")
    parser.add_argument("--secret-name", required=True, help="The name or ARN of the secret.")
    parser.add_argument("--secret-value", required=True, help="The secret value as a JSON string (e.g., '{\"key\": \"value\"}').")
    parser.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION), help=f"AWS Region (default: {DEFAULT_REGION} or AWS_DEFAULT_REGION env var).")
    parser.add_argument("--description", default="", help="Optional description for the secret.")

    args = parser.parse_args()

    print("--- Starting Secret Creation/Update --- ")
    if create_or_update_secret(args.secret_name, args.secret_value, args.region, args.description):
        print("--- Script Finished Successfully ---")
        sys.exit(0)
    else:
        print("--- Script Finished With Errors ---")
        sys.exit(1) 