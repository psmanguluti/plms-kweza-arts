import os, time
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

class FLPHandler(FileSystemEventHandler):
    def __init__(self, capture_fn):
        self.capture_fn = capture_fn
        self._last = {}

    def on_modified(self, event):
        if event.is_directory or not event.src_path.endswith('.flp'): return
        now = time.time()
        if now - self._last.get(event.src_path, 0) < 2.0: return
        self._last[event.src_path] = now
        try:    self.capture_fn(event.src_path)
        except Exception as e: print(f'[Watcher] {e}')

_observer = None

def start_watcher(folder, capture_fn):
    global _observer
    os.makedirs(folder, exist_ok=True)
    if _observer and _observer.is_alive(): _observer.stop(); _observer.join()
    _observer = Observer()
    _observer.schedule(FLPHandler(capture_fn), path=folder, recursive=True)
    _observer.start()
    return _observer

def stop_watcher():
    global _observer
    if _observer and _observer.is_alive(): _observer.stop(); _observer.join()
    _observer = None

def is_watching():
    global _observer
    return _observer is not None and _observer.is_alive()
