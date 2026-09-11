# 方案 D：三层混合记忆架构 — 详细实施方案

> 流萤 Fairy · AI 记忆功能升级方案
> 2026-09-11

---

## 一、方案概述

### 1.1 目标

使流萤 AI 在聊天交互中能够**跨会话、跨语义地访问和利用记忆内容**，具体包括：

- 用户说"上次讨论的那个方案"时，系统能准确召回相关历史对话
- 长对话中自动压缩上下文，避免 token 浪费
- 近期对话保持原文连贯，远期对话通过语义检索召回
- 所有数据本地存储，零外部依赖，保障隐私

### 1.2 背景与问题

当前流萤的记忆系统基于 `core/memory.py`，使用 SQLite + FTS5 全文检索。核心瓶颈：

| 问题 | 表现 | 影响 |
|------|------|------|
| 纯关键词匹配 | "上次讨论的方案"无法命中 | 语义相关但关键词不同的查询完全失效 |
| 无会话压缩 | 长对话 token 浪费严重 | 成本增加，且超出上下文窗口后丢失信息 |
| 无跨会话语义检索 | 只能搜当前会话或按关键词搜 | 历史对话的价值未被充分利用 |

### 1.3 架构总览

三层记忆从上到下覆盖不同时间粒度：

```
┌─────────────────────────────────────────────────────┐
│                  三层记忆架构                         │
│                                                     │
│  ┌─────────────────────────────────────────────┐   │
│  │  Layer 1: 缓冲层（Buffer）                   │   │
│  │  · 最近 8 轮对话原文                         │   │
│  │  · 始终注入 System Prompt                    │   │
│  │  · 保证对话连贯性（无延迟）                    │   │
│  └─────────────────────────────────────────────┘   │
│                     ↓                              │
│  ┌─────────────────────────────────────────────┐   │
│  │  Layer 2: 摘要层（Summary）                  │   │
│  │  · 当前会话的 LLM 压缩摘要                   │   │
│  │  · 每 10 轮自动触发                          │   │
│  │  · 保留关键决策、偏好、上下文                  │   │
│  └─────────────────────────────────────────────┘   │
│                     ↓                              │
│  ┌─────────────────────────────────────────────┐   │
│  │  Layer 3: 语义层（Semantic）                 │   │
│  │  · 全量历史对话的向量索引                      │   │
│  │  · 用户输入触发语义检索                        │   │
│  │  · 混合检索：向量相似度 + FTS5 + 重要度       │   │
│  │  · 返回 Top-5 相关历史片段                    │   │
│  └─────────────────────────────────────────────┘   │
│                                                     │
└─────────────────────────────────────────────────────┘
```

### 1.4 设计原则

1. **本地优先**：所有数据存储在本地 SQLite，不依赖外部服务
2. **优雅降级**：向量模型不可用时自动退化为关键词模式，功能不中断
3. **渐进实现**：可分两期实施，一期做 Layer 1+2，二期加 Layer 3
4. **零侵入**：与现有 `Memory` 类接口兼容，不破坏已有功能

---

## 二、技术选型与依赖

### 2.1 核心依赖

| 组件 | 选型 | 版本要求 | 用途 | 打包体积 |
|------|------|---------|------|---------|
| 数据库 | SQLite（已有） | 3.35+ | 持久化存储 | 0（已含） |
| 全文检索 | FTS5 trigram（已有） | — | 关键词兜底 | 0（已含） |
| Embedding 模型 | **m3e-base** | — | 中文语义向量化 | ~400MB |
| 推理框架 | ONNX Runtime | ≥1.16 | 本地模型推理 | ~30MB |
| 向量计算 | 纯 Python（兜底） | — | 余弦相似度 | 0 |
| 向量加速 | sqlite-vec（可选） | ≥0.1 | 数据库级向量搜索 | ~5MB |

### 2.2 Embedding 模型选型对比

| 模型 | 维度 | 中文效果 | 本地部署 | 打包体积 | 推荐度 |
|------|------|---------|---------|---------|-------|
| **m3e-base** | 768 | ✅ 专项优化 | ONNX | ~400MB | ⭐⭐⭐ |
| BGE-small-zh | 512 | ✅ | ONNX | ~200MB | ⭐⭐ |
| nomic-embed-text | 768 | ⚠️ 一般 | ONNX | ~300MB | ⭐ |
| text-embedding-3-small | 1536 | ✅ | API（需联网） | ~0MB | ⭐（不适合本地） |

