# Running Integration Tests Locally

These tests interact with **real, deployed AWS resources** in the `dev` environment. Ensure your AWS credentials are configured correctly for the target account and region (`eu-north-1`).

## Prerequisites

1.  **Deployed `dev` Stack:** The `ai-multi-comms-replies-dev` CloudFormation stack must be successfully deployed via `sam deploy`.
2.  **Test Conversation Record:** A specific test record **must exist** in the `ai-multi-comms-conversations-dev` DynamoDB table. This record provides essential configuration (secret names, Assistant ID, Thread ID) for the test run.
    *   **Record Identifiers Used by Test:**
        *   `primary_channel` (User Phone): `+447835065013`
        *   `conversation_id`: `ci-aaa-000#pi-aaa-000#f1fa775c-a8a8-4352-9f03-6351bc20fe24#447588713814`
    *   **Creating/Updating the Record:** You can use the `template-sender-engine` trigger script (`replies_engine_docs/scripts/trigger_e2e_test.sh`) to create this record initially if needed, or ensure the `company_data_dev_test_record.json` file contains the correct details and run the `create_test_company_data_records.py` script. Ensure the `thread_id` and `assistant_id_replies` in the record are valid for your OpenAI account.
3.  **Environment Variables:**
    *   Runtime variables needed by the tests (API URL, table names, queue URL) are set automatically by the `pytest_configure` hook in `tests/integration/conftest.py` using the correct `dev` values.
    *   Dummy variables for *unit tests* are set via `pytest.ini` and the `pytest-env` plugin but are overridden for integration runs by the `conftest.py` hook.
4.  **Python Environment:** Ensure your virtual environment is active and required packages (`pytest`, `requests`, `boto3`, `twilio`) are installed.

## Running the Tests

Execute the following command from the project root (`replies-engine/`):

```bash
pytest -m integration tests/integration
```

*   The `-m integration` flag selects only tests marked with `@pytest.mark.integration`.

## Important Notes

*   **Real External Calls:** These tests make **real API calls** to OpenAI and Twilio using the credentials specified in the Secrets Manager secrets referenced by the test DynamoDB record. This will incur costs and potentially send real WhatsApp messages.
*   **Test Record State:** The tests rely on the pre-existing DynamoDB record. After a successful run, the test attempts to reset the `conversation_status` of this record back to `initial_message_sent`. If a test fails mid-execution, the record might be left in an intermediate state (`reply_sent`) or the staging/lock tables might not be cleaned up, requiring manual intervention before the next run against the same record. 