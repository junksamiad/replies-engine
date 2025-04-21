# utils/sqs_heartbeat.py - Messaging Lambda (WhatsApp)

"""
Implements an SQS Heartbeat mechanism using a background thread
to extend the visibility timeout of an SQS message.
"""

import threading
import time
import logging
import boto3
import os # Added os import for LOG_LEVEL
from botocore.exceptions import ClientError
from typing import Optional

# Initialize logger for this module
logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO').upper()) # Use env var

# Default visibility timeout extension duration (matches queue default)
DEFAULT_VISIBILITY_TIMEOUT_EXTENSION_SEC = 600 # 10 minutes

class SQSHeartbeat:
    """
    Manages extending the visibility timeout for an SQS message in a background thread.
    """
    def __init__(self, queue_url: str, receipt_handle: str,
                 interval_sec: int, visibility_timeout_sec: int = DEFAULT_VISIBILITY_TIMEOUT_EXTENSION_SEC):
        """
        Initializes the SQSHeartbeat instance.

        Args:
            queue_url: The URL of the SQS queue.
            receipt_handle: The receipt handle of the message to extend.
            interval_sec: The interval (in seconds) at which to extend the visibility timeout.
                          This should be significantly less than visibility_timeout_sec.
            visibility_timeout_sec: The new visibility timeout (in seconds) to set on each extension.
        """
        if not all([queue_url, receipt_handle]):
             raise ValueError("queue_url and receipt_handle cannot be empty.")
        if interval_sec <= 0:
             raise ValueError("interval_sec must be positive.")
        if visibility_timeout_sec <= interval_sec:
             logger.warning(f"Visibility timeout ({visibility_timeout_sec}s) is not significantly longer than interval ({interval_sec}s). Heartbeat may not be effective.")

        self.queue_url = queue_url
        self.receipt_handle = receipt_handle
        self.interval_sec = interval_sec
        self.visibility_timeout_sec = visibility_timeout_sec

        # Internal state
        self._stop_event = threading.Event()
        self._thread = None
        self._error = None
        self._running = False
        self._lock = threading.Lock() # Protects access to _error and _running

        # Initialize SQS client within the class
        # The Lambda execution role must have sqs:ChangeMessageVisibility permission
        try:
            self._sqs_client = boto3.client("sqs")
            logger.debug("Internal SQS client initialized for heartbeat.")
        except Exception as e:
            logger.exception("Failed to initialize boto3 SQS client for heartbeat.")
            raise RuntimeError("Could not initialize SQS client for heartbeat") from e


    def _run(self):
        """The target function for the background heartbeat thread."""
        logger.info(f"Heartbeat thread started for receipt handle: ...{self.receipt_handle[-10:]}")
        while not self._stop_event.wait(self.interval_sec): # Wait for interval or stop signal
            try:
                logger.info(f"Extending visibility timeout by {self.visibility_timeout_sec}s for receipt handle: ...{self.receipt_handle[-10:]}")
                self._sqs_client.change_message_visibility(
                    QueueUrl=self.queue_url,
                    ReceiptHandle=self.receipt_handle,
                    VisibilityTimeout=self.visibility_timeout_sec
                )
                logger.debug(f"Successfully extended visibility for ...{self.receipt_handle[-10:]}")
            except ClientError as e:
                error_code = e.response.get('Error', {}).get('Code', 'Unknown')
                logger.error(f"Heartbeat failed for ...{self.receipt_handle[-10:]}. Error: {error_code} - {e}")
                with self._lock:
                    # Only store the first error encountered
                    if self._error is None:
                       self._error = e
                # Signal the thread to stop, don't call stop() from within _run
                self._stop_event.set()
                break # Exit the loop immediately after setting stop event
            except Exception as e:
                logger.exception(f"Unexpected error in heartbeat thread for ...{self.receipt_handle[-10:]}: {e}")
                with self._lock:
                     if self._error is None:
                        self._error = e
                # Signal the thread to stop
                self._stop_event.set()
                break

        logger.info(f"Heartbeat thread stopped for receipt handle: ...{self.receipt_handle[-10:]}")
        with self._lock:
            self._running = False # Ensure running flag is set to false when thread exits

    def start(self):
        """Starts the background heartbeat thread."""
        with self._lock:
            if self._running:
                logger.warning("Heartbeat thread start called when already running.")
                return
            if self._thread is not None and self._thread.is_alive():
                 logger.warning("Attempting to start heartbeat when previous thread might still be alive. Not restarting.")
                 return # Prevent multiple threads

            self._stop_event.clear() # Ensure stop event is not set
            self._error = None       # Clear any previous error
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            self._running = True
            logger.info(f"Heartbeat thread initiated for ...{self.receipt_handle[-10:]}")

    def stop(self):
        """Signals the heartbeat thread to stop and waits for it to terminate."""
        if self._thread is None:
            logger.debug("Stop called but heartbeat thread was never started or already stopped.")
            return

        # Check if already stopped or trying to stop from within itself
        if self._stop_event.is_set() or self._thread is threading.current_thread():
            logger.debug(f"Heartbeat stop signal already sent or called from within thread for ...{self.receipt_handle[-10:]}. No action needed.")
            return

        logger.info(f"Stopping heartbeat thread for ...{self.receipt_handle[-10:]}...")
        self._stop_event.set() # Signal the thread to stop waiting/processing

        # Wait for the thread to finish
        # Add a reasonable timeout
        join_timeout_seconds = self.interval_sec + 10 # Wait a bit longer than the interval
        self._thread.join(timeout=join_timeout_seconds)

        if self._thread.is_alive():
             logger.warning(f"Heartbeat thread for ...{self.receipt_handle[-10:]} did not terminate gracefully after {join_timeout_seconds}s.")
        else:
             logger.debug(f"Heartbeat thread for ...{self.receipt_handle[-10:]} joined successfully.")

        # Clean up reference to the thread object after ensuring it's stopped/joined
        self._thread = None
        # Note: _running flag is set to False inside the _run method when it exits.

    def check_for_errors(self) -> Optional[Exception]:
        """
        Checks if any errors occurred in the heartbeat thread.

        Returns:
            The first Exception encountered, or None if no errors occurred.
        """
        with self._lock:
            return self._error

    @property
    def running(self) -> bool:
        """Returns True if the heartbeat thread is currently marked as running and alive."""
        with self._lock:
            # Check both the flag and the thread's liveness for robustness
            is_alive = self._thread is not None and self._thread.is_alive()
            # Log if flag is true but thread is not alive (indicates potential race or unclean exit)
            if self._running and not is_alive:
                 logger.warning(f"Heartbeat running flag is True, but thread for ...{self.receipt_handle[-10:]} is not alive.")
            return self._running and is_alive 