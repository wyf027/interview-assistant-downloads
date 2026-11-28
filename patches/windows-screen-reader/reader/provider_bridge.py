"""Exchange completion requests with the desktop app which owns the API settings."""
import concurrent.futures
import json
import queue
import secrets
import sys
import threading


class AppProvider:
    def __init__(self, emit, end, cancel):
        self.emit, self.end, self.cancel = emit, end, cancel
        self.replies = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            for line in sys.stdin.buffer:
                self.replies.put(json.loads(line))
        finally:
            self.end.set()

    def complete(self, data):
        request = secrets.token_hex(8)
        self.emit("provider-request", id=request, messages=data["messages"],
                  maxTokens=data["max_tokens"], json=bool(data.get("response_format")))
        while not self.end.is_set() and not self.cancel.is_set():
            try:
                reply = self.replies.get(timeout=.15)
            except queue.Empty:
                continue
            if reply.get("id") == request:
                if reply.get("error"):
                    raise RuntimeError(reply["error"])
                if reply.get("text", "").strip():
                    return reply["text"].strip()
                raise RuntimeError("API 返回空答案")
        self.emit("provider-cancel", id=request)
        raise concurrent.futures.CancelledError()
