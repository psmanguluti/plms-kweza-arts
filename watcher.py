"""
watcher.py — Reliable FL Studio file watcher with detailed logging.

FL Studio on Windows uses atomic saves:
  1. Writes to a temp file  (triggers on_created)
  2. Renames temp → final   (triggers on_moved)
  3. Sometimes also fires on_modified

We listen to ALL three events so nothing is missed.
"""
import os, time, threading, logging
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Configure logging for the watcher module
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')


class FLPHandler(FileSystemEventHandler):
    def __init__(self, capture_fn):
        self.capture_fn = capture_fn
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        logging.info("FLPHandler initialized")

    # ── internal debounce ──────────────────────────────────────
    def _should_fire(self, path: str) -> bool:
        now = time.time()
        with self._lock:
            if now - self._last.get(path, 0) < 2.5:
                logging.debug(f"Debounced {path} (too soon)")
                return False
            self._last[path] = now
            return True

    def _handle(self, path: str):
        if not path.lower().endswith('.flp'):
            logging.debug(f"Ignored non‑FLP file: {path}")
            return
        logging.info(f"FLP event detected: {path}")
        # Small delay so FL Studio finishes writing before we read
        time.sleep(0.4)
        if self._should_fire(path):
            logging.info(f"Calling capture function for {path}")
            try:
                self.capture_fn(path)
                logging.info(f"Capture completed for {path}")
            except Exception as e:
                logging.error(f"Error in capture function: {e}", exc_info=True)

    # ── watchdog events ────────────────────────────────────────
    def on_modified(self, event):
        if not event.is_directory:
            logging.debug(f"on_modified: {event.src_path}")
            self._handle(event.src_path)

    def on_created(self, event):
        """Catches FL Studio writing the initial temp file."""
        if not event.is_directory:
            logging.debug(f"on_created: {event.src_path}")
            self._handle(event.src_path)

    def on_moved(self, event):
        """Catches FL Studio renaming temp → final .flp (atomic save)."""
        if not event.is_directory:
            logging.debug(f"on_moved: {event.dest_path} (was {event.src_path})")
            self._handle(event.dest_path)


# ── Module-level state ─────────────────────────────────────────
_observer: Observer | None = None


def start_watcher(folder: str, capture_fn) -> Observer:
    global _observer
    os.makedirs(folder, exist_ok=True)
    logging.info(f"Starting watcher on folder: {folder}")

    if _observer and _observer.is_alive():
        logging.info("Stopping existing watcher...")
        _observer.stop()
        _observer.join(timeout=3)

    handler = FLPHandler(capture_fn)
    _observer = Observer()
    _observer.schedule(handler, path=folder, recursive=True)
    _observer.start()
    logging.info(f"Watcher now monitoring {os.path.abspath(folder)}")
    return _observer


def stop_watcher():
    global _observer
    if _observer and _observer.is_alive():
        logging.info("Stopping watcher...")
        _observer.stop()
        _observer.join(timeout=3)
    _observer = None
    logging.info("Watcher stopped")


def is_watching() -> bool:
    return _observer is not None and _observer.is_alive()