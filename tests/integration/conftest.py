import os
import random
import string
import time
import uuid
import json
from typing import Dict, Any

import boto3
import pytest
import requests
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Helpers / Constants
# ---------------------------------------------------------------------------

REGION = os.environ.get("AWS_REGION", "eu-north-1")
ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID")  # Optional but helpful

# Remove top-level constants derived from os.environ
# They will be read directly from os.environ within fixtures/tests
# after pytest_configure updates the environment.
# API_BASE_URL = os.environ.get("REPLIES_API_URL", "...")
# STAGE_TABLE_NAME = os.environ.get("STAGE_TABLE_NAME", "...")
# LOCK_TABLE_NAME = os.environ.get("LOCK_TABLE_NAME", "...")
# CONVERSATIONS_TABLE_NAME = os.environ.get("CONVERSATIONS_TABLE_NAME", "...")
# WHATSAPP_QUEUE_URL = os.environ.get("WHATSAPP_QUEUE_URL", "...")
# WHATSAPP_QUEUE_DELAY_SEC = int(os.environ.get("WHATSAPP_QUEUE_DELAY_SEC", "10"))

# --- Hook to override env vars for integration tests ---
def pytest_configure():
    """This hook runs after pytest-env sets dummies but before tests are collected/run."""
    # Define the *real* values for the dev environment
    _REAL_DEV_ENV = {
        "REPLIES_API_URL": "https://716zgxg7ma.execute-api.eu-north-1.amazonaws.com/dev",
        "STAGE_TABLE_NAME": "ai-multi-comms-replies-conversations-stage-dev",
        "LOCK_TABLE_NAME": "ai-multi-comms-replies-conversations-trigger-lock-dev",
        "CONVERSATIONS_TABLE_NAME": "ai-multi-comms-conversations-dev",
        "WHATSAPP_QUEUE_URL": "https://sqs.eu-north-1.amazonaws.com/337909745089/ai-multi-comms-replies-whatsapp-queue-dev",
        "AWS_REGION": "eu-north-1", # Also set region if code relies on it
        # Add any other variables needed by integration tests that might conflict with dummies
    }
    print("\nConfiguring integration test environment variables...")
    os.environ.update(_REAL_DEV_ENV)

# ---------------------------------------------------------------------------
# Pytest Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def aws_clients():
    """Return cached boto3 clients/resources used across tests."""
    session = boto3.Session(region_name=REGION)

    return {
        "dynamodb": session.resource("dynamodb"),
        "sqs": session.client("sqs"),
        "secretsmanager": session.client("secretsmanager")
    }

# --- Fixture to fetch the real auth token ---
@pytest.fixture(scope="session")
def twilio_auth_token(aws_clients):
    """Fetches the Twilio auth token from Secrets Manager once per session."""
    # Use the actual secret name for the dev environment
    secret_name = "ai-multi-comms/whatsapp-credentials/cucumber-recruitment/clarify-cv/twilio-dev"
    print(f"\nFetching Twilio auth token from secret: {secret_name}")
    try:
        sm_client = aws_clients["secretsmanager"]
        response = sm_client.get_secret_value(SecretId=secret_name)
        secret_string = response['SecretString']
        # Assuming the secret stores a JSON string with a key like 'twilio_auth_token'
        # Adjust the key if your secret structure is different
        auth_token = json.loads(secret_string).get('twilio_auth_token')
        if not auth_token:
            pytest.fail(f"Key 'twilio_auth_token' not found in secret {secret_name}", pytrace=False)
        print("Successfully fetched Twilio auth token.")
        return auth_token
    except ClientError as e:
        pytest.fail(f"Failed to fetch Twilio auth token secret {secret_name}: {e}", pytrace=False)
    except (json.JSONDecodeError, KeyError) as e:
        pytest.fail(f"Failed to parse Twilio auth token from secret {secret_name}: {e}", pytrace=False)
    except Exception as e:
        pytest.fail(f"Unexpected error fetching Twilio auth token secret {secret_name}: {e}", pytrace=False)

@pytest.fixture
def test_phone_numbers() -> Dict[str, str]:
    """Generate a deterministic pair of test phone numbers."""
    # Use random portion to avoid collisions across concurrent runs
    suffix = "".join(random.choices(string.digits, k=4))
    user_number = f"+4477009{suffix}"
    company_number = "+447700900000"  # Static Twilio business number used in dev stack
    return {
        "user": user_number,
        "company": company_number,
    }


@pytest.fixture
def conversation_id(test_phone_numbers) -> str:
    # Keep derivation logic in sync with staging lambda (sorted numbers)
    nums_sorted = sorted([test_phone_numbers["user"], test_phone_numbers["company"]])
    # Keep plus signs because staging lambda includes them in conversation_id derivation
    return f"conv_{'_'.join(nums_sorted)}"


