"""记忆系统：本地 SQLite 持久化 + 可检索（中文友好）。

- 全量对话原文落盘，重启不丢
- 关键词检索：优先用 SQLite FTS5 trigram（中文可用），不支持时自动降级为 LIKE 检索
- short（<3 字）查询走 LIKE，避免 trigram 匹配不到
- 记忆分类：对话、笔记、待办、备忘
- 重要度评分：0-10，自动根据内容和用户反馈
- 记忆关联：相似记忆链接
- 记忆过期：可配置保留时间
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


class Memory:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        # 允许被多个进程（如语音助手 + 控制台 / 外部宿主）同时打开写入，避免 database is locked
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.Error:
            pass
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
                meta TEXT,
                category TEXT DEFAULT '对话',
                importance INTEGER DEFAULT 5,
                related_ids TEXT DEFAULT '[]',
                expires_at INTEGER DEFAULT 0,
                tags TEXT DEFAULT '[]'
            )
            """
        )
        # 添加新字段（如果不存在）
        self._add_column_if_not_exists(cur, "messages", "category", "TEXT DEFAULT '对话'")
        self._add_column_if_not_exists(cur, "messages", "importance", "INTEGER DEFAULT 5")
        self._add_column_if_not_exists(cur, "messages", "related_ids", "TEXT DEFAULT '[]'")
        self._add_column_if_not_exists(cur, "messages", "expires_at", "INTEGER DEFAULT 0")
        self._add_column_if_not_exists(cur, "messages", "tags", "TEXT DEFAULT '[]'")

        cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_category ON messages(category)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_importance ON messages(importance)")
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

    def _add_column_if_not_exists(self, cur, table: str, column: str, type_def: str) -> None:
        """如果列不存在则添加。"""
        try:
            cur.execute(f"SELECT {column} FROM {table} LIMIT 1")
        except sqlite3.OperationalError:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_def}")

    def add(self, session_id: str, role: str, content: str, meta: str | None = None,
            category: str = "对话", importance: int = 5, related_ids: list[int] | None = None,
            expires_at: int = 0, tags: list[str] | None = None) -> int:
        """添加记忆条目。

        Args:
            session_id: 会话ID
            role: 角色（user/assistant/system）
            content: 内容
            meta: 元数据JSON
            category: 分类（对话/笔记/待办/备忘）
            importance: 重要度（0-10，默认5）
            related_ids: 关联记忆ID列表
            expires_at: 过期时间戳（0表示永不过期）
            tags: 标签列表
        """
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO messages(session_id, role, content, ts, meta, category, importance, related_ids, expires_at, tags) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (session_id, role, content, int(time.time()), meta, category,
             max(0, min(10, importance)), json.dumps(related_ids or []), expires_at,
             json.dumps(tags or [])),
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

    def stats(self) -> dict:
        """获取记忆统计信息。"""
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) as total FROM messages")
        total = cur.fetchone()["total"]
        cur.execute("SELECT category, COUNT(*) as count FROM messages GROUP BY category")
        categories = {row["category"]: row["count"] for row in cur.fetchall()}
        cur.execute("SELECT AVG(importance) as avg_importance FROM messages")
        avg_importance = cur.fetchone()["avg_importance"] or 0
        cur.execute("SELECT MIN(ts) as earliest, MAX(ts) as latest FROM messages")
        row = cur.fetchone()
        earliest = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["earliest"])) if row["earliest"] else None
        latest = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["latest"])) if row["latest"] else None
        return {
            "total": total,
            "categories": categories,
            "avg_importance": round(avg_importance, 2),
            "earliest": earliest,
            "latest": latest,
        }

    def get_by_category(self, category: str, limit: int = 20) -> list[dict]:
        """按分类获取记忆。"""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, role, content, ts, session_id, importance, tags FROM messages "
            "WHERE category=? ORDER BY ts DESC LIMIT ?",
            (category, limit),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_important(self, min_importance: int = 8, limit: int = 20) -> list[dict]:
        """获取重要记忆。"""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, role, content, ts, session_id, category, importance FROM messages "
            "WHERE importance>=? ORDER BY importance DESC, ts DESC LIMIT ?",
            (min_importance, limit),
        )
        return [dict(r) for r in cur.fetchall()]

    def update_importance(self, message_id: int, importance: int) -> None:
        """更新记忆重要度。"""
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE messages SET importance=? WHERE id=?",
            (max(0, min(10, importance)), message_id),
        )
        self.conn.commit()

    def add_relation(self, message_id: int, related_id: int) -> None:
        """添加记忆关联。"""
        cur = self.conn.cursor()
        cur.execute("SELECT related_ids FROM messages WHERE id=?", (message_id,))
        row = cur.fetchone()
        if not row:
            return
        related = json.loads(row["related_ids"] or "[]")
        if related_id not in related:
            related.append(related_id)
            cur.execute(
                "UPDATE messages SET related_ids=? WHERE id=?",
                (json.dumps(related), message_id),
            )
            self.conn.commit()

    def cleanup_expired(self) -> int:
        """清理过期记忆，返回清理数量。"""
        now = int(time.time())
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) as c FROM messages WHERE expires_at>0 AND expires_at<?", (now,))
        count = cur.fetchone()["c"]
        if count > 0:
            cur.execute("DELETE FROM messages WHERE expires_at>0 AND expires_at<?", (now,))
            self.conn.commit()
        return count

    def add_tag(self, message_id: int, tag: str) -> None:
        """添加标签。"""
        cur = self.conn.cursor()
        cur.execute("SELECT tags FROM messages WHERE id=?", (message_id,))
        row = cur.fetchone()
        if not row:
            return
        tags = json.loads(row["tags"] or "[]")
        if tag not in tags:
            tags.append(tag)
            cur.execute(
                "UPDATE messages SET tags=? WHERE id=?",
                (json.dumps(tags), message_id),
            )
            self.conn.commit()

    def search_by_tag(self, tag: str, limit: int = 20) -> list[dict]:
        """按标签搜索记忆。"""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, role, content, ts, session_id, category, importance, tags FROM messages "
            "WHERE tags LIKE ? ORDER BY ts DESC LIMIT ?",
            (f'%"{tag}"%', limit),
        )
        return [dict(r) for r in cur.fetchall()]

    # ---------- 记忆管理（控制台「记忆」页用） ----------
    _DEFAULT_COLS = "id, session_id, role, content, ts, category, importance, tags"

    def _where(self, keyword: str, category: str, min_importance: int,
               cols: str | None = None) -> tuple[str, list]:
        sql = (f"SELECT {cols or self._DEFAULT_COLS} FROM messages "
               "WHERE importance >= ?")
        params: list = [int(min_importance or 0)]
        cat = (category or "").strip()
        if cat and cat != "全部":
            sql += " AND category = ?"
            params.append(cat)
        kw = (keyword or "").strip()
        if kw:
            sql += " AND content LIKE ?"
            params.append(f"%{kw}%")
        return sql, params

    def categories(self) -> list[str]:
        """已有的分类清单（给筛选下拉用）。"""
        cur = self.conn.cursor()
        cur.execute("SELECT DISTINCT category FROM messages "
                    "WHERE category IS NOT NULL AND category <> '' ORDER BY category")
        return [str(r["category"]) for r in cur.fetchall()]

    def query(self, keyword: str = "", category: str = "", min_importance: int = 0,
              offset: int = 0, limit: int = 50) -> list[dict]:
        """按关键词 / 分类 / 最低重要度翻页查询（附带分类、重要度、标签）。"""
        sql, params = self._where(keyword, category, min_importance)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params += [int(limit), int(offset)]
        cur = self.conn.cursor()
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def count_query(self, keyword: str = "", category: str = "",
                    min_importance: int = 0) -> int:
        """符合条件的总条数（分页用）。"""
        sql, params = self._where(keyword, category, min_importance, cols="COUNT(*) AS c")
        cur = self.conn.cursor()
        cur.execute(sql, params)
        return int(cur.fetchone()["c"])

    def delete(self, message_id: int) -> bool:
        """删除单条记忆（同时清理 FTS 索引）。"""
        mid = int(message_id)
        cur = self.conn.cursor()
        cur.execute("SELECT content FROM messages WHERE id=?", (mid,))
        row = cur.fetchone()
        if not row:
            return False
        content = row["content"] or ""
        cur.execute("DELETE FROM messages WHERE id=?", (mid,))
        if self.fts_ok:
            try:
                # 外部内容表的正确删除姿势：必须带上原始内容
                cur.execute("INSERT INTO messages_fts(messages_fts, rowid, content) "
                            "VALUES('delete', ?, ?)", (mid, content))
            except sqlite3.Error:
                pass
        self.conn.commit()
        return True

    def set_tags(self, message_id: int, tags: list[str]) -> None:
        """整体覆盖某条记忆的标签。"""
        cur = self.conn.cursor()
        cur.execute("UPDATE messages SET tags=? WHERE id=?",
                    (json.dumps(list(tags or []), ensure_ascii=False), int(message_id)))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
