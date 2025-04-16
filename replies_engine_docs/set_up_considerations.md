# Considerations for Separate Microservices Sharing DynamoDB Tables

This document outlines key considerations when setting up two separate microservices (e.g., `template-sender-engine` and `replies-engine`), each in its own project/repository with its own `template.yaml`, that need to access the same DynamoDB tables.

## 1. Shared Access is Acceptable

Having multiple independent microservices access the same DynamoDB tables is a common and acceptable pattern. DynamoDB is designed for concurrent access.

## 2. IAM Permissions

*   **Requirement:** The IAM execution roles associated with the Lambda functions in **both** microservices' `template.yaml` files must be granted the necessary permissions (e.g., `dynamodb:GetItem`, `dynamodb:PutItem`, `dynamodb:UpdateItem`, `dynamodb:Query`) to interact with the shared tables (`ConversationsTable`, `CompanyDataTable`).
*   **Least Privilege:** Grant only the permissions required by each service.

## 3. Resource Definition (Crucial)

*   **Define Once:** The `AWS::DynamoDB::Table` resources for the shared tables should be defined in **only one** location.
    *   **Option A:** Define them in the `template.yaml` of one of the microservices (e.g., the original `template-sender-engine`).
    *   **Option B (Recommended for Scalability):** Define them in a separate, dedicated AWS SAM/CloudFormation stack focused solely on shared infrastructure.
*   **Do Not Duplicate:** Avoid defining the same table resource in multiple `template.yaml` files, as this will cause deployment conflicts.
*   **Reference Elsewhere:** The microservice(s) that *do not* define the table resource will need the table names passed to them (e.g., via environment variables set in their `template.yaml`). Their Lambda functions will use these names to interact with the tables using the AWS SDK.

## 4. Schema Coordination

*   **Consistency:** Both microservices reading from and writing to the same table(s) must agree on the data schema (attribute names, data types, expected structure).
*   **Evolution:** Changes to the schema must be coordinated to avoid breaking one of the services.

## 5. Concurrency Control

*   **Race Conditions:** If both services could potentially update the *same item* concurrently (e.g., changing status fields), use DynamoDB's conditional updates (`ConditionExpression`) to prevent lost updates or inconsistent state.
*   **Idempotency:** Implement idempotency logic (e.g., using conditional writes based on unique IDs or state transitions) where necessary, especially when processing messages from queues.

## 6. Capacity Planning

*   **Shared Load:** Both services contribute to the read/write throughput consumed from the shared tables.
*   **Provisioning:** Ensure the tables' provisioned capacity (or on-demand scaling configuration) is sufficient to handle the combined peak load from both microservices.

By addressing these points, you can successfully manage shared DynamoDB tables between separate microservice projects. 