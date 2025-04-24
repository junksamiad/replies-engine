import os
import random
import string
import time
import uuid
from typing import Dict, Any

import boto3
import pytest
import requests

# ---------------------------------------------------------------------------
# Helpers / Constants
# ---------------------------------------------------------------------------

REGION = os.environ.get("AWS_REGION", "eu-north-1")
ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID")  # Optional but helpful for table ARNs if needed

# Replies‑engine (dev) default resource names – override with env vars if desired
API_BASE_URL = os.environ.get("REPLIES_API_URL", "https://dc1fn3ses0.execute-api.eu-north-1.amazonaws.com")
STAGE_TABLE_NAME = os.environ.get("STAGE_TABLE_NAME", "ai-multi-comms-replies-conversations-stage-dev")
LOCK_TABLE_NAME = os.environ.get("LOCK_TABLE_NAME", "ai-multi-comms-replies-conversations-trigger-lock-dev")
CONVERSATIONS_TABLE_NAME = os.environ.get("CONVERSATIONS_TABLE_NAME", "ai-multi-comms-conversations-dev")
WHATSAPP_QUEUE_URL = os.environ.get("WHATSAPP_QUEUE_URL", "https://sqs.eu-north-1.amazonaws.com/337909745089/ai-multi-comms-replies-whatsapp-queue-dev")
WHATSAPP_QUEUE_DELAY_SEC = int(os.environ.get("WHATSAPP_QUEUE_DELAY_SEC", "10"))

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
    }


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
    table = aws_clients["dynamodb"].Table(CONVERSATIONS_TABLE_NAME)

    # Token values below can reference existing dev secrets / assistant ids already deployed
    item = {
        "primary_channel": test_phone_numbers["user"],
        "conversation_id": conversation_id,
        "thread_id": str(uuid.uuid4()),
        "conversation_status": "active",
        "ai_config": {
            "assistant_id_replies": "asst_replies_test",  # dummy but required
            "api_key_reference": "ai-multi-comms/openai-api-key/whatsapp-dev-test"
        },
        "channel_config": {
            "whatsapp_credentials_id": "ai-multi-comms/whatsapp-credentials/company/dev-test",
            "company_whatsapp_number": test_phone_numbers["company"],
        },
        # Other attributes stripped for brevity
    }

    table.put_item(Item=item)

    yield item

    # Clean up – best‑effort delete
    try:
        table.delete_item(Key={
            "primary_channel": item["primary_channel"],
            "conversation_id": item["conversation_id"],
        })
    except Exception:
        pass


@pytest.fixture
def clear_stage_and_lock_tables(aws_clients, conversation_id):
    """Ensure staging & lock tables are clear of this conversation before test."""
    stage_tbl = aws_clients["dynamodb"].Table(STAGE_TABLE_NAME)
    lock_tbl = aws_clients["dynamodb"].Table(LOCK_TABLE_NAME)
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