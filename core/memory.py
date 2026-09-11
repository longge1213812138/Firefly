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
import re
import sqlite3
import time
from pathlib import Path

# 分类加分：这几类是"用户主动存下来的"，信号价值高于闲聊，召回时略微优先
_CATEGORY_BONUS = {"笔记": 1.2, "待办": 1.2, "备忘": 1.0}

# 长问句拆词用的停用词（先按长度倒序替换，避免"怎么样"被"怎么"吃成半个字）
_QUERY_STOPWORDS = (
    "还记得", "记不记得", "是不是", "有没有", "为什么", "怎么样", "怎么办", "什么",
    "怎么", "为啥", "来着", "的话", "我们", "我", "你", "他", "她", "它",
    "吗", "呢", "吧", "的", "了", "呀", "啊", "嘛", "啰", "哦",
)


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

    # ---------- 召回 ----------
    # 带分类/重要度的列（召回重排要用；_DEFAULT_COLS 是记忆页另一套，别混用）
    _RICH_COLS = ("m.id, m.role, m.content, m.ts, m.session_id, "
                  "m.category, m.importance, m.tags")

    _CJK = r"\u4e00-\u9fff"

    def _core_terms(self, query: str, strip_stopwords: bool = True) -> list[str]:
        """从自然语言问句里抠出真正有信息量的 2~3 字片段。

        为什么要这一步（审查报告 D9）：FTS trigram 是**子串**匹配，
        "豆豆怕什么来着？" 这种长问句几乎不可能作为整体命中
        （记忆里存的是"我的猫叫豆豆，很怕打雷"）。
        抠出 "豆豆" 这样的短片段再去 LIKE，命中率就高得多了。
        """
        core = query or ""
        if strip_stopwords:
            for w in sorted(_QUERY_STOPWORDS, key=len, reverse=True):
                core = core.replace(w, " ")
        core = re.sub(rf"[^{self._CJK}A-Za-z0-9]+", " ", core)
        terms: list[str] = []
        for piece in core.split():
            if len(piece) < 2:
                continue
            for size in (3, 2):
                for i in range(len(piece) - size + 1):
                    t = piece[i:i + size]
                    if t not in terms:
                        terms.append(t)
        return terms

    def _search_by_terms(self, cur, query: str, pool: int) -> list[dict]:
        """整句命中不了时的兜底：拆短词逐个 LIKE，取并集。

        先试「去停用词」的核心词（更精准）；核心词被吃得只剩单字时
        （如"我的猫怎么了"→只剩"猫"），再退回原句切片，免得一无所获。
        """
        terms = self._core_terms(query) or self._core_terms(query, strip_stopwords=False)
        out: list[dict] = []
        seen: set[int] = set()
        for term in terms[:8]:
            cur.execute(
                f"SELECT {self._RICH_COLS} FROM messages m "
                "WHERE m.content LIKE ? ORDER BY m.id DESC LIMIT ?",
                (f"%{term}%", pool),
            )
            for r in cur.fetchall():
                d = dict(r)
                if int(d["id"]) not in seen:
                    seen.add(int(d["id"]))
                    out.append(d)
            if len(out) >= pool:
                break
        return out

    def _rerank(self, rows: list[dict], rel: dict[int, float] | None = None) -> list[dict]:
        """按「相关度 + 重要度 + 分类」重排召回结果（审查报告 3.1 / P1-5）。

        此前检索只看关键词命中：记忆页里把重要度调到 10、归类成「待办/笔记」，
        对召回**毫无影响**——功能做了却没接线。这里把管理动作接上。
        """
        for r in rows:
            imp = int(r.get("importance") or 5)
            cat = str(r.get("category") or "对话")
            score = imp * 0.35 + _CATEGORY_BONUS.get(cat, 0.0)
            if rel:
                score += rel.get(int(r["id"]), 0.0)
            r["_score"] = round(score, 3)
        rows.sort(key=lambda r: (-r["_score"], -int(r.get("ts") or 0)))
        return rows

    def search(self, query: str, limit: int = 10, rerank: bool = True) -> list[dict]:
        q = (query or "").strip()
        if not q:
            return []
        cur = self.conn.cursor()
        out: list[dict] = []
        rel: dict[int, float] = {}
        seen: set[int] = set()
        # 候选池放大：留出余量给重排挑，否则"相关度略低但重要度高"的条目挤不进来
        pool = max(int(limit), 1) * 3

        if self.fts_ok and len(q) >= 3:
            try:
                cur.execute(
                    f"SELECT {self._RICH_COLS}, bm25(messages_fts) AS _bm "
                    "FROM messages_fts f JOIN messages m ON m.id = f.rowid "
                    "WHERE messages_fts MATCH ? ORDER BY bm25(messages_fts) LIMIT ?",
                    (q, pool),
                )
                for r in cur.fetchall():
                    d = dict(r)
                    # bm25 越小越相关（SQLite 里是负值），取负变成"越大越相关"
                    rel[int(d["id"])] = -float(d.pop("_bm", 0.0) or 0.0)
                    if int(d["id"]) not in seen:
                        seen.add(int(d["id"]))
                        out.append(d)
            except sqlite3.Error:
                pass

        if not out:
            cur.execute(
                f"SELECT {self._RICH_COLS} FROM messages m "
                "WHERE m.content LIKE ? ORDER BY m.id DESC LIMIT ?",
                (f"%{q}%", pool),
            )
            out = [dict(r) for r in cur.fetchall()]

        if not out:
            out = self._search_by_terms(cur, q, pool)
            rel = {}

        if rerank:
            out = self._rerank(out, rel or None)
        return out[:limit]

    def pinned(self, min_importance: int = 8, limit: int = 5,
               exclude_ids: set[int] | None = None) -> list[dict]:
        """「重要且未过期」的记忆：无论本轮话题命不命中，都固定注入（P1-5）。

        这样记忆页把某条调到重要度 8 以上，它就真的"永远在线"了。
        """
        skip = {int(x) for x in (exclude_ids or ())}
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {self._RICH_COLS} FROM messages m "
            "WHERE m.importance >= ? "
            "AND (m.expires_at IS NULL OR m.expires_at = 0 OR m.expires_at > ?) "
            "ORDER BY m.importance DESC, m.ts DESC LIMIT ?",
            (int(min_importance), int(time.time()), int(limit) + len(skip)),
        )
        rows = [dict(r) for r in cur.fetchall() if int(r["id"]) not in skip]
        return rows[:int(limit)]

    def count(self) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM messages")
        return int(cur.fetchone()["c"])

    def _fmt_hit(self, h: dict) -> str:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(h["ts"]))
        who = "我" if h["role"] == "user" else "Fairy"
        cat = str(h.get("category") or "对话")
        tag = f"（{cat}）" if cat and cat != "对话" else ""
        return f"- [{ts}] {who}{tag}：{h['content']}"

    def build_recall_block(self, query: str, top_k: int = 5, pin: bool = True,
                           pin_min: int = 8, pin_limit: int = 5) -> str:
        """给大模型用的长期记忆片段。

        两部分：
        1. **关键词召回**：与当前这句话相关的往事（按相关度+重要度+分类重排）
        2. **常驻重要记忆**：重要度 >= pin_min 且未过期的条目，无论是否命中都带上
           （上限 pin_limit 条）——让记忆页的「重要度」真正影响注入
        """
        hits = self.search(query, limit=top_k)
        used = {int(h["id"]) for h in hits}
        pinned_rows = (self.pinned(min_importance=pin_min, limit=pin_limit,
                                   exclude_ids=used) if pin else [])

        sections: list[str] = []
        if hits:
            sections.append("\n".join(self._fmt_hit(h) for h in hits))
        if pinned_rows:
            sections.append("【我一直记得的重要事情】")
            sections.append("\n".join(self._fmt_hit(h) for h in pinned_rows))
        return "\n".join(sections)

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

    def count_expired(self) -> int:
        """有多少条已过期（供「清理过期」先报数量再当面确认用）。"""
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM messages "
                    "WHERE expires_at>0 AND expires_at<?", (int(time.time()),))
        return int(cur.fetchone()["c"])

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
