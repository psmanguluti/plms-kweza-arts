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
_token        = 0
_lock         = threading.Lock()

# File-stability check (R2): a file is copied only after its size AND modified time
# have stopped changing. STABLE_CHECKS identical readings, STABLE_INTERVAL seconds apart,
# i.e. the file must be quiet for (STABLE_CHECKS - 1) * STABLE_INTERVAL seconds.
STABLE_INTERVAL = 1.0
STABLE_CHECKS   = 3
STABLE_TIMEOUT  = 120.0


def _signature(path):
    """(size, mtime_ns) or None when the file is missing / locked mid-rename."""
    try:
        st = os.stat(path)
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def wait_until_stable(path, interval=None, checks=None, timeout=None,
                      sleep=time.sleep, clock=time.monotonic):
    """Block until `path` has the same size and modified time on `checks` consecutive
    readings, `interval` seconds apart. Returns True when stable, False on timeout.
    An empty or missing file is never stable (FL Studio writes a temp file, then renames)."""
    interval = STABLE_INTERVAL if interval is None else interval
    checks   = STABLE_CHECKS   if checks   is None else checks
    timeout  = STABLE_TIMEOUT  if timeout  is None else timeout
    deadline = clock() + timeout
    last, same = None, 0
    while True:
        sig = _signature(path)
        if sig is not None and sig[0] > 0 and sig == last:
            same += 1
        else:
            same = 1 if (sig is not None and sig[0] > 0) else 0
        last = sig
        if same >= checks:
            return True
        if clock() >= deadline:
            return False
        sleep(interval)


class FLPHandler(FileSystemEventHandler):
    def __init__(self, capture_fn):
        self.capture_fn = capture_fn
        self._last  = {}             # path -> (size, mtime_ns) of the last file we captured
        self._busy  = set()          # paths currently waiting for a stable file
        self._dirty = set()          # paths that changed again while we were waiting
        self._dlock = threading.Lock()

    def _begin(self, path):
        """True if this thread should process `path`; otherwise mark it dirty so the
        thread already waiting on it re-checks once more."""
        with self._dlock:
            if path in self._busy:
                self._dirty.add(path)
                return False
            self._busy.add(path)
            return True

    def _end(self, path):
        """True if the file changed again while we worked (caller loops once more)."""
        with self._dlock:
            if path in self._dirty:
                self._dirty.discard(path)
                return True
            self._busy.discard(path)
            return False

    def _handle(self, path):
        if not path.lower().endswith('.flp'):
            return
        if '/backup/' in path.lower().replace('\\', '/'):
            return               # FL Studio's own Backup folder
        if not self._begin(path):
            return
        while True:
            if wait_until_stable(path):
                sig = _signature(path)
                if sig is not None and self._last.get(path) == sig:
                    pass                          # same file we already captured: nothing new
                else:
                    logging.info(f'[Watcher] FLP change detected: {path}')
                    try:
                        self.capture_fn(path)
                        self._last[path] = sig
                    except Exception as e:
                        logging.error(f'[Watcher] Capture error: {e}', exc_info=True)
            else:
                logging.warning(f'[Watcher] {path} never stopped changing; skipped '
                                f'(the next save will be captured).')
            if not self._end(path):
                break

    def on_modified(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def on_created(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._handle(event.dest_path)


def _as_folders(folders):
    """Accept one path or a list of paths; return unique, existing, non-nested folders."""
    if isinstance(folders, (str, bytes, os.PathLike)):
        folders = [folders]
    out = []
    for f in folders:
        f = os.path.abspath(os.fspath(f))
        os.makedirs(f, exist_ok=True)
        if f not in out:
            out.append(f)
    # a folder inside another watched folder is already covered (recursive=True)
    def covered(f):
        return any(o != f and os.path.commonpath([o, f]) == o for o in out)
    return [f for f in out if not covered(f)]


def _launch(folders, capture_fn):
    obs = PollingObserver(timeout=2)     # poll every 2 seconds
    handler = FLPHandler(capture_fn)
    for folder in folders:
        obs.schedule(handler, path=folder, recursive=True)
        logging.info(f'[Watcher] Polling: {folder}')
    obs.start()
    return obs


def _guardian(token, folders, capture_fn):
    """Restarts the observer automatically if it dies. Exits when a newer watcher replaces it."""
    global _observer
    while True:
        time.sleep(5)
        with _lock:
            if _observer is None or _token != token:
                break                    # stopped or superseded
            if not _observer.is_alive():
                logging.warning('[Watcher] Observer died — restarting...')
                try:
                    _observer.stop()
                except Exception:
                    pass
                try:
                    _observer = _launch(folders, capture_fn)
                    logging.info('[Watcher] Restarted successfully.')
                except Exception as e:
                    logging.error(f'[Watcher] Restart failed: {e}')


def start_watcher(folders, capture_fn):
    """Watch one folder or a list of folders for .flp changes."""
    global _observer, _capture_fn, _watch_folder, _token
    folders = _as_folders(folders)
    if not folders:
        raise ValueError('No folders to watch.')
    with _lock:
        if _observer and _observer.is_alive():
            _observer.stop()
            _observer.join(timeout=3)
        _capture_fn   = capture_fn
        _watch_folder = folders
        _token       += 1
        token         = _token
        _observer     = _launch(folders, capture_fn)
    threading.Thread(target=_guardian, args=(token, folders, capture_fn),
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