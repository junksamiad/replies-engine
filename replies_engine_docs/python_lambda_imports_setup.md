# Python Imports Setup for AWS Lambda and Local Testing (`pytest`)

## 1. The Problem: Import Path Conflicts

Python is sometimes regarded as not being a great fit for AWS. 

When developing AWS Lambda functions in Python using AWS SAM and testing locally with tools like `pytest`, a common conflict arises regarding how Python modules are imported:

*   **AWS Lambda Runtime:** When a Lambda function is deployed, AWS typically takes the contents of the directory specified in the `template.yaml` `CodeUri` and places them at the root of the execution environment's Python path. Lambda then executes the handler specified (e.g., `index.lambda_handler`). In this context, if `index.py` tries to use relative imports like `from .utils import ...`, it fails with an `ImportError: attempted relative import with no known parent package` because, from its perspective, it's being run as a top-level script, not part of a package.
*   **Local `pytest`:** When running tests locally (e.g., `pytest tests/unit/` from the project root), `pytest` often discovers and runs tests in a way that *does* treat your source code directories (`src_dev/.../app/`) as packages (especially if they contain `__init__.py` files). In this context, relative imports (`from .utils import ...`) within your source code *work*, but absolute imports relative to the `app` directory (`from utils import ...`) might fail depending on the test execution path setup.

This leads to a frustrating cycle: code that works locally fails when deployed, or code that works when deployed fails local unit tests.

## 2. The Solution: Package Structure and SAM Configuration

We resolved this by restructuring the Lambda code slightly and adjusting the SAM template configuration. This approach ensures imports work consistently in both environments without requiring helper scripts to toggle import styles. 

**Key Components of the Solution:**

1.  **Code Directory Structure:**
    *   Inside the original Lambda source code directory (e.g., `src_dev/channel_router/app/`), create a dedicated subdirectory to act as the Python package for your Lambda code. We used `lambda_pkg`.
    *   Move **all** your Python source files and subdirectories (`index.py`, `utils/`, `services/`, `core/`, etc.) **into** this new `lambda_pkg` directory.
    *   Ensure an empty `__init__.py` file exists within the `lambda_pkg` directory to explicitly mark it as a Python package. Also ensure necessary `__init__.py` files exist in any subdirectories like `utils/`, `services/` etc.

    ```
    template-sender-engine/
    ├── src_dev/
    │   ├── channel_router/
    │   │   └── app/  <-- Original CodeUri root
    │   │       ├── requirements.txt  <-- requirements.txt stays HERE
    │   │       └── lambda_pkg/       <-- NEW Package Directory
    │   │           ├── __init__.py   <-- Marks lambda_pkg as package
    │   │           ├── index.py      <-- Handler code (uses relative imports)
    │   │           ├── utils/
    │   │           │   ├── __init__.py
    │   │           │   └── ...
    │   │           └── services/
    │   │               ├── __init__.py
    │   │               └── ...
    │   └── channel_processor/
    │       └── whatsapp/
    │           └── app/  <-- Original CodeUri root
    │               ├── requirements.txt  <-- requirements.txt stays HERE
    │               └── lambda_pkg/       <-- NEW Package Directory
    │                   ├── __init__.py
    │                   ├── index.py
    │                   └── ... (utils/, services/)
    ├── tests/
    │   └── unit/
    │       ├── channel_router/
    │       │   ├── test_index.py  <-- Tests import from ...app.lambda_pkg...
    │       │   └── ...
    │       └── channel_processor/
    │           └── ...
    └── template.yaml
    ```

2.  **`requirements.txt` Location:**
    *   The `requirements.txt` file for each Lambda function **must reside in the parent `app/` directory** (the directory specified by `CodeUri`), *not* inside the `lambda_pkg/` directory. `sam build` looks for this file relative to the `CodeUri`.

3.  **Imports *within* Lambda Code (`lambda_pkg/`):**
    *   All imports within the `lambda_pkg` directory that refer to other modules or sub-packages *also within* `lambda_pkg` **must use explicit relative imports**.
    *   **Example** (inside `src_dev/.../app/lambda_pkg/index.py`):
        ```python
        from .utils import request_parser
        from .services import dynamodb_service
        from .core import context_builder
        ```

4.  **`template.yaml` Configuration:**
    *   **`CodeUri`:** This property for the `AWS::Serverless::Function` resource must point to the **parent `app/` directory** (e.g., `src_dev/channel_router/app/`).
    *   **`Handler`:** This property must specify the handler function using the **package path** (e.g., `lambda_pkg.index.lambda_handler`). This tells Lambda to import the `lambda_pkg` package first, then find the `index` module and the `lambda_handler` function within it.

    ```yaml
    Resources:
      ChannelRouterFunction:
        Type: AWS::Serverless::Function
        Properties:
          FunctionName: !Sub '${ProjectPrefix}-channel-router-${EnvironmentName}'
          CodeUri: src_dev/channel_router/app/       # Points to parent dir
          Handler: lambda_pkg.index.lambda_handler # Points into the package
          Runtime: python3.11
          # ... other properties
      WhatsAppProcessorFunction:
        Type: AWS::Serverless::Function
        Properties:
          FunctionName: !Sub '${ProjectPrefix}-whatsapp-channel-processor-${EnvironmentName}'
          CodeUri: src_dev/channel_processor/whatsapp/app/ # Points to parent dir
          Handler: lambda_pkg.index.lambda_handler      # Points into the package
          Runtime: python3.11
          # ... other properties
    ```

5.  **Imports within Unit Tests (`tests/unit/...`):**
    *   Test files must import the application code using the **full path** relative to the project root, including the `lambda_pkg` directory.
    *   Any `@patch` decorators used in tests must also use the full path including `lambda_pkg` when targeting objects within the application code.
    *   **Example** (inside `tests/unit/channel_router/test_index.py`):
        ```python
        from src_dev.channel_router.app.lambda_pkg.index import lambda_handler
        from src_dev.channel_router.app.lambda_pkg.services import dynamodb_service # If importing directly

        # Example patch
        @patch('src_dev.channel_router.app.lambda_pkg.services.dynamodb_service.get_company_config')
        def test_something(mock_get_config):
            # ...
        ```

## 3. Why This Works

*   **Lambda:** By setting `Handler` to `lambda_pkg.index.lambda_handler`, Lambda imports `lambda_pkg` first. When `index.py` is executed, it knows it's part of the `lambda_pkg` package, so relative imports like `from .utils import ...` resolve correctly to other modules within `lambda_pkg`. The `CodeUri` pointing to the parent `app/` ensures `lambda_pkg` and `requirements.txt` are correctly packaged.
*   **`pytest`:** When run from the project root, `pytest` can find the `src_dev` directory. Imports in test files like `from src_dev.channel_router.app.lambda_pkg...` work because `pytest` adds the project root to the Python path. The relative imports *within* the `lambda_pkg` code also work correctly in this testing context because `lambda_pkg` is treated as a package (due to `__init__.py`).

This configuration (CodeUri: .../app/, Handler: lambda_pkg.index.lambda_handler) is the one commonly recommended for making relative imports work consistently.

## 4. Summary

This setup provides a consistent structure that satisfies both the Lambda runtime and local `pytest` execution without needing brittle workarounds like `sys.path` manipulation or import-toggling scripts. Remember the key locations: code inside `lambda_pkg` using relative imports, `requirements.txt` outside `lambda_pkg` (in `app/`), `CodeUri` pointing to `app/`, and `Handler` pointing to `lambda_pkg.index.lambda_handler`. Test files import using the full `src_dev...lambda_pkg...` path.
