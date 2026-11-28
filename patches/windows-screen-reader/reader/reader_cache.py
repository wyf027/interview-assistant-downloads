"""Local content-addressed cache for generated answers and image transcriptions."""
import hashlib
import json
import sqlite3
import time
from collections import OrderedDict


def fingerprint(*fields):
    raw = json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


class ReaderCache:
    def __init__(self, path):
        self.memory = OrderedDict()
        self.db = None
        try:
            self.db = sqlite3.connect(path, timeout=1)
            with self.db:
                self.db.execute("CREATE TABLE IF NOT EXISTS entries (key TEXT PRIMARY KEY, value TEXT NOT NULL, saved INTEGER NOT NULL)")
                self.db.execute("DELETE FROM entries WHERE saved < ?", (int(time.time())-90*86400,))
        except sqlite3.Error:
            self.close()

    def remember(self, key, value):
        self.memory[key] = value
        self.memory.move_to_end(key)
        if len(self.memory) > 256:
            self.memory.popitem(last=False)

    def get(self, key):
        if key in self.memory:
            return self.memory[key]
        if self.db is not None:
            try:
                row = self.db.execute("SELECT value FROM entries WHERE key=?", (key,)).fetchone()
                if row:
                    self.remember(key, row[0])
                    return row[0]
            except sqlite3.Error:
                pass
        return None

    def get_answer(self, key, question=""):
        value = self.get(key)
        if value in ("待核对：选项解析失败", "待核对：交换选项顺序后答案不一致"):
            return None
        if value and value.endswith("〔模型作答〕"):
            return None  # Superseded format from the initial single-pass trial.
        if value and question.startswith(("多选题\n","多项选择题\n")):
            if value.endswith(("〔模型原序作答〕","〔模型两次一致〕")) and "、" not in value.split("　〔",1)[0].split(" · ",1)[0]:
                return None
        return value

    def put(self, key, value):
        self.remember(key, value)
        if self.db is not None:
            try:
                with self.db:
                    self.db.execute("INSERT OR REPLACE INTO entries VALUES (?,?,?)", (key,value,int(time.time())))
                    self.db.execute("DELETE FROM entries WHERE key IN (SELECT key FROM entries ORDER BY saved DESC LIMIT -1 OFFSET 3000)")
            except sqlite3.Error:
                pass

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
