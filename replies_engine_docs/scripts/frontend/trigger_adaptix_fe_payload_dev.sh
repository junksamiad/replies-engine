#!/bin/bash

# --- Replace placeholders below! ---
API_GATEWAY_URL="https://xlijn1k4xh.execute-api.eu-north-1.amazonaws.com/dev/initiate-conversation"
API_KEY="YbgTABlGlg6s2YZ9gcyuB4AUhi5jJcC05yeKcCWR"
# ---------------------------------

# Generate a unique request ID using Python
REQUEST_ID=$(python -c 'import uuid; print(uuid.uuid4())')
# Generate current timestamp in ISO 8601 format
REQUEST_TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

echo "Generated Request ID: $REQUEST_ID"
echo "Generated Timestamp: $REQUEST_TIMESTAMP"

# Use a Here Document for the base request body
# We'll use placeholders for dynamic values and substitute them later
read -r -d '' REQUEST_BODY_TEMPLATE <<'EOF'
{
  "company_data": {
    "company_id": "ci-aaa-000",
    "project_id": "pi-aaa-000"
  },
  "recipient_data": {
    "recipient_first_name": "User",
    "recipient_last_name": "One",
    "recipient_tel": "+447835065013",
    "recipient_email": "accounts@adaptixinnovation.co.uk",
    "comms_consent": true
  },
  "project_data": {
    "adaptix_id": "use_case_1",
    "projects": []
  },
  "request_data": {
    "request_id": "PLACEHOLDER_REQUEST_ID",
    "channel_method": "whatsapp",
    "initial_request_timestamp": "PLACEHOLDER_TIMESTAMP"
  }
}
EOF

# Substitute placeholders with dynamic values
# Using sed for simple substitution
REQUEST_BODY=$(echo "$REQUEST_BODY_TEMPLATE" | sed "s/PLACEHOLDER_REQUEST_ID/$REQUEST_ID/" | sed "s/PLACEHOLDER_TIMESTAMP/$REQUEST_TIMESTAMP/")

echo "Sending request to: $API_GATEWAY_URL"
echo "Request Body:"
echo "$REQUEST_BODY" # Optional: print the body to verify

curl -X POST "$API_GATEWAY_URL" \
-H "Content-Type: application/json" \
-H "x-api-key: $API_KEY" \
-d "$REQUEST_BODY"

echo "\nRequest complete." 