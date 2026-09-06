"""记忆系统：本地 SQLite 持久化 + 可检索（中文友好）。

- 全量对话原文落盘，重启不丢
- 关键词检索：优先用 SQLite FTS5 trigram（中文可用），不支持时自动降级为 LIKE 检索
- short（<3 字）查询走 LIKE，避免 trigram 匹配不到
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path


class Memory:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.fts_ok = False
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                ts INTEGER NOT NULL,
                meta TEXT
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts)")
        self.conn.commit()

        # 尝试建立 FTS5 索引（中文用 trigram 分词）
        for tokenizer in ("trigram", "unicode61"):
            try:
                cur.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts "
                    f"USING fts5(content, content='messages', content_rowid='id', tokenize='{tokenizer}')"
                )
                self.conn.commit()
                self.fts_ok = True
                self.tokenizer = tokenizer
                break
            except sqlite3.Error:
                self.fts_ok = False
        if not self.fts_ok:
            self.tokenizer = "like"

    def add(self, session_id: str, role: str, content: str, meta: str | None = None) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO messages(session_id, role, content, ts, meta) VALUES (?,?,?,?,?)",
            (session_id, role, content, int(time.time()), meta),
        )
        self.conn.commit()
        mid = cur.lastrowid
        if self.fts_ok:
            try:
                cur.execute(
                    "INSERT INTO messages_fts(rowid, content) VALUES (?,?)", (mid, content)
                )
                self.conn.commit()
            except sqlite3.Error:
                pass
        return mid

    def recent(self, session_id: str, limit: int = 20) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, role, content, ts FROM messages WHERE session_id=? "
            "ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]
        rows.reverse()
        return rows

    def search(self, query: str, limit: int = 10) -> list[dict]:
        q = (query or "").strip()
        if not q:
            return []
        cur = self.conn.cursor()
        out: list[dict] = []
        seen: set[int] = set()

        if self.fts_ok and len(q) >= 3:
            try:
                cur.execute(
                    "SELECT m.id, m.role, m.content, m.ts, m.session_id "
                    "FROM messages_fts f JOIN messages m ON m.id = f.rowid "
                    "WHERE messages_fts MATCH ? ORDER BY bm25(messages_fts) LIMIT ?",
                    (q, limit),
                )
                for r in cur.fetchall():
                    if r["id"] not in seen:
                        seen.add(r["id"])
                        out.append(dict(r))
            except sqlite3.Error:
                pass

        if not out:
            cur.execute(
                "SELECT id, role, content, ts, session_id FROM messages "
                "WHERE content LIKE ? ORDER BY id DESC LIMIT ?",
                (f"%{q}%", limit),
            )
            out = [dict(r) for r in cur.fetchall()]
        return out

    def count(self) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM messages")
        return int(cur.fetchone()["c"])

    def build_recall_block(self, query: str, top_k: int = 5) -> str:
        """给大模型用的长期记忆片段。"""
        hits = self.search(query, limit=top_k)
        if not hits:
            return ""
        lines = []
        for h in hits:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(h["ts"]))
            who = "我" if h["role"] == "user" else "Fairy"
            lines.append(f"- [{ts}] {who}：{h['content']}")
        return "\n".join(lines)

    def close(self) -> None:
        self.conn.close()