@pytest.fixture
def seed_conversation_item(aws_clients, test_phone_numbers, conversation_id):
    """Insert a minimal conversation item into the shared conversations table so that the
    messaging lambda can hydrate context later in the flow."""
    conversations_table_name = os.environ['CONVERSATIONS_TABLE_NAME']
    table = aws_clients["dynamodb"].Table(conversations_table_name)

    # Extract numbers without prefix for GSI
    user_num_no_prefix = test_phone_numbers["user"].split(':')[-1]
    company_num_no_prefix = test_phone_numbers["company"].split(':')[-1]

    # Define a credential ID that matches the IAM policy pattern AND EXISTS in Secrets Manager
    matching_credential_id = "ai-multi-comms/whatsapp-credentials/cucumber-recruitment/clarify-cv/twilio-dev" # Use actual existing secret name for dev

    item = {
        # Main Keys
        "primary_channel": test_phone_numbers["user"],
        "conversation_id": conversation_id,
        # GSI Keys (Match GSI definition)
        "gsi_company_whatsapp_number": company_num_no_prefix, # GSI PK
        "gsi_recipient_tel": user_num_no_prefix,             # GSI SK
        # Other required fields
        "project_status": "active",
        "allowed_channels": ["whatsapp"],
        "thread_id": str(uuid.uuid4()),
        "conversation_status": "active",
        "ai_config": {
            "assistant_id_replies": "asst_replies_test",
            "api_key_reference": "ai-multi-comms/openai-api-key/whatsapp-dev-test"
        },
        "channel_config": {
            "whatsapp_credentials_id": matching_credential_id, # Use the actual secret name
            "company_whatsapp_number": company_num_no_prefix,
        },
    }

    try:
        print(f"\nSEEDING conversation item with cred ID: {matching_credential_id} into {conversations_table_name}")
        table.put_item(Item=item)
        print("SEEDING successful")
    except ClientError as e:
        print(f"\nERROR SEEDING conversation item: {e}") # Add print
        pytest.fail(f"Failed to seed conversation item: {e}", pytrace=False)
    except Exception as e:
        print(f"\nUNEXPECTED ERROR SEEDING conversation item: {e}") # Add print
        pytest.fail(f"Unexpected error seeding conversation item: {e}", pytrace=False)

    yield item

    # Clean up – best‑effort delete
    try:
        print(f"\nCLEANING UP seeded item for {conversation_id}")
        table = aws_clients["dynamodb"].Table(os.environ['CONVERSATIONS_TABLE_NAME'])
        table.delete_item(Key={
            "primary_channel": item["primary_channel"],
            "conversation_id": item["conversation_id"],
        })
        print("CLEANUP successful")
    except Exception as e:
        print(f"\nERROR CLEANING UP seeded item: {e}")
        pass # Allow cleanup to fail silently


@pytest.fixture
def clear_stage_and_lock_tables(aws_clients, conversation_id):
    """Ensure staging & lock tables are clear of this conversation before test."""
    # Read table names directly from os.environ inside the fixture
    stage_tbl_name = os.environ['STAGE_TABLE_NAME']
    lock_tbl_name = os.environ['LOCK_TABLE_NAME']
    stage_tbl = aws_clients["dynamodb"].Table(stage_tbl_name)
    lock_tbl = aws_clients["dynamodb"].Table(lock_tbl_name)
    # Delete potential residual items
    try:
        # Scan is acceptable for tiny dev tables; otherwise use query on PK
        resp = stage_tbl.query(KeyConditionExpression=boto3.dynamodb.conditions.Key("conversation_id").eq(conversation_id))
        with stage_tbl.batch_writer() as bw:
            for it in resp.get("Items", []):
                bw.delete_item(Key={"conversation_id": it["conversation_id"], "message_sid": it["message_sid"]})
    except Exception:
        pass

    try:
        lock_tbl.delete_item(Key={"conversation_id": conversation_id})
    except Exception:
        pass
    yield

    # Post‑test cleanup identical
    try:
        resp = stage_tbl.query(KeyConditionExpression=boto3.dynamodb.conditions.Key("conversation_id").eq(conversation_id))
        with stage_tbl.batch_writer() as bw:
            for it in resp.get("Items", []):
                bw.delete_item(Key={"conversation_id": it["conversation_id"], "message_sid": it["message_sid"]})
    except Exception:
        pass
    try:
        lock_tbl.delete_item(Key={"conversation_id": conversation_id})
    except Exception:
        pass 