**选择 m3e-base 的理由**：
- 中文语义表征效果在开源模型中领先（C-MTEB 榜单）
- ONNX 格式可直接打包进 exe，无需用户额外安装
- 768 维度在精度和性能之间取得平衡
- 社区活跃，有大量中文场景验证

### 2.3 可选依赖

| 组件 | 说明 | 是否必须 |
|------|------|---------|
| sentence-transformers | m3e-base 的 Python 封装 | 开发期使用，打包时用 ONNX |
| onnxruntime | ONNX 推理引擎 | 打包时必须 |
| sqlite-vec | SQLite 向量搜索扩展 | 可选，提升大规模检索性能 |

### 2.4 Python 包安装

```bash
# 开发环境
pip install sentence-transformers onnxruntime

# 打包环境（仅需 onnxruntime）
pip install onnxruntime
```

---

## 三、具体实施步骤

### 第一期：缓冲层 + 摘要层（1-2 天）

#### Step 1：修改 Fairy.respond() — 集成缓冲层

**目标**：将最近 N 轮对话原文注入 System Prompt，保证对话连贯性。

**改动文件**：`main.py`

**改动内容**：

```python
# 在 Fairy 类中新增方法
def _buffer_context(self, n: int = 8) -> str:
    """Layer 1: 获取最近 N 轮对话原文作为缓冲上下文。"""
    recent = self.memory.recent(self.session_id, limit=n)
    if not recent:
        return ""
    lines = []
    for r in recent:
        who = "用户" if r["role"] == "user" else "Fairy"
        lines.append(f"{who}：{r['content']}")
    return "\n".join(lines)
```

**修改 `_system_prompt` 方法**，增加缓冲层参数：

```python
def _system_prompt(self, recall: str, buffer_ctx: str = "",
                   summary_ctx: str = "") -> str:
    parts = [self.persona, "", actions.DESCRIPTIONS]

    # Layer 1: 缓冲层（最近对话原文）
    if buffer_ctx:
        parts += ["", "【最近的对话记录】", buffer_ctx]

    # Layer 2: 摘要层
    if summary_ctx:
        parts += ["", "【当前会话摘要】", summary_ctx]

    # Layer 3: 语义层（长期记忆）
    if recall:
        parts += ["", "【你记得的与当前话题相关的往事】", recall,
                  "（自然地运用这些记忆，不要生硬地复述，也不要说你查了数据库）"]

    return "\n".join(parts)
```

**修改 `respond` 方法**，组装三层上下文：

```python
def respond(self, user_text: str, auto_confirm: bool = False) -> str:
    self._set_state("thinking")
    try:
        self.memory.add(self.session_id, "user", user_text)
        self.stats.record_message("user")
        self.persona = load_persona(self.cfg)

        # === 三层记忆组装 ===

        # Layer 1: 缓冲层
        buffer_ctx = self._buffer_context(n=8)

        # Layer 2: 摘要层（由 Summarizer 管理）
        summary_ctx = self._get_summary()

        # Layer 3: 语义层（长期记忆召回）
        recall = self.memory.build_recall_block(
            user_text, top_k=int(self.cfg["memory"].get("recall_top_k", 5))
        )

        # 组装 System Prompt
        self.llm.system_prompt = self._system_prompt(
            recall, buffer_ctx, summary_ctx
        )

        # ... 后续对话逻辑不变 ...
```

#### Step 2：实现会话摘要器

**新增文件**：`core/summarizer.py`

```python
"""会话摘要器：每 N 轮自动压缩对话历史，保留关键信息。"""
from __future__ import annotations

import time


class SessionSummarizer:
    """会话级摘要管理器。

    每隔 interval 轮对话，自动用 LLM 将最近的对话压缩为一段摘要。
    摘要保留在内存中，注入 System Prompt 的 Layer 2。
    """

    def __init__(self, llm, interval: int = 10, max_history: int = 20):
        """
        Args:
            llm: LLM 实例（用于生成摘要）
            interval: 每隔多少轮触发一次摘要
            max_history: 摘要时考虑的历史轮数
        """
        self.llm = llm
        self.interval = interval
        self.max_history = max_history
        self.turn_count = 0
        self.summary = ""
        self._last_summary_ts = 0

    def tick(self, messages: list[dict]) -> str:
        """每轮对话后调用。返回当前摘要（可能为空）。"""
        self.turn_count += 1
        if self.turn_count % self.interval == 0 and len(messages) >= 3:
            self.summary = self._do_summarize(messages)
            self._last_summary_ts = time.time()
        return self.summary

    def _do_summarize(self, messages: list[dict]) -> str:
        """调用 LLM 生成对话摘要。"""
        # 取最近 max_history 条消息
        recent = messages[-self.max_history:]
        conversation = "\n".join(
            f"{'用户' if m['role'] == 'user' else 'Fairy'}: {m['content']}"
            for m in recent
        )

        prompt = (
            "请将以下对话压缩为一段简洁的中文摘要（不超过 200 字）。\n"
            "要求：保留关键决策、用户偏好、重要事实；"
            "删除寒暄、重复内容和过程描述。只输出摘要正文，不要开场白。\n\n"
            f"对话内容：\n{conversation}"
        )

        try:
            result = self.llm.chat([{"role": "user", "content": prompt}])
            return (result or "").strip()[:500]  # 安全截断
        except Exception:
            return self.summary  # 失败时保留上一次摘要

    def reset(self) -> None:
        """新会话时重置。"""
        self.turn_count = 0
        self.summary = ""
        self._last_summary_ts = 0
```

**在 `Fairy.__init__` 中初始化**：

```python
from core.summarizer import SessionSummarizer

class Fairy:
    def __init__(self, cfg, ...):
        # ... 现有初始化 ...
        self.summarizer = SessionSummarizer(
            self.llm,
            interval=int(cfg.get("memory", {}).get("summary_interval", 10)),
            max_history=int(cfg.get("memory", {}).get("summary_max_history", 20)),
        )
```

**在 `respond` 中调用摘要器**：

```python
def respond(self, user_text, ...):
    # ... 省略前面 ...
    self.memory.add(self.session_id, "user", user_text)

    # Layer 2: 摘要层 — 每轮检查是否需要更新摘要
    all_messages = self.memory.recent(
        self.session_id, limit=self.summarizer.max_history
    )
    summary_ctx = self.summarizer.tick(all_messages)

    # ... 组装 prompt ...
```

#### Step 3：优化时间衰减和重要度加权

**修改 `build_recall_block`**，增加时间衰减：

```python
def build_recall_block(self, query: str, top_k: int = 5,
                       recency_half_life_days: int = 30) -> str:
    """给大模型用的长期记忆片段（带时间衰减）。"""
    hits = self.search(query, limit=top_k * 2)  # 多召回一些，排序后截断
    if not hits:
        return ""

    import math
    now = time.time()

    def _score(h: dict) -> float:
        """综合得分 = 重要度 * 时间衰减。"""
        importance = h.get("importance", 5) / 10.0  # 归一化到 0-1
        age_days = (now - h["ts"]) / 86400
        decay = 2 ** (-age_days / recency_half_life_days)
        return importance * decay + decay * 0.3  # 时间衰减占 30% 基础权重

    hits.sort(key=_score, reverse=True)
    hits = hits[:top_k]

    lines = []
    for h in hits:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(h["ts"]))
        who = "我" if h["role"] == "user" else "Fairy"
        lines.append(f"- [{ts}] {who}：{h['content']}")
    return "\n".join(lines)
```

---

### 第二期：语义层 — 向量检索（3-5 天）

#### Step 4：扩展 SQLite Schema

**在 `Memory._init_schema` 中新增向量表**：

```python
def _init_vec_tables(self) -> None:
    """初始化向量存储表（Layer 3 语义检索用）。"""
    cur = self.conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vec_memories (
            id INTEGER PRIMARY KEY,
            msg_id INTEGER NOT NULL,
            embedding BLOB,
            model TEXT DEFAULT 'm3e-base',
            created_at INTEGER NOT NULL,
            FOREIGN KEY (msg_id) REFERENCES messages(id)
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_vec_msg ON vec_memories(msg_id)"
    )
    self.conn.commit()
```

#### Step 5：实现本地 Embedding 模型

**新增文件**：`core/embedding.py`

```python
"""本地 Embedding 模型封装（m3e-base ONNX 推理）。"""
from __future__ import annotations

import struct
from pathlib import Path


class LocalEmbedder:
    """本地 Embedding 模型：优先 ONNX Runtime，降级为纯 Python。"""

    def __init__(self, model_dir: str = "models/m3e-base"):
        self.model_dir = Path(model_dir)
        self._session = None
        self._available = False
        self._dimension = 768
        self._init_model()

    def _init_model(self) -> None:
        """尝试加载 ONNX 模型。"""
        try:
            import onnxruntime as ort
            onnx_path = self.model_dir / "model.onnx"
            if onnx_path.exists():
                self._session = ort.InferenceSession(str(onnx_path))
                self._available = True
                return

            # 尝试 sentence-transformers（开发环境）
            from sentence_transformers import SentenceTransformer
            st_path = self.model_dir / "sentence_transformers"
            if st_path.exists():
                self._st_model = SentenceTransformer(str(st_path))
                self._available = True
                self._use_st = True
                return
        except ImportError:
            pass

        print("⚠️ Embedding 模型不可用，语义检索降级为关键词模式")

    def is_available(self) -> bool:
        return self._available

    def dimension(self) -> int:
        return self._dimension

    def embed(self, text: str) -> list[float] | None:
        """将文本转为向量。失败返回 None。"""
        if not self._available:
            return None
        try:
            if hasattr(self, "_use_st"):
                return self._st_model.encode(text).tolist()
            # ONNX 推理（需要 tokenizer，此处简化）
            return self._onnx_embed(text)
        except Exception:
            return None

    def _onnx_embed(self, text: str) -> list[float]:
        """ONNX 推理（需要配合 tokenizer）。"""
        # 实际实现需要 tokenizer 编码 + ONNX 推理
        # 此处为框架代码
        raise NotImplementedError("需要集成 tokenizer")

    @staticmethod
    def serialize(vec: list[float]) -> bytes:
        """向量序列化为二进制（float32 LE）。"""
        return struct.pack(f"<{len(vec)}f", *vec)

    @staticmethod
    def deserialize(data: bytes) -> list[float]:
        """二进制反序列化为向量。"""
        n = len(data) // 4
        return list(struct.unpack(f"<{n}f", data))

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        """纯 Python 余弦相似度。"""
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
```

#### Step 6：在 Memory 中集成向量检索

**在 `core/memory.py` 中新增方法**：

```python
class Memory:
    def __init__(self, db_path: str):
        # ... 现有代码 ...
        self._init_vec_tables()
        self._embedder = None  # 延迟加载

    def _get_embedder(self):
        """延迟加载 Embedding 模型（首次调用时初始化）。"""
        if self._embedder is None:
            from core.embedding import LocalEmbedder
            self._embedder = LocalEmbedder()
        return self._embedder

    def add_with_embedding(self, session_id: str, role: str, content: str,
                           **kwargs) -> int:
        """写入消息 + 同时生成向量嵌入。"""
        msg_id = self.add(session_id, role, content, **kwargs)

        embedder = self._get_embedder()
        if embedder and embedder.is_available():
            embedding = embedder.embed(content)
            if embedding:
                self._store_embedding(msg_id, embedding)

        return msg_id

    def _store_embedding(self, msg_id: int, embedding: list[float]) -> None:
        """存储向量到 vec_memories 表。"""
        from core.embedding import LocalEmbedder
        data = LocalEmbedder.serialize(embedding)
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO vec_memories(msg_id, embedding, model, created_at) "
            "VALUES (?, ?, ?, ?)",
            (msg_id, data, "m3e-base", int(time.time()))
        )
        self.conn.commit()

    def semantic_search(self, query: str, top_k: int = 5) -> list[dict]:
        """语义检索：向量相似度 + FTS5 混合。"""
        embedder = self._get_embedder()
        if not embedder or not embedder.is_available():
            # 降级为纯关键词搜索
            return self.search(query, limit=top_k)

        q_vec = embedder.embed(query)
        if q_vec is None:
            return self.search(query, limit=top_k)

        # 1. 向量搜索
        vec_results = self._vector_search(q_vec, top_k * 2)

        # 2. FTS5 关键词搜索
        fts_results = self.search(query, top_k * 2)

        # 3. 混合融合
        return self._merge_results(vec_results, fts_results, top_k)

    def _vector_search(self, query_vec: list[float],
                       limit: int) -> list[dict]:
        """纯 Python 向量搜索（兜底方案）。"""
        from core.embedding import LocalEmbedder

        cur = self.conn.cursor()
        cur.execute(
            "SELECT v.id, v.msg_id, v.embedding "
            "FROM vec_memories v "
            "JOIN messages m ON m.id = v.msg_id "
            "WHERE v.embedding IS NOT NULL"
        )

        scored = []
        for row in cur.fetchall():
            stored_vec = LocalEmbedder.deserialize(row["embedding"])
            sim = LocalEmbedder.cosine_similarity(query_vec, stored_vec)
            scored.append((row["msg_id"], sim))

        scored.sort(key=lambda x: -x[1])
        results = []
        for msg_id, sim in scored[:limit]:
            cur.execute(
                "SELECT id, role, content, ts, session_id "
                "FROM messages WHERE id=?", (msg_id,)
            )
            r = cur.fetchone()
            if r:
                d = dict(r)
                d["vec_score"] = sim
                results.append(d)
        return results

    def _merge_results(self, vec_results: list[dict],
                       fts_results: list[dict],
                       top_k: int) -> list[dict]:
        """混合融合：向量 70% + FTS5 30% 加权。"""
        scores: dict[int, float] = {}
        best: dict[int, dict] = {}

        # 向量结果权重 0.7
        for i, r in enumerate(vec_results):
            rid = r["id"]
            rank_score = 1.0 - (i / max(len(vec_results), 1))
            scores[rid] = scores.get(rid, 0) + rank_score * 0.7
            best[rid] = r

        # FTS 结果权重 0.3
        for i, r in enumerate(fts_results):
            rid = r["id"]
            rank_score = 1.0 - (i / max(len(fts_results), 1))
            scores[rid] = scores.get(rid, 0) + rank_score * 0.3
            if rid not in best:
                best[rid] = r

        # 按融合得分排序
        sorted_ids = sorted(scores.keys(), key=lambda x: -scores[x])
        return [best[mid] for mid in sorted_ids[:top_k]]
```

#### Step 7：修改对话流程使用语义搜索

**修改 `Fairy.respond` 中的 Layer 3 调用**：

```python
def respond(self, user_text, ...):
    # ... 省略 ...

    # Layer 3: 语义层 — 使用语义搜索替代纯关键词搜索
    recall_hits = self.memory.semantic_search(
        user_text, top_k=int(self.cfg["memory"].get("recall_top_k", 5))
    )
    recall = self._format_recall(recall_hits)

    # ... 组装 prompt ...
```

#### Step 8：添加 Embedding 缓存

**避免重复计算**：相同文本不重新生成向量。

```python
# 在 Memory 类中添加缓存
def __init__(self, db_path):
    # ... 现有代码 ...
    self._embed_cache: dict[str, list[float]] = {}

def _cached_embed(self, text: str) -> list[float] | None:
    """带缓存的向量化。"""
    cache_key = text[:200]  # 截断作为 key
    if cache_key in self._embed_cache:
        return self._embed_cache[cache_key]

    embedder = self._get_embedder()
    if not embedder or not embedder.is_available():
        return None

    vec = embedder.embed(text)
    if vec:
        self._embed_cache[cache_key] = vec
    return vec
```

---

## 四、关键代码示例

### 4.1 完整的上下文组装流程

```python
def respond(self, user_text: str, auto_confirm: bool = False) -> str:
    self._set_state("thinking")
    try:
        # 1. 存储用户消息
        self.memory.add(self.session_id, "user", user_text)
        self.stats.record_message("user")
        self.persona = load_persona(self.cfg)

        # 2. 三层记忆组装
        buffer_ctx = self._buffer_context(n=8)          # Layer 1

        all_msgs = self.memory.recent(self.session_id, limit=20)
        summary_ctx = self.summarizer.tick(all_msgs)     # Layer 2

        recall_hits = self.memory.semantic_search(       # Layer 3
            user_text, top_k=5
        )
        recall = self._format_recall(recall_hits)

        # 3. 组装 System Prompt
        self.llm.system_prompt = self._system_prompt(
            recall, buffer_ctx, summary_ctx
        )

        # 4. 获取历史消息
        history = self.memory.recent(
            self.session_id,
            limit=int(self.cfg["llm"].get("max_history_turns", 20))
        )
        messages = [{"role": h["role"], "content": h["content"]} for h in history]

        # 5. LLM 生成回复
        reply = self.llm.chat(messages)
        text, acts = llm_mod.extract_actions(reply)

        # 6. 执行动作（如有）
        # ... 现有动作执行逻辑 ...

        # 7. 存储助手回复
        self.memory.add(self.session_id, "assistant", text)
        self.stats.record_message("assistant")

        # 8. 情绪演化
        if self.emotion and self.emotion.enabled:
            try:
                self._last_scene = f"用户刚说：『{user_text[:40]}』"
                self.emotion.update_from_turn(user_text, text)
            except Exception:
                pass

        return text
    finally:
        self._set_state("idle")
```

### 4.2 向量序列化与反序列化

```python
import struct

def serialize_vector(vec: list[float]) -> bytes:
    """float32 小端序二进制。"""
    return struct.pack(f"<{len(vec)}f", *vec)

def deserialize_vector(data: bytes) -> list[float]:
    """二进制反序列化。"""
    n = len(data) // 4
    return list(struct.unpack(f"<{n}f", data))
```

### 4.3 混合检索融合算法

```python
def merge_results(vec_results, fts_results, top_k, vec_weight=0.7, fts_weight=0.3):
    """RRF（Reciprocal Rank Fusion）变体融合。"""
    scores = {}
    best = {}

    for rank, r in enumerate(vec_results):
        rid = r["id"]
        # 倒数排名得分
        score = 1.0 / (rank + 60)  # RRF 常数 k=60
        scores[rid] = scores.get(rid, 0) + score * vec_weight
        best[rid] = r

    for rank, r in enumerate(fts_results):
        rid = r["id"]
        score = 1.0 / (rank + 60)
        scores[rid] = scores.get(rid, 0) + score * fts_weight
        if rid not in best:
            best[rid] = r

    sorted_ids = sorted(scores.keys(), key=lambda x: -scores[x])
    return [best[mid] for mid in sorted_ids[:top_k]]
```

---

## 五、所需资源与配置

### 5.1 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `core/memory.py` | **修改** | 新增向量表、语义搜索、混合融合方法 |
| `core/embedding.py` | **新增** | 本地 Embedding 模型封装 |
| `core/summarizer.py` | **新增** | 会话摘要器 |
| `main.py` | **修改** | 集成三层记忆到 `respond()` 流程 |
| `config.example.json` | **修改** | 新增 memory 相关配置项 |
| `build_exe.py` | **修改** | 打包 Embedding 模型文件 |
| `models/m3e-base/` | **新增目录** | ONNX 格式的 m3e-base 模型文件 |

### 5.2 配置项

在 `config.json` 的 `memory` 部分新增：

```json
{
  "memory": {
    "db_path": "data/memory.db",
    "recall_top_k": 5,
    "buffer_size": 8,
    "summary_interval": 10,
    "summary_max_history": 20,
    "embedding": {
      "enabled": true,
      "model_dir": "models/m3e-base",
      "dimension": 768,
      "vec_weight": 0.7,
      "fts_weight": 0.3
    }
  }
}
```

### 5.3 模型文件准备

```bash
# 下载 m3e-base ONNX 模型（约 400MB）
# 方式 1：从 HuggingFace 下载
pip install huggingface_hub
python -c "
from huggingface_hub import snapshot_download
snapshot_download('moka-ai/m3e-base', local_dir='models/m3e-base')
"

# 方式 2：使用 export 脚本转换为 ONNX
python -c "
from sentence_transformers import SentenceTransformer
model = SentenceTransformer('moka-ai/m3e-base')
model.save('models/m3e-base/sentence_transformers')
"
```

### 5.4 打包配置修改

在 `build_exe.py` 的 `common` 列表中添加：

```python
"--add-data", f"{ROOT / 'models'}{SEP}models",
```

### 5.5 磁盘与内存预估

| 指标 | 预估值 | 说明 |
|------|--------|------|
| 模型文件大小 | ~400MB | m3e-base ONNX |
| 单条向量大小 | ~3KB | 768 维 × 4 字节 |
| 10,000 条记忆索引 | ~30MB | SQLite BLOB 存储 |
| 运行时内存占用 | ~500MB | 模型加载后常驻 |
| 单次查询延迟 | 50-200ms | 含向量化 + 相似度计算 |

---

## 六、潜在风险与应对措施

### 6.1 风险矩阵

| 风险 | 概率 | 影响 | 应对措施 |
|------|------|------|---------|
| ONNX Runtime 跨平台兼容性 | 中 | 高 | 提供纯 Python 余弦相似度兜底；CI 多平台测试 |
| m3e-base 模型打包体积过大 | 高 | 中 | 提供"精简版"（不含模型，仅关键词模式）；或用更小的 BGE-small |
| Embedding 推理速度慢 | 低 | 中 | ONNX Runtime 加速；embedding 缓存避免重复计算 |
| 摘要质量不稳定 | 中 | 低 | 摘要失败时保留上一次摘要；摘要内容可编辑 |
| SQLite WAL 模式并发冲突 | 低 | 中 | 已有 busy_timeout=5000 兜底；多进程写入时串行化 |
| 向量索引膨胀 | 低 | 低 | 定期清理过期向量；提供 `cleanup_vec()` 方法 |
| 冷启动（首次无记忆） | 高 | 低 | 三层均为空时正常工作，随使用逐渐积累 |

### 6.2 降级策略

```
Embedding 模型可用？
  ├─ YES → 向量搜索 + FTS5 混合（完整功能）
  └─ NO  → FTS5 关键词搜索 + LIKE 兜底（现有功能）
          └─ FTS5 也不可用？
              └─ 纯 LIKE 模糊匹配（最低保障）
```

**代码实现**：

```python
def semantic_search(self, query: str, top_k: int = 5) -> list[dict]:
    """语义检索，自动降级。"""
    try:
        embedder = self._get_embedder()
        if embedder and embedder.is_available():
            q_vec = embedder.embed(query)
            if q_vec:
                vec_results = self._vector_search(q_vec, top_k * 2)
                fts_results = self.search(query, top_k * 2)
                return self._merge_results(vec_results, fts_results, top_k)
    except Exception as exc:
        print(f"⚠️ 语义检索失败，降级为关键词模式：{exc}")

    # 降级：纯关键词搜索
    return self.search(query, limit=top_k)
```

### 6.3 性能优化策略

1. **Embedding 缓存**：相同文本不重复计算，内存缓存 + LRU 淘汰
2. **增量索引**：新消息写入时才生成向量，历史数据按需补建
3. **批量向量化**：空闲时批量为历史消息生成向量，避免首次查询延迟
4. **SQLite WAL 模式**：已启用，读写不阻塞
5. **模型延迟加载**：首次查询时才加载 Embedding 模型，启动速度不受影响

### 6.4 测试计划

| 测试项 | 方法 | 通过标准 |
|--------|------|---------|
| 缓冲层注入 | 手动对话 10 轮，检查 Prompt | 最近 8 轮原文出现在 Prompt 中 |
| 摘要生成 | 对话 10 轮后检查摘要 | 摘要包含关键信息，不超过 200 字 |
| 向量写入 | 写入消息后查 vec_memories 表 | embedding BLOB 非空，维度 768 |
| 语义搜索 | 搜索同义词/近义词 | 相关记忆被召回，得分 > 0.5 |
| 混合融合 | 对比纯向量/纯 FTS/混合 | 混合结果相关性优于单一方式 |
| 降级测试 | 删除/重命名模型文件 | 自动退化为 FTS5，功能不中断 |
| 性能测试 | 10,000 条记忆下查询 | 延迟 < 200ms |
| 打包测试 | PyInstaller 打包后运行 | 流萤.exe 正常启动，记忆功能可用 |

---

## 附录：与现有系统的兼容性

本方案与现有系统完全兼容：

| 现有接口 | 是否改动 | 说明 |
|----------|---------|------|
| `Memory.add()` | 不变 | 旧接口继续可用 |
| `Memory.search()` | 不变 | FTS5 + LIKE 搜索不变 |
| `Memory.recent()` | 不变 | 按时间取最近消息不变 |
| `Memory.build_recall_block()` | 增强 | 增加时间衰减和重要度加权 |
| `Fairy.respond()` | 增强 | 增加三层上下文组装 |
| `Fairy.say()` | 不变 | 语音播报不变 |
| `gui.py` | 不变 | GUI 控制台不变 |
| `config.json` | 向后兼容 | 新配置项有默认值，旧配置正常工作 |

---

> **文档版本**：v1.0
> **最后更新**：2026-09-11
> **作者**：Fairy 开发助手
