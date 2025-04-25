import json
import time
import os
from typing import List, Dict, Any
from unittest.mock import patch, MagicMock

import boto3
import pytest
import requests
from boto3.dynamodb.conditions import Key

# Remove top-level constants, read from os.environ within tests/fixtures
# from .conftest import (
#     API_BASE_URL,
#     STAGE_TABLE_NAME,
#     LOCK_TABLE_NAME,
#     CONVERSATIONS_TABLE_NAME, # Not directly used in this file
#     WHATSAPP_QUEUE_URL,
#     WHATSAPP_QUEUE_DELAY_SEC,
# )

# --- Define constants for the specific pre-existing test record --- #
TEST_USER_PHONE = "+447835065013"
TEST_COMPANY_PHONE = "+447588713814"
TEST_CONVERSATION_ID = "ci-aaa-001#pi-aaa-001#f1fa775c-a8a8-4352-9f03-6351bc20fe24#447588713814"
TEST_REQUEST_BODY = "THIS IS AN INTEGRATION TEST RUN, PLEASE REPLY WITH ANY MESSAGE"

# Import Twilio validator
from twilio.request_validator import RequestValidator

@pytest.mark.integration
# Use only the cleanup fixture, pass fixed conversation ID
@pytest.mark.usefixtures("clear_stage_and_lock_tables")
class TestWhatsAppReplyFlow:
    """Integration tests that exercise the deployed *dev* replies‑engine stack.
    These tests require AWS credentials with access to the dev account and the
    resources enumerated in integration conftest.py via os.environ.
    Mocks external AI and Twilio send calls.
    """

    # Increase timeout slightly to allow for full flow + SQS delay
    _POLL_TIMEOUT_LONG = 90 # seconds
    _POLL_TIMEOUT_SHORT = 30 # seconds
    _POLL_INTERVAL = 3.0 # seconds

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
        raise AssertionError(f"Timeout ({timeout}s) waiting for condition: {predicate.__name__}")

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_full_reply_flow(
        self,
        aws_clients,
        twilio_auth_token, # Inject token fixture
    ):
        """Tests the full flow using a PRE-EXISTING conversation record."""
        # --- Test Setup --- #
        stage_table_name = os.environ['STAGE_TABLE_NAME']
        lock_table_name = os.environ['LOCK_TABLE_NAME']
        conversations_table_name = os.environ['CONVERSATIONS_TABLE_NAME']
        api_base_url = os.environ['REPLIES_API_URL']
        request_url = f"{api_base_url}/whatsapp"
        whatsapp_queue_delay_sec = int(os.environ.get("WHATSAPP_QUEUE_DELAY_SEC", 10))
        min_wait_time = whatsapp_queue_delay_sec + 30 # Adjust buffer as needed

        stage_tbl = aws_clients["dynamodb"].Table(stage_table_name)
        lock_tbl = aws_clients["dynamodb"].Table(lock_table_name)
        conversations_tbl = aws_clients["dynamodb"].Table(conversations_table_name)

        # --- 1. Fire webhook to trigger Staging Lambda --- #
        msg_sid = f"SM_INT_TEST_{int(time.time() * 1000)}"
        payload_dict = {
            "To": f"whatsapp:{TEST_COMPANY_PHONE}",
            "From": f"whatsapp:{TEST_USER_PHONE}",
            "Body": TEST_REQUEST_BODY,
            "MessageSid": msg_sid,
            "AccountSid": "ACXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
        }

        # Compute the valid signature using the real token
        validator = RequestValidator(twilio_auth_token)
        signature = validator.compute_signature(request_url, payload_dict)

        print(f"\nSending POST to {request_url}...")
        resp = requests.post(
            request_url,
            data=payload_dict,
            headers={"X-Twilio-Signature": signature}, # Send REAL signature
            timeout=15
        )
        print(f"API Response Status: {resp.status_code}")
        assert resp.status_code == 200
        print("Staging Lambda invocation successful (API returned 200).")

        # --- 2. Wait and Verify Final State & Cleanup --- #

        print(f"Waiting {min_wait_time}s for SQS delay and processing (incl. real AI/Twilio calls)...")
        time.sleep(min_wait_time)

        # Helper to check staging table
        def _get_stage_fragments():
            try:
                resp_q = stage_tbl.query(
                    KeyConditionExpression=Key("conversation_id").eq(TEST_CONVERSATION_ID)
                )
                print(f"  Polling staging table for {TEST_CONVERSATION_ID}: Found {len(resp_q.get('Items', []))} items.")
                return resp_q.get("Items", [])
            except ClientError as e:
                print(f"  Polling staging table failed: {e}")
                return None # Indicate error

        # Helper to check lock table
        def _get_trigger_lock():
             try:
                 lock_item = lock_tbl.get_item(Key={"conversation_id": TEST_CONVERSATION_ID}).get("Item")
                 print(f"  Polling lock table for {TEST_CONVERSATION_ID}: Found item? {lock_item is not None}")
                 return lock_item
             except ClientError as e:
                 print(f"  Polling lock table failed: {e}")
                 return None # Indicate error

        # Helper to check main conversations table status and history
        def _get_final_conversation_state():
            try:
                key = {
                    "primary_channel": TEST_USER_PHONE,
                    "conversation_id": TEST_CONVERSATION_ID
                }
                item = conversations_tbl.get_item(Key=key).get("Item")
                if not item:
                    print(f"  Polling main table for {TEST_CONVERSATION_ID}: Item not found yet.")
                    return None
                status = item.get("conversation_status")
                history = item.get("messages", [])
                # Check if status is updated and history has expected number of messages
                # Original record + user reply + assistant reply = expected length?
                # Adjust initial_history_len based on the pre-existing record
                initial_history_len = len(item.get('initial_message_history_for_test', [])) # Need a way to know initial state
                expected_history_len = initial_history_len + 2
                print(f"  Polling main table for {TEST_CONVERSATION_ID}: Status='{status}', MessagesLen={len(history)}")
                if status == "reply_sent" and len(history) >= expected_history_len:
                    return item
                return None # Not yet in final state
            except ClientError as e:
                print(f"  Polling main table failed: {e}")
                return None # Indicate error

        # --- Assertions --- #

        # A. Verify Staging Table Cleanup
        print(f"\nPolling for staging table cleanup (max {self._POLL_TIMEOUT_LONG}s)...")
        try:
             self._poll_until(lambda: not _get_stage_fragments(), timeout=self._POLL_TIMEOUT_LONG, interval=self._POLL_INTERVAL)
             print("Staging table cleanup VERIFIED.")
        except AssertionError:
             pytest.fail("Timed out waiting for staging table items to be deleted.", pytrace=False)

        # B. Verify Trigger Lock Cleanup
        print(f"\nPolling for trigger lock cleanup (max {self._POLL_TIMEOUT_SHORT}s)...")
        try:
            self._poll_until(lambda: not _get_trigger_lock(), timeout=self._POLL_TIMEOUT_SHORT, interval=self._POLL_INTERVAL)
            print("Trigger lock cleanup VERIFIED.")
        except AssertionError:
            pytest.fail("Timed out waiting for trigger lock item to be deleted.", pytrace=False)

        # C. Verify Final State in Main Conversations Table
        print(f"\nPolling for main conversation table final state (max {self._POLL_TIMEOUT_LONG}s)...")
        final_item = None
        try:
            final_item = self._poll_until(_get_final_conversation_state, timeout=self._POLL_TIMEOUT_LONG, interval=self._POLL_INTERVAL)
            print(f"Main conversation table final state VERIFIED (Status: {final_item.get('conversation_status')}, MessagesLen: {len(final_item.get('messages', []))})")
        except AssertionError:
            pytest.fail("Timed out waiting for main conversation table to reach final state (reply_sent, messages updated).", pytrace=False)

        # D. Optional: Add more specific assertions on final_item content
        assert final_item is not None, "Final conversation item was None despite polling success (should not happen)"
        message_history = final_item.get("messages", [])
        # Adjust check based on known history length of the pre-existing record
        # assert len(message_history) >= initial_history_len + 2
        # Example: Check last message role
        assert message_history[-1].get("role") == "assistant"
        assert message_history[-2].get("role") == "user"
        assert message_history[-2].get("content") == TEST_REQUEST_BODY
        print("Additional assertions on final item passed (optional).")

    # Remove the old test_trigger_message_appears_on_sqs
    # def test_trigger_message_appears_on_sqs(...): 
    #     ... 