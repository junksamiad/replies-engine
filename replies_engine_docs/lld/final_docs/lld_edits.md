# LLD Edits Summary

Below is a consolidated list of the low‑level design edits we've agreed upon so far for the **Conversations‑Stage DynamoDB table** and the **Messaging Lambda** logic in the replies‑engine.

---

## 1. Conversations‑Stage Table Edits

1.  **Key Schema**
    -  **Partition Key (PK):** `conversation_id` (String)
    -  **Sort Key (SK):** `message_sid` (String)

2.  **Attributes to Persist**
    -  `conversation_id` (S)
    -  `message_sid` (S)
    -  `primary_channel` (S)  
       • Enables a direct GetItem on the main Conversations table without GSIs.
    -  `body` (S)  
       • The user's incoming message text fragment.
    -  `sender_id` (S)  
       • (Optional) For audit or deduplication of retried webhooks.
    -  `received_at` (S or N)  
       • UTC timestamp when the fragment arrived, used for ordering.
    -  `expires_at` (N) – TTL  
       • Unix epoch (seconds) = now + batch window (W) + safety buffer.

3.  **Removed**
    - Eliminated storing the full `context_object` map in the stage table.

---

## 2. Messaging Lambda Logic Edits

### 2.1 Stage Table Query

-  **DynamoDB Query** must set **ConsistentRead = true** to guarantee all just‑written fragments are visible.
-  **Sort** the returned `Items` in Lambda by `(received_at, message_sid)` to reconstruct true arrival order.

### 2.2 Merge Step

-  **Concatenate** all `body` fields into a single string:
   ```python
   combined_body = "\n".join(item['body'] for item in items)
   ```
-  This produces one logical "user turn" for downstream processing.

### 2.3 Hydrate Canonical Conversation Row

-  Perform a **GetItem** on the main `conversations-dev` table using:
   -  `primary_channel` (from the first stage item)
   -  `conversation_id` (batch key)
-  Use **ConsistentRead = true** to ensure the freshest state.

### 2.4 Atomic Conditional Update (Lock + Append)

-  Issue one **UpdateItem** that:
   1. **Appends** a new message entry to `messages` (using `list_append(if_not_exists(...), ...)`).
   2. **Sets** `conversation_status` to `processing_reply`.
   3. **Updates** `updated_at` with the current timestamp.
-  **ConditionExpression:**
   ```text
   conversation_status <> 'processing_reply'
   ```
-  **Behavior on Condition Failure:**
  - DynamoDB throws `ConditionalCheckFailedException` if the status is already `processing_reply`.
  - Catch this exception and **treat it as a successful lock** (i.e., continue processing without re‑appending).

### 2.5 Outbound Context Assembly

-  After the update, **fetch** the updated conversation record.
-  Build the next `context_object` for the downstream step by combining:
   -  The fresh conversation record.
   -  `combined_body` (latest user turn).
   -  Any routing metadata (e.g., target queue URL, channel method).

### 2.6 Cleanup Timing

-  **Do not** delete stage table items or the trigger‑lock entry until **after**:
  1. The downstream reply (e.g., Twilio/API call) succeeds.
  2. The final conversation table update (e.g., setting status to `reply_sent`).
-  Leaving items intact ensures safe retries in failure scenarios.

### 2.7 DLQ Failure Handling

-  Implement in the Messaging Lambda's error path:  
  If processing ultimately fails and the trigger message moves to the DLQ, **update** the main conversation record to set:
  ```text
  conversation_status = 'reply_failed'
  ```
-  This can be done *before* re‑throwing the exception so the record reflects failure even if the function is retried.

---

*These edits ensure a lean, reliable staging mechanism and a clear, atomic processing flow in the Messaging Lambda.* 