"""
watcher.py — Reliable FL Studio file watcher.

FL Studio on Windows uses atomic saves:
  1. Writes to a temp file  (triggers on_created)
  2. Renames temp → final   (triggers on_moved)
  3. Sometimes also fires on_modified

We listen to ALL three events so nothing is missed.
"""
import os, time, threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


class FLPHandler(FileSystemEventHandler):
    def __init__(self, capture_fn):
        self.capture_fn = capture_fn
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    # ── internal debounce ──────────────────────────────────────
    def _should_fire(self, path: str) -> bool:
        now = time.time()
        with self._lock:
            if now - self._last.get(path, 0) < 2.5:
                return False
            self._last[path] = now
            return True

    def _handle(self, path: str):
        if not path.lower().endswith('.flp'):
            return
        # Small delay so FL Studio finishes writing before we read
        time.sleep(0.4)
        if self._should_fire(path):
            print(f'[Watcher] Captured: {path}')
            try:
                self.capture_fn(path)
            except Exception as e:
                print(f'[Watcher] Error: {e}')

    # ── watchdog events ────────────────────────────────────────
    def on_modified(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def on_created(self, event):
        """Catches FL Studio writing the initial temp file."""
        if not event.is_directory:
            self._handle(event.src_path)

    def on_moved(self, event):
        """Catches FL Studio renaming temp → final .flp (atomic save)."""
        if not event.is_directory:
            self._handle(event.dest_path)


# ── Module-level state ─────────────────────────────────────────
_observer: Observer | None = None


def start_watcher(folder: str, capture_fn) -> Observer:
    global _observer
    os.makedirs(folder, exist_ok=True)

    if _observer and _observer.is_alive():
        _observer.stop()
        _observer.join(timeout=3)

    handler   = FLPHandler(capture_fn)
    _observer = Observer()
    _observer.schedule(handler, path=folder, recursive=True)
    _observer.start()
    print(f'[Watcher] Monitoring: {os.path.abspath(folder)}')
    return _observer


def stop_watcher():
    global _observer
    if _observer and _observer.is_alive():
        _observer.stop()
        _observer.join(timeout=3)
    _observer = None


def is_watching() -> bool:
    return _observer is not None and _observer.is_alive()