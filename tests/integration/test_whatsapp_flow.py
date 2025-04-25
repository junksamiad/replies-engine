import json
import time
import os
from typing import List
from unittest.mock import patch

import boto3
import pytest
import requests
from twilio.request_validator import RequestValidator

# Remove top-level constants, read from os.environ within tests/fixtures
# from .conftest import (
#     API_BASE_URL,
#     STAGE_TABLE_NAME,
#     LOCK_TABLE_NAME,
#     CONVERSATIONS_TABLE_NAME, # Not directly used in this file
#     WHATSAPP_QUEUE_URL,
#     WHATSAPP_QUEUE_DELAY_SEC,
# )


@pytest.mark.integration
@pytest.mark.usefixtures("clear_stage_and_lock_tables", "seed_conversation_item")
class TestWhatsAppReplyFlow:
    """Integration tests that exercise the deployed *dev* replies‑engine stack.
    These tests require AWS credentials with access to the dev account and the
    resources enumerated in ``tests/integration/conftest.py``. Set environment
    variables to override defaults when running against a different stack.
    """

    _SQS_POLL_TIMEOUT = 60  # seconds – max time to wait for trigger message

    # ---------------------------------------------------------------------
    # Helper / polling utilities
    # ---------------------------------------------------------------------

    @staticmethod
    def _poll_until(predicate, timeout: int = 30, interval: float = 2.0, *args, **kwargs):
        """Poll *predicate* every *interval* seconds until it returns a truthy value
        or *timeout* seconds have elapsed.  The predicate is called with *args/
        **kwargs* each iteration.  The final truthy value is returned; raises
        ``AssertionError`` on timeout so that the test fails."""
        start = time.time()
        while time.time() - start <= timeout:
            result = predicate(*args, **kwargs)
            if result:
                return result
            time.sleep(interval)
        raise AssertionError("Timeout waiting for condition to become true")

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_stage_lambda_persists_fragment_and_lock(
        self,
        aws_clients,
        test_phone_numbers,
        conversation_id,
        twilio_auth_token,
    ):
        """Send a fake Twilio webhook with a VALID signature and ensure the staging lambda
        persists a fragment row and acquires the trigger lock."""
        # Read resource names from os.environ
        stage_table_name = os.environ['STAGE_TABLE_NAME']
        lock_table_name = os.environ['LOCK_TABLE_NAME']
        api_base_url = os.environ['REPLIES_API_URL']
        request_url = f"{api_base_url}/whatsapp"

        stage_tbl = aws_clients["dynamodb"].Table(stage_table_name)
        lock_tbl = aws_clients["dynamodb"].Table(lock_table_name)

        # ----------------- 1. Fire webhook ----------------------------
        msg_sid = f"SM{int(time.time() * 1000)}"
        payload_dict = {
            "To": f"whatsapp:{test_phone_numbers['company']}",
            "From": f"whatsapp:{test_phone_numbers['user']}",
            "Body": "Hello integration test",
            "MessageSid": msg_sid,
            "AccountSid": "ACXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
        }

        # Compute the valid signature
        validator = RequestValidator(twilio_auth_token)
        signature = validator.compute_signature(request_url, payload_dict)
        print(f"\nComputed Signature: {signature}")

        # Send request with REAL signature
        resp = requests.post(
            request_url,
            data=payload_dict,
            headers={"X-Twilio-Signature": signature},
            timeout=10
        )

        # API Gateway replies 200 OK instantly (staging lambda returns body)
        assert resp.status_code == 200

        # ----------------- 2. Verify DynamoDB fragment row -------------
        def _has_stage_fragment():
            resp_q = stage_tbl.query(
                KeyConditionExpression=boto3.dynamodb.conditions.Key("conversation_id").eq(conversation_id)
            )
            return resp_q["Items"]

        items: List[dict] = self._poll_until(_has_stage_fragment, timeout=20)
        assert any(it["message_sid"] == msg_sid for it in items)

        # ----------------- 3. Verify lock row --------------------------
        lock_item = lock_tbl.get_item(Key={"conversation_id": conversation_id}).get("Item")
        assert lock_item is not None, "Trigger lock not acquired"

    def test_trigger_message_appears_on_sqs(
        self,
        aws_clients,
        test_phone_numbers,
        conversation_id,
    ):
        """End‑to‑end part 2 – after the staging lambda executes it should
        enqueue a trigger message (conversation_id & primary_channel) on the
        WhatsApp queue.  We poll the queue until the message is visible."""
        # Read resource names from os.environ
        whatsapp_queue_url = os.environ['WHATSAPP_QUEUE_URL']
        # Use get for delay as it might not be overridden by configure hook
        whatsapp_queue_delay_sec = int(os.environ.get("WHATSAPP_QUEUE_DELAY_SEC", 10))

        sqs = aws_clients["sqs"]

        # Allow time for queue delay to elapse plus processing jitter
        min_visible_time = whatsapp_queue_delay_sec + 2
        time.sleep(min_visible_time)

        def _receive_once():
            msgs = sqs.receive_message(
                QueueUrl=whatsapp_queue_url,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=1,
            ).get("Messages", [])
            # Filter messages matching our conversation id
            return [m for m in msgs if conversation_id in m["Body"]]

        matching_msgs = self._poll_until(_receive_once, timeout=self._SQS_POLL_TIMEOUT)
        assert matching_msgs, "No trigger message found for conversation id"

        # Clean up the message(s) so that dev queue doesn't grow indefinitely
        for m in matching_msgs:
            sqs.delete_message(
                QueueUrl=whatsapp_queue_url,
                ReceiptHandle=m["ReceiptHandle"],
            ) 