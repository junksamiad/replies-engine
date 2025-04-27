import os
import random
import string
import time
import uuid
import json
from typing import Dict, Any, Generator, Tuple
from datetime import datetime, timezone

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
def conversation_id() -> str: # Removed dependency on test_phone_numbers
    # Generate a unique conversation ID for each test run
    unique_part = str(uuid.uuid4())
    return f"test-int-{unique_part}"

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

# --- Fixture to create/delete the specific test conversation record --- #
@pytest.fixture(scope="function")
def conversation_record_fixture(aws_clients) -> Generator[Tuple[str, str, str, str], None, None]:
    """
    Creates the specific conversation record in DynamoDB for testing
    based on user-provided structure and fixed IDs, and cleans it up afterwards.
    """
    conversations_tbl_name = os.environ['CONVERSATIONS_TABLE_NAME']
    conversations_tbl = aws_clients["dynamodb"].Table(conversations_tbl_name)

    # --- Use FIXED identifiers from user data --- #
    raw_user_phone = "+447835065013"
    prefixed_user_phone = "whatsapp:+447835065013"
    conversation_id = "ci-aaa-000#pi-aaa-000#218c1681-382c-4995-8741-94c74ed88800#447450659796"
    company_phone = "+447450659796"

    print(f"\nSetting up conversation record fixture:")
    print(f"  Primary Channel (User Phone): {raw_user_phone} (Stored)") # Log raw phone
    print(f"  Conversation ID: {conversation_id}")

    # --- Base Record Structure (Translated from DynamoDB JSON) ---
    # Values taken from the user-provided structure
    ai_config = {
        "api_key_reference": "ai-multi-comms/openai-api-key/whatsapp-dev",
        "assistant_id_3": "",
        "assistant_id_4": "",
        "assistant_id_5": "",
        "assistant_id_replies": "asst_yI2xMm8ixFbXd6OE99Y377kZ",
        "assistant_id_template_sender": "asst_FAYJva8KwGksePejLpMhEE2A"
    }
    channel_config = {
        "company_whatsapp_number": company_phone,
        "whatsapp_credentials_id": "ai-multi-comms/whatsapp-credentials/adaptix-innovation/tests/twilio-dev"
    }
    thread_id = "thread_ZOEo3jwJDaCGwn965yICjn7z"

    timestamp = datetime.now(timezone.utc).isoformat()
    test_record = {
        "primary_channel": raw_user_phone, # STORE WITHOUT PREFIX
        "conversation_id": conversation_id,
        "ai_config": ai_config,
        "allowed_channels": ["whatsapp", "sms", "email"],
        "auto_queue_initial_message": False,
        "auto_queue_reply_message": False,
        "channel_config": channel_config,
        "channel_method": "whatsapp",
        "comms_consent": True,
        "company_id": "ci-aaa-000",
        "company_name": "Adaptix Innovation",
        "company_rep": {
            "company_rep_1": "Adaptix Innovation",
        },
        "conversation_status": "initial_message_sent", # Start state for test
        "created_at": timestamp,
        "function_call": False,
        "gsi_company_whatsapp_number": company_phone,
        "gsi_recipient_email": "accounts@adaptixinnovation.co.uk",
        "gsi_recipient_tel": raw_user_phone, # Ensure GSI uses raw phone
        "hand_off_to_human": False,
        "initial_request_timestamp": "2025-04-26T05:38:47Z",
        "messages": [], # Start with empty history for the test
        "processor_version": "dev-1.0.0",
        "project_data": {
            "adaptix_id": "use_case_1",
            "projects": []
        },
        "project_id": "pi-aaa-000",
        "project_name": "Tests",
        "project_status": "active",
        "recipient_email": "accounts@adaptixinnovation.co.uk",
        "recipient_first_name": "User",
        "recipient_last_name": "One", # Reverted to match original data
        "recipient_tel": raw_user_phone, # Ensure this uses raw phone
        "request_id": conversation_id.split("#")[-2], # Extract request_id part
        "router_version": "router-dev-1.0.1",
        "task_complete": 0,
        "thread_id": thread_id,
        "updated_at": timestamp,
        "ttl": int(time.time()) + 3600 # Safety net TTL
    }

    try:
        print(f"  Creating record in {conversations_tbl_name}...")
        conversations_tbl.put_item(Item=test_record)
        print("  Record created successfully.")
        # Yield the PREFIXED phone for payload, raw phone for key, conv_id, company_phone
        yield (prefixed_user_phone, raw_user_phone, conversation_id, company_phone)
    finally:
        # Teardown: Delete the created record using the RAW phone as the key
        print(f"\nCleaning up conversation record fixture for {conversation_id}...")
        try:
            conversations_tbl.delete_item(
                Key={
                    "primary_channel": raw_user_phone,
                    "conversation_id": conversation_id
                }
            )
            print("  Record deleted successfully.")
        except ClientError as e:
            print(f"  WARN: Failed to delete conversation record during cleanup: {e}")
        except Exception as e:
            print(f"  WARN: Unexpected error during conversation record cleanup: {e}")

        # --- ADDED: Also cleanup staging and lock tables --- #
        print(f"  Cleaning up staging/lock tables for {conversation_id}...")
        try:
            stage_tbl_name = os.environ['STAGE_TABLE_NAME']
            lock_tbl_name = os.environ['LOCK_TABLE_NAME']
            stage_tbl = aws_clients["dynamodb"].Table(stage_tbl_name)
            lock_tbl = aws_clients["dynamodb"].Table(lock_tbl_name)

            # Cleanup Staging Table
            try:
                # Query is safer than scan if table might grow, but scan is simpler for tests
                resp = stage_tbl.query(KeyConditionExpression=boto3.dynamodb.conditions.Key("conversation_id").eq(conversation_id))
                items = resp.get("Items", [])
                if items:
                    with stage_tbl.batch_writer() as bw:
                        for item in items:
                             # Ensure both keys exist before attempting delete
                             if "conversation_id" in item and "message_sid" in item:
                                 bw.delete_item(Key={"conversation_id": item["conversation_id"], "message_sid": item["message_sid"]})
                    print(f"    Deleted {len(items)} items from staging table.")
                else:
                    print("    No items found in staging table to delete.")
            except Exception as stage_ex:
                print(f"    WARN: Error cleaning up staging table: {stage_ex}")

            # Cleanup Lock Table
            try:
                lock_tbl.delete_item(Key={"conversation_id": conversation_id})
                print("    Attempted deletion from lock table (no error doesn't guarantee existence).")
            except Exception as lock_ex:
                 print(f"    WARN: Error cleaning up lock table: {lock_ex}")

        except Exception as cleanup_ex:
             print(f"  WARN: Unexpected error during staging/lock table cleanup: {cleanup_ex}") 