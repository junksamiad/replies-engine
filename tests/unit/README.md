# Running Unit Tests Locally

## Important: Setting PYTHONPATH

Due to the project structure and how modules are imported (e.g., `from src. ...`), the Python interpreter needs help finding the source code modules when running `pytest` from the project root directory (`replies-engine/`).

If you run `pytest tests/unit/` directly, you will likely encounter `ModuleNotFoundError: No module named 'src'`. While `pytest.ini` contains `pythonpath = src`, this setting may not be consistently picked up in all local environments.

**To ensure tests run correctly:** You should explicitly prepend the `src` directory to your `PYTHONPATH` environment variable *before* running the tests in your current terminal session.

### Command

Execute the following command in your terminal from the `replies-engine` project root directory (ensure your virtual environment is active):

```bash
PYTHONPATH=$(pwd)/src:$PYTHONPATH pytest tests/unit
```
*(Note: Placing the new path at the beginning `$(pwd)/src:$PYTHONPATH` is often more reliable than appending)*

Alternatively, you can export it first for the session:
```bash
export PYTHONPATH=$(pwd)/src:$PYTHONPATH
pytest tests/unit
```

### Notes

*   You need to use this prefix (or run the `export` command) **once per terminal session** before running the unit tests locally.
*   The `pytest-env` plugin configured in `pytest.ini` handles setting *runtime* environment variables needed by the code (like table names, queue URLs), but **not** the Python import path itself.
*   This `PYTHONPATH` adjustment is typically **not** needed in CI/CD environments where the source checkout structure and execution context are usually configured differently. 