"""Opt-in bounded telemetry writer; no motor access."""
import json
import queue
import threading
import time

active_recorder = None

class Recorder:
    def __init__(self, path):
        self.file = open(path, 'x')
        self.queue = queue.Queue(maxsize=2048)
        self.dropped = 0
        self.error = None
        self.thread = threading.Thread(target=self._write, daemon=True)
        self.thread.start()

    def emit(self, record):
        try:
            self.queue.put_nowait(dict(t=time.monotonic(), **record))
        except queue.Full:
            self.dropped += 1

    def _write(self):
        try:
            while True:
                item = self.queue.get()
                if item is None:
                    break
                self.file.write(json.dumps(item, allow_nan=False)+'\n')
            self.file.write(json.dumps(dict(type='writer_summary', dropped=self.dropped))+'\n')
        except Exception as exc:
            self.error = str(exc)
        finally:
            self.file.close()

    def close(self):
        while self.thread.is_alive():
            try:
                self.queue.put(None, timeout=.1)
                break
            except queue.Full:
                pass
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            self.error = 'writer did not finish within 5 seconds'
