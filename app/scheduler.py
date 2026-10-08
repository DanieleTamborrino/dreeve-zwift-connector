import logging
import os
import threading
import time
from datetime import datetime, timezone
from croniter import croniter
from app.sync import perform_sync, load_tokens

logger = logging.getLogger("zwift_connector.scheduler")

class SyncScheduler:
    def __init__(self):
        # Default to once per day at 02:00 AM UTC (0 2 * * *)
        self.cron_expr = os.getenv("SYNC_CRON", "0 2 * * *").strip()
        self.is_syncing = False
        self.last_sync_time = None
        self.next_sync_time = None
        self.last_result = None
        self._thread = None
        self._stop_event = threading.Event()

    def get_next_cron_run(self, now=None):
        """Calculate next scheduled run time using 5-field cron format."""
        if not now:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            iter_obj = croniter(self.cron_expr, now)
            return iter_obj.get_next(datetime)
        except Exception as e:
            logger.error(f"Invalid cron expression '{self.cron_expr}': {e}")
            return None

    def start(self):
        """Start the background cron scheduler thread."""
        if not self.cron_expr:
            logger.info("SYNC_CRON is empty. Cron scheduler disabled.")
            return

        logger.info(f"Starting background cron scheduler with expression: '{self.cron_expr}'")
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the background scheduler thread."""
        self._stop_event.set()

    def _run_loop(self):
        time.sleep(3)

        while not self._stop_event.is_set():
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            next_run = self.get_next_cron_run(now)

            if not next_run:
                logger.error(f"Cron scheduler disabled due to invalid cron expression '{self.cron_expr}'.")
                break

            self.next_sync_time = next_run.isoformat() + "Z"
            wait_seconds = max((next_run - now).total_seconds(), 1)
            logger.info(f"Cron schedule '{self.cron_expr}': Next sync at {self.next_sync_time} (in {wait_seconds:.0f}s)")

            for _ in range(int(wait_seconds)):
                if self._stop_event.is_set():
                    break
                time.sleep(1)

            if not self._stop_event.is_set():
                tokens = load_tokens()
                if tokens and "access_token" in tokens:
                    self.run_sync()
                
                # Prevent duplicate trigger within the same minute
                time.sleep(65)

    def run_sync(self, time_window: str = None) -> dict:
        """Execute a sync run asynchronously in a worker thread if not already running."""
        if self.is_syncing:
            logger.warning("Sync is already in progress. Skipping duplicate execution.")
            return {"status": "in_progress", "message": "Sync is already in progress."}

        self.is_syncing = True
        self.last_sync_time = datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"
        
        def _worker():
            try:
                logger.info(f"Executing sync task worker (time_window={time_window})...")
                result = perform_sync(time_window=time_window)
                self.last_result = result
            except Exception as e:
                logger.error(f"Unhandled exception during sync: {e}")
                self.last_result = {"status": "error", "message": str(e), "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"}
            finally:
                self.is_syncing = False

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

        return {
            "status": "started",
            "message": "Sync started in background.",
            "time_window": time_window or os.getenv("SYNC_TIME_WINDOW", "1_week")
        }

    def get_status(self) -> dict:
        tokens = load_tokens()
        has_tokens = bool(tokens and "access_token" in tokens)
        
        return {
            "authenticated": has_tokens,
            "cron_expr": self.cron_expr,
            "is_syncing": self.is_syncing,
            "last_sync_time": self.last_sync_time,
            "next_sync_time": self.next_sync_time,
            "last_result": self.last_result
        }

# Global singleton scheduler instance
scheduler = SyncScheduler()
