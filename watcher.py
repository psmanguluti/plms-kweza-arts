"""
watcher.py — Reliable FL Studio file watcher.

Why PollingObserver?
  FL Studio on Windows uses atomic saves (write temp -> rename to .flp).
  The default WindowsApiObserver misses rename events and dies silently.
  PollingObserver checks file modification times on a fixed interval —
  slower but 100% reliable across all Windows versions and save patterns.
"""
import os, time, threading, logging
from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler

logging.basicConfig(level=logging.INFO,
                    format='[%(asctime)s] %(levelname)s: %(message)s')

# Module-level state
_observer     = None
_capture_fn   = None
_watch_folder = None
_lock         = threading.Lock()


class FLPHandler(FileSystemEventHandler):
    def __init__(self, capture_fn):
        self.capture_fn = capture_fn
        self._last = {}
        self._dlock = threading.Lock()

    def _debounce(self, path):
        now = time.time()
        with self._dlock:
            if now - self._last.get(path, 0) < 3.0:
                return False
            self._last[path] = now
            return True

    def _handle(self, path):
        if not path.lower().endswith('.flp'):
            return
        time.sleep(0.6)          # let FL Studio finish writing
        if not self._debounce(path):
            return
        logging.info(f'[Watcher] FLP change detected: {path}')
        try:
            self.capture_fn(path)
        except Exception as e:
            logging.error(f'[Watcher] Capture error: {e}', exc_info=True)

    def on_modified(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def on_created(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._handle(event.dest_path)


def _launch(folder, capture_fn):
    obs = PollingObserver(timeout=2)     # poll every 2 seconds
    obs.schedule(FLPHandler(capture_fn), path=folder, recursive=True)
    obs.start()
    logging.info(f'[Watcher] Polling: {os.path.abspath(folder)}')
    return obs


def _guardian(folder, capture_fn):
    """Restarts the observer automatically if it dies."""
    global _observer
    while True:
        time.sleep(5)
        with _lock:
            if _observer is None:
                break                    # stop() was called — exit guardian
            if not _observer.is_alive():
                logging.warning('[Watcher] Observer died — restarting...')
                try:
                    _observer.stop()
                except Exception:
                    pass
                try:
                    _observer = _launch(folder, capture_fn)
                    logging.info('[Watcher] Restarted successfully.')
                except Exception as e:
                    logging.error(f'[Watcher] Restart failed: {e}')


def start_watcher(folder, capture_fn):
    global _observer, _capture_fn, _watch_folder
    os.makedirs(folder, exist_ok=True)
    with _lock:
        if _observer and _observer.is_alive():
            _observer.stop()
            _observer.join(timeout=3)
        _capture_fn   = capture_fn
        _watch_folder = folder
        _observer     = _launch(folder, capture_fn)
    threading.Thread(target=_guardian, args=(folder, capture_fn),
                     daemon=True, name='watcher-guardian').start()
    return _observer


def stop_watcher():
    global _observer, _capture_fn, _watch_folder
    with _lock:
        if _observer and _observer.is_alive():
            _observer.stop()
            _observer.join(timeout=3)
        _observer = _capture_fn = _watch_folder = None
    logging.info('[Watcher] Stopped.')


def is_watching():
    return _observer is not None and _observer.is_alive()