"""情绪模型（程序化）· 对齐小米 MiMo-V2.5-TTS 官方方案。

## 官方的情绪方案是什么（摘自 mimo.mi.com 语音合成文档）
- 风格指令放在 `messages` 里 **role=user** 的 content；**要念的文本放 role=assistant**。
- 支持**一句话自然语言描述**，也支持更精细的 **导演模式**：用「角色 / 场景 / 指导」三段
  像给演员写剧本一样刻画，模型据此演绎。
- 支持**复合情绪**（"温柔但疲惫"、"带着哽咽的笑意"），而不是只能选单一情绪标签。

→ 所以本模块的职责是：把**数值化、可持久化、会随时间衰减**的情绪状态，
   翻译成符合上述规范的自然语言风格指令。情绪状态本身是"程序化"的（不是写死在人设文本里）。

## 三个维度
| 维度 | 范围 | 含义 |
|---|---|---|
| valence 愉悦度 | -1 ~ 1 | 越正越开心，越负越低落 |
| arousal 唤醒度 | 0 ~ 1 | 越高越激动/亢奋，越低越慵懒/疲惫 |
| intimacy 亲密度 | 0 ~ 1 | 随互动缓慢增长，影响说话的分寸与随性程度 |
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

# 默认基准（没有外部刺激时会慢慢回到这里，像人的情绪自然平复）
DEFAULT_BASELINE = {"valence": 0.25, "arousal": 0.45, "intimacy": 0.30}

# 关键词兜底词典（LLM 不可用时用；只做粗判，不做精细情绪识别）
_POS_WORDS = ("开心", "高兴", "哈哈", "嘿嘿", "谢谢", "谢谢你", "喜欢", "太好了", "不错",
              "棒", "赞", "顺利", "成功", "舒服", "放松", "满足", "期待", "惊喜", "爱你")
_NEG_WORDS = ("累", "疲惫", "困", "难受", "难过", "烦", "压力", "焦虑", "担心", "害怕",
              "生气", "郁闷", "委屈", "疼", "生病", "加班", "失败", "糟糕", "崩溃", "孤单")
_HIGH_AROUSAL = ("急", "赶紧", "快点", "激动", "兴奋", "紧张", "马上", "来不及", "气死")
_LOW_AROUSAL = ("累", "困", "睡", "懒", "慢慢", "休息", "安静", "轻点", "没力气", "熬夜")

# 大脑有时会把"动作词"当成情绪词返回（如"安慰"），这些要被过滤掉
_ACTION_WORDS = ("安慰", "建议", "回答", "解释", "帮助", "询问", "回应", "倾听",
                 "关心", "陪伴", "共情", "支持", "提醒", "劝导", "分析")

# 主情绪标签：由 (valence, arousal) 落在哪个格子里决定
_LABEL_TABLE = (
    # (valence 下限, arousal 下限, 标签)
    (0.45, 0.65, "兴奋"),
    (0.45, 0.30, "温柔"),
    (0.45, 0.00, "恬静"),
    (0.10, 0.65, "愉快"),
    (0.10, 0.30, "平和"),
    (0.10, 0.00, "安静"),
    (-0.25, 0.65, "焦虑"),
    (-0.25, 0.30, "平静"),
    (-0.25, 0.00, "倦怠"),
    (-0.60, 0.65, "烦躁"),
    (-0.60, 0.30, "低落"),
    (-0.60, 0.00, "怅然"),
)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(x)))


def describe(valence: float, arousal: float) -> tuple[str, str]:
    """由两个维度推出 (主情绪标签, 复合情绪短语)。

    复合情绪对应官方说的"多情绪混合"——例如 温柔(高愉悦低唤醒) + 疲惫(极低唤醒)。
    """
    label = "平静"
    for v_lo, a_lo, name in _LABEL_TABLE:
        if valence >= v_lo and arousal >= a_lo:
            label = name
            break

    compound = label
    if valence >= 0.30 and arousal <= 0.28:
        compound = "温柔但有些疲惫"
    elif valence >= 0.45 and arousal >= 0.70:
        compound = "压不住的兴奋"
    elif valence <= -0.40 and arousal >= 0.65:
        compound = "克制着的烦躁"
    elif valence <= -0.40 and arousal <= 0.30:
        compound = "安静的失落"
    elif valence >= 0.10 and arousal <= 0.20:
        compound = "慵懒放松"
    return label, compound


@dataclass
class EmotionState:
    valence: float = DEFAULT_BASELINE["valence"]
    arousal: float = DEFAULT_BASELINE["arousal"]
    intimacy: float = DEFAULT_BASELINE["intimacy"]
    label: str = "平和"
    compound: str = "平和"
    turns: int = 0
    updated_at: int = 0
    reason: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


class EmotionModel:
    """程序化情绪状态机：读/写 SQLite + 每轮更新 + 生成 MiMo 风格指令。"""

    def __init__(self, cfg: dict, db_path: str | None = None):
        self.db_path = str(db_path or (cfg.get("memory", {}) or {}).get("db_path")
                          or "data/memory.db")
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()  # 保护跨线程写库（后台异步推断 + 主线程读取）
        self._read_cfg(cfg)
        self.state = EmotionState(**self.baseline)
        if self.enabled:
            self._init_schema()
            self.load()

    def _read_cfg(self, cfg: dict) -> None:
        """把「配置驱动的属性」从 cfg 里读出来（构造与热重载共用同一处，避免两套默认值）。"""
        self.cfg = cfg or {}
        ecfg = dict(self.cfg.get("emotion", {}) or {})
        self.enabled = bool(ecfg.get("enabled", True))
        self.style_mode = str(ecfg.get("style_mode", "director") or "director").lower()
        self.infer_with_llm = bool(ecfg.get("infer_with_llm", True))
        # 是否把"此刻的心情"作为一段状态行注入大模型上下文（管"说什么"，
        # 与 style_instruction 管"怎么念"是两件事）
        self.inject_to_context = bool(ecfg.get("inject_to_context", True))
        # 回复生成前先用关键词垫一步（让本轮语气就跟上来）；不喜欢这种"抢跑"可以关掉
        self.pre_hint = bool(ecfg.get("pre_hint", True))
        self.decay_per_hour = float(ecfg.get("decay_per_hour", 0.12))
        self.intimacy_gain = float(ecfg.get("max_intimacy_gain_per_turn", 0.01))
        base = dict(DEFAULT_BASELINE)
        base.update({k: float(v) for k, v in (ecfg.get("baseline") or {}).items()
                     if k in base})
        self.baseline = base

    def apply_config(self, cfg: dict) -> None:
        """热重载配置：只刷新"配置驱动的属性"，**保留当前情绪状态**（D1 用）。

        注意不要重建实例——亲密度是长期聊出来的，重建（或 reset）会把它抹掉。
        """
        was_enabled = self.enabled
        self.cfg = cfg or {}
        self._read_cfg(cfg)
        if self.enabled and not was_enabled:
            # 从"关"变"开"：库表可能还没建、状态也没读过
            self._init_schema()
            self.load()

    # ------------------------------------------------------------------ 存储
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            # check_same_thread=False：允许后台线程复用主线程创建的连接
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA busy_timeout=5000")
            except sqlite3.Error:
                pass
        return self._conn

    def _init_schema(self) -> None:
        cur = self._db().cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS emotion_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                valence REAL NOT NULL, arousal REAL NOT NULL, intimacy REAL NOT NULL,
                label TEXT, compound TEXT, turns INTEGER DEFAULT 0,
                updated_at INTEGER DEFAULT 0, reason TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS emotion_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                valence REAL, arousal REAL, intimacy REAL,
                label TEXT, compound TEXT, note TEXT
            )
            """
        )
        self._db().commit()

    def load(self) -> EmotionState:
        cur = self._db().cursor()
        cur.execute("SELECT * FROM emotion_state WHERE id=1")
        row = cur.fetchone()
        if row:
            self.state = EmotionState(
                valence=row["valence"], arousal=row["arousal"], intimacy=row["intimacy"],
                label=row["label"] or "平和", compound=row["compound"] or "平和",
                turns=int(row["turns"] or 0), updated_at=int(row["updated_at"] or 0),
                reason=row["reason"] or "",
            )
        self._decay_to_now()
        return self.state

    def save(self) -> None:
        if not self.enabled:
            return
        s = self.state
        with self._lock:
            cur = self._db().cursor()
            cur.execute(
                "INSERT INTO emotion_state(id, valence, arousal, intimacy, label, compound,"
                " turns, updated_at, reason) VALUES (1,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET valence=excluded.valence, arousal=excluded.arousal,"
                " intimacy=excluded.intimacy, label=excluded.label, compound=excluded.compound,"
                " turns=excluded.turns, updated_at=excluded.updated_at, reason=excluded.reason",
                (s.valence, s.arousal, s.intimacy, s.label, s.compound,
                 s.turns, s.updated_at, s.reason),
            )
            self._db().commit()

    def _log(self, note: str = "") -> None:
        if not self.enabled:
            return
        s = self.state
        with self._lock:
            cur = self._db().cursor()
            cur.execute(
                "INSERT INTO emotion_log(ts, valence, arousal, intimacy, label, compound, note)"
                " VALUES (?,?,?,?,?,?,?)",
                (int(time.time()), s.valence, s.arousal, s.intimacy, s.label, s.compound, note[:200]),
            )
            self._db().commit()

    def history(self, limit: int = 60) -> list[dict]:
        if not self.enabled:
            return []
        cur = self._db().cursor()
        cur.execute("SELECT ts, valence, arousal, intimacy, label, compound FROM emotion_log"
                    " ORDER BY id DESC LIMIT ?", (int(limit),))
        rows = [dict(r) for r in cur.fetchall()]
        rows.reverse()
        return rows

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None

    # ------------------------------------------------------------------ 演化
    def _decay_to_now(self) -> None:
        """按流逝时间把情绪拉回基准（模拟情绪自然平复）；长期不看也能恢复。"""
        s = self.state
        now = int(time.time())
        if not s.updated_at:
            s.updated_at = now
            return
        hours = max(0.0, (now - s.updated_at) / 3600.0)
        if hours <= 0:
            return
        keep = max(0.0, min(1.0, (1.0 - self.decay_per_hour) ** hours))
        for k in ("valence", "arousal", "intimacy"):
            old = getattr(s, k)
            setattr(s, k, self.baseline[k] + (old - self.baseline[k]) * keep)

    def _lexicon_infer(self, text: str) -> dict:
        """关键词兜底：返回"该有的目标情绪值"。"""
        t = text or ""
        pos = sum(1 for w in _POS_WORDS if w in t)
        neg = sum(1 for w in _NEG_WORDS if w in t)
        hi = sum(1 for w in _HIGH_AROUSAL if w in t)
        lo = sum(1 for w in _LOW_AROUSAL if w in t)
        dv = _clamp((pos - neg) * 0.14, -0.40, 0.40)
        da = _clamp((hi - lo) * 0.15, -0.35, 0.35)
        s = self.state
        target_v = _clamp(s.valence + dv, -1.0, 1.0)
        target_a = _clamp(s.arousal + da, 0.0, 1.0)
        label, _ = describe(target_v, target_a)
        return {"valence": target_v, "arousal": target_a, "label": label,
                "reason": f"关键词兜底(pos={pos},neg={neg})" if (pos or neg or hi or lo) else "无明显情绪线索"}

    def _llm_infer(self, user_text: str, reply_text: str = "") -> dict | None:
        """用 MiMo 大脑推断"此刻流萤应有的情绪"，要求严格 JSON 输出。"""
        try:
            from . import llm as llm_mod
        except Exception:  # noqa: BLE001
            return None
        prompt = (
            "你是情绪分析器。下面是一段陪伴对话，请判断：听到用户这句话后，"
            "陪伴助手「流萤」**自己的心情**应该变成什么样。\n"
            "共情规则（非常重要）：\n"
            "1. 用户低落/难过/疲惫/受挫时，流萤也会跟着低落一些（valence 偏负），"
            "表现出心疼和陪着的感觉；**不要反过来变得乐观、兴奋或愉悦**。\n"
            "2. 用户开心时，流萤跟着愉悦（valence 偏正）。\n"
            "3. arousal 是「激动/亢奋程度」：用户在倾诉烦恼时应当偏低、沉稳，而不是高亢。\n"
            "只输出一个 JSON 对象，不要解释、不要代码块、不要多余文字。字段：\n"
            '{"valence": -1到1的数, "arousal": 0到1的数, '
            '"label": "2~4个字的情绪词，例如 心疼/低落/温柔/开心/担心/疲惫/自责；'
            '不要写「安慰」「建议」「回应」这类动作词", "reason": "不超过20字的原因"}\n\n'
            f"用户说：{user_text}\n"
            f"流萤回：{reply_text or '（还没回）'}"
        )
        try:
            brain = llm_mod.make_llm(self.cfg, system_prompt="你是情绪分析器，只输出 JSON。")
            brain.temperature = 0.2
            out = brain.chat([{"role": "user", "content": prompt}])
            m = re.search(r"\{.*\}", out, re.DOTALL)
            if not m:
                return None
            data = json.loads(m.group(0))
            raw_label = str(data.get("label", "") or "").strip()
            # 过滤掉"安慰/建议"这类动作词，长度也要像情绪词；不合格就交给数值推导
            if not (2 <= len(raw_label) <= 4) or any(w in raw_label for w in _ACTION_WORDS):
                raw_label = ""
            return {
                "valence": _clamp(data.get("valence", self.state.valence), -1.0, 1.0),
                "arousal": _clamp(data.get("arousal", self.state.arousal), 0.0, 1.0),
                "label": raw_label or None,
                "reason": f"大脑推断：{str(data.get('reason', ''))[:40]}",
            }
        except Exception:  # noqa: BLE001
            return None

    def update_from_turn(self, user_text: str, reply_text: str = "",
                         use_llm: bool | None = None) -> EmotionState:
        """一轮对话结束后更新情绪：推断目标值 → 平滑混合 → 亲密度微增 → 持久化。"""
        if not self.enabled:
            return self.state
        use_llm = self.infer_with_llm if use_llm is None else use_llm
        self._decay_to_now()

        guess = None
        if use_llm:
            guess = self._llm_infer(user_text, reply_text)
        if guess is None:
            guess = self._lexicon_infer(user_text)

        alpha = 0.45  # 平滑系数：既跟随线索，又不会一秒变脸
        s = self.state
        s.valence = _clamp(s.valence * (1 - alpha) + guess["valence"] * alpha, -1.0, 1.0)
        s.arousal = _clamp(s.arousal * (1 - alpha) + guess["arousal"] * alpha, 0.0, 1.0)
        # 亲密度：每轮缓慢增长，聊得越久越亲近（有上限）
        warm = 1.6 if any(w in (user_text or "") for w in ("谢谢", "喜欢", "爱你", "陪我")) else 1.0
        s.intimacy = _clamp(s.intimacy + self.intimacy_gain * warm, 0.0, 1.0)

        num_label, num_compound = describe(s.valence, s.arousal)
        s.label, s.compound = num_label, num_compound
        if guess.get("label"):
            # 大脑给的词更贴切时优先用；若数值上本来没形成"复合情绪"，连描述一起换掉
            s.label = guess["label"]
            if num_compound == num_label:
                s.compound = guess["label"]
        s.turns += 1
        s.updated_at = int(time.time())
        s.reason = str(guess.get("reason", ""))[:80]
        self.save()
        self._log(note=f"用户：{(user_text or '')[:60]}")
        return s

    def update_from_turn_async(self, user_text: str, reply_text: str = "") -> None:
        """一轮结束后在后台线程更新情绪，不阻塞主回复与播报（每轮省一次完整 LLM 往返）。

        推断失败/写库失败都被吞掉并记日志，绝不影响对话主流程。
        """
        if not self.enabled:
            return

        def _job() -> None:
            try:
                self.update_from_turn(user_text, reply_text)
            except Exception as exc:  # noqa: BLE001
                import logging
                logging.getLogger("firefly").warning("情绪后台更新失败：%s", exc)

        threading.Thread(target=_job, daemon=True, name="firefly-emotion").start()

    def pre_turn_hint(self, user_text: str) -> EmotionState:
        """回复生成**之前**的轻量预判：只用关键词词典，零成本、不调大模型。

        解决 D6「情绪慢一拍」：用户说"我今天特别累"，**第一句**回应就该是低沉的，
        而不是等这一轮播完、后台 LLM 推断完、下一句才变。
        轮末仍会照常跑完整推断（甚至用大模型精修），本方法只是"先垫一步"。
        """
        if not self.enabled or not self.pre_hint:
            return self.state
        guess = self._lexicon_infer(user_text)
        s = self.state
        self._decay_to_now()
        alpha = 0.35  # 比轮末的 0.45 轻：毕竟只是关键词粗判，别抢跑太狠
        s.valence = _clamp(s.valence * (1 - alpha) + guess["valence"] * alpha, -1.0, 1.0)
        s.arousal = _clamp(s.arousal * (1 - alpha) + guess["arousal"] * alpha, 0.0, 1.0)
        s.label, s.compound = describe(s.valence, s.arousal)
        s.updated_at = int(time.time())
        s.reason = str(guess.get("reason", ""))[:80]
        # 不写 emotion_log、不加 turns：这只是本轮的前置垫步，轮末那次才是正式记录
        self.save()
        return s

    def reset(self, note: str = "手动重置到基准") -> EmotionState:
        s = self.state
        s.valence = self.baseline["valence"]
        s.arousal = self.baseline["arousal"]
        s.intimacy = self.baseline["intimacy"]
        s.label, s.compound = describe(s.valence, s.arousal)
        s.updated_at = int(time.time())
        s.reason = note
        self.save()
        self._log(note=note)
        return s

    def nudge(self, valence: float = 0.0, arousal: float = 0.0, intimacy: float = 0.0) -> EmotionState:
        """手动微调（GUI 用）。"""
        s = self.state
        s.valence = _clamp(s.valence + valence, -1.0, 1.0)
        s.arousal = _clamp(s.arousal + arousal, 0.0, 1.0)
        s.intimacy = _clamp(s.intimacy + intimacy, 0.0, 1.0)
        s.label, s.compound = describe(s.valence, s.arousal)
        s.updated_at = int(time.time())
        s.reason = "手动微调"
        self.save()
        self._log(note="手动微调")
        return s

    # ------------------------------------------------------------------ 风格指令
    def _guidance(self) -> list[str]:
        """把维度翻译成"导演给演员的舞台提示"。"""
        s = self.state
        out: list[str] = []
        if s.arousal >= 0.68:
            out.append("语速偏快、咬字轻快有力")
        elif s.arousal <= 0.30:
            out.append("语速放慢，句与句之间留出自然的停顿，不着急")
        else:
            out.append("语速适中，像平常聊天")

        if s.valence >= 0.45:
            out.append("声音明亮有温度，尾音微微上扬")
        elif s.valence <= -0.40:
            out.append("声音压低放轻，气息偏轻，尾音稍微收着，先接住对方的情绪")
        else:
            out.append("语气平稳自然，不刻意用力")

        if s.arousal <= 0.26:
            out.append("带一点刚醒似的慵懒和气声，但吐字仍然清楚")
        if s.intimacy >= 0.60:
            out.append("可以带点随性的小语气，像对很熟的人说话，不必拘谨")
        if s.label and s.label not in ("平和", "平静"):
            out.append(f"整体呈现「{s.compound}」的复合情绪，而不是单一的机械情绪")
        return out

    # ---------------------------------------------------- 注入 LLM 上下文
    def _mood_guidance(self) -> list[str]:
        """把维度翻译成**措辞级**要求（管"说什么"，与 _guidance 的"怎么念"区分）。

        _guidance 是给 TTS 的舞台提示（语速/气息/尾音），大模型看不见也不需要；
        这里要的是"文字该用什么态度说话"，否则情绪只影响语气、不影响措辞 → 人格不一致。
        """
        s = self.state
        out: list[str] = []
        if s.valence <= -0.40:
            out.append("你正跟着对方一起低落：措辞要收敛、轻一点，先接住情绪，"
                       "别急着讲道理或转去轻松话题")
        elif s.valence <= -0.10:
            out.append("你心情略沉：措辞温和克制，不要过分热络")
        elif s.valence >= 0.45:
            out.append("你心情不错：可以自然流露轻快与温度，但别浮夸")
        else:
            out.append("你心态平稳：照常自然说话即可")

        if s.arousal <= 0.30:
            out.append("你有点累/懒洋洋：句子短一些，少用感叹号和排比")
        elif s.arousal >= 0.70:
            out.append("你情绪比较激动：语气可以有起伏，但别失控")

        if s.intimacy >= 0.60:
            out.append("你们已经很熟了：少一点客套，多一点随性")
        elif s.intimacy <= 0.20:
            out.append("你们还不算太熟：保持分寸，别过分亲昵")
        return out

    def context_line(self) -> str:
        """给**大模型**看的「此刻的心情」状态行（不是给 TTS 的演绎指令）。

        约 60~110 字，避免把三维数值原样塞进上下文浪费 token。
        """
        s = self.state
        head = (f"【此刻的心情】你现在是「{s.compound}」"
                f"（愉悦度 {s.valence:+.2f} / 唤醒度 {s.arousal:.2f} / 亲密度 {s.intimacy:.2f}）。")
        body = "；".join(self._mood_guidance())
        tail = "让**文字**的语气与这份心情一致即可，不要向用户描述或复述这些数值。"
        return f"{head}说话时请与这份心情一致：{body}。{tail}"

    def style_brief(self) -> str:
        """一句话自然语言风格指令（官方"直接一句话描述"的用法）。"""
        s = self.state
        return (f"用「{s.compound}」的感觉说这句话："
                f"{'，'.join(self._guidance()[:3])}。")

    def style_director(self, scene: str = "") -> str:
        """导演模式（官方推荐的高表现力写法：角色 / 场景 / 指导）。"""
        s = self.state
        persona_line = (self.cfg.get("_emotion_persona") or
                        "流萤（Firefly），用户的本地语音陪伴助手；温柔真诚、有分寸感，"
                        "像认识很久的老朋友，不说客套话。")
        scene_line = scene or "在日常对话里回应对方。"
        guides = "\n".join(f"- {g}" for g in self._guidance())
        return (f"【角色】{persona_line}\n"
                f"【场景】{scene_line}此刻她的情绪是「{s.compound}」"
                f"（愉悦度 {s.valence:+.2f}，唤醒度 {s.arousal:.2f}，亲密度 {s.intimacy:.2f}）。\n"
                f"【指导】\n{guides}")

    def style_instruction(self, scene: str = "", mode: str | None = None) -> str:
        """给 TTS 的风格指令（放进 role=user），按配置选择 brief 或 director。"""
        mode = (mode or self.style_mode or "director").lower()
        return self.style_brief() if mode == "brief" else self.style_director(scene)

    def snapshot(self) -> dict:
        d = self.state.as_dict()
        d["enabled"] = self.enabled
        d["style_mode"] = self.style_mode
        d["style_preview"] = self.style_instruction()
        d["inject_to_context"] = self.inject_to_context
        d["pre_hint"] = self.pre_hint
        d["context_preview"] = (self.context_line()
                                if (self.enabled and self.inject_to_context) else "")
        d["baseline"] = dict(self.baseline)
        return d


def make_emotion(cfg: dict) -> EmotionModel:
    return EmotionModel(cfg)
