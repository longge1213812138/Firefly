"""Firefly（流萤）本地语音助手 · 主入口

用法（一键脚本已封装，也可命令行）：
  python main.py              语音模式（默认；桌宠随语音模式自动出现）
  python main.py --text       键盘输入模式（不用麦克风，方便调试/没配 Key 时试跑）
  python main.py --devices    查看电脑的麦克风/喇叭设备
  python main.py --search 关键词   检索历史对话
  python main.py --selftest   离线自检（不联网）
  python main.py --pet        只启动桌宠
  python main.py --pi-check   外部 agent（Pi）体检
  python main.py --emotion     查看当前情绪状态与发给 TTS 的风格指令
  python gui.py               图形控制台（对话/记忆/配置/状态）

对话中想让 Pi 帮忙，输入 /pi <任务>（仅在用户明确要求时才调用，且每次都会当面确认）。
"""
from __future__ import annotations

import argparse
import hashlib
import queue
import re
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from core import actions, audio_io, llm as llm_mod, safety  # noqa: E402
from core.asr import make_asr  # noqa: E402
from core.config import config_path, load_config, load_persona  # noqa: E402
from core.emotion import EmotionModel  # noqa: E402
from core.harness import AsyncExecutor, Task, TaskQueue, TaskState, make_harness  # noqa: E402
from core.memory import Memory  # noqa: E402
from core.sentence_buffer import SentenceBuffer  # noqa: E402
from core.stats import UsageStats  # noqa: E402
from core.tts import MiMoTTS, make_tts  # noqa: E402
from core.wake import WakeListener  # noqa: E402


def estimate_tokens(text: str) -> int:
    """粗略估 token 数（只用于界面上给个量级，不追求精确）。

    中日韩字符按「1 字 ≈ 1 token」估，其余字符按「4 字符 ≈ 1 token」估——
    对本地自用的量级判断足够，不做 tokenizer 依赖。
    """
    cjk = 0
    other = 0
    for ch in (text or ""):
        if ("\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff"
                or "\uac00" <= ch <= "\ud7af"):
            cjk += 1
        else:
            other += 1
    return int(cjk + other / 4.0 + 0.5)


class _StreamSpeaker:
    """流式播报器：后台线程「按句合成 + 播放」，带 1 句前瞻以规避 ACTION 行。

    前瞻逻辑：收到第 N+1 句才播第 N 句；流结束时只有确认「无 ACTION」才播最后一句。
    这样「好的，我帮你查一下。」这类开场白在带动作的场景下不会先被念出来。
    检测到用户插话（barge-in）会立刻停止并丢弃剩余句子。
    """

    def __init__(self, firefly: "Firefly"):
        self.firefly = firefly
        self.buf = SentenceBuffer()
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.interrupted = threading.Event()
        self._cancelled = False
        self.thread = threading.Thread(target=self._run, daemon=True, name="firefly-speaker")
        self.thread.start()

    def feed_delta(self, delta: str) -> None:
        for sent in self.buf.feed(delta):
            self.q.put(("sent", sent))

    def finish(self, has_action: bool) -> None:
        for sent in self.buf.flush():
            self.q.put(("sent", sent))
        self.q.put(("done", has_action))

    def cancel(self) -> None:
        self._cancelled = True
        self.q.put(("cancel", None))

    def join(self, timeout: float = 180.0) -> None:
        self.thread.join(timeout=timeout)

    def _speak_one(self, text: str) -> bool:
        """合成并播一句；返回 True 表示被用户插话打断。"""
        f = self.firefly
        try:
            f._set_state("speaking")
            instr = f._tts_instruction()
            ref = f.cfg.get("tts", {}).get("reference_audio_path", "")
            wav = f.tts.synth(text, instr, ref)
            a = f.cfg["audio"]
            tail = float(a.get("output_tail_silence", 0.8))
            if a.get("barge_in", True):
                return audio_io.play_wav_bytes_interruptible(
                    wav,
                    input_device=a.get("input_device"),
                    output_device=a.get("output_device"),
                    tail_silence=tail,
                    mic_threshold=float(a.get("barge_in_threshold", 0.02)),
                )
            audio_io.play_wav_bytes(wav, device=a.get("output_device"), tail_silence=tail)
            return False
        except Exception as exc:  # noqa: BLE001
            print(f"  （语音播报失败：{exc}）", flush=True)
            return False

    def _run(self) -> None:
        prev: str | None = None
        has_action = False
        while True:
            try:
                item = self.q.get(timeout=60)
            except queue.Empty:
                item = ("done", False)
            kind, val = item
            if kind == "cancel":
                return
            if kind == "done":
                has_action = bool(val)
                break
            # kind == "sent"：先把上一句播出去，再记住当前句
            if prev is not None:
                if self._speak_one(prev):
                    self.interrupted.set()
                    return
            prev = val
        # 收尾：没有 ACTION 才播最后一句
        if prev is not None and not has_action and not self._cancelled:
            if self._speak_one(prev):
                self.interrupted.set()


class Firefly:
    def __init__(self, cfg: dict, speak: bool = True, verbose: bool = True,
                 confirm_fn=None, echo: bool = True, emotion=None,
                 config_file: str | None = None):
        self.cfg = cfg
        self.speak = speak
        self.verbose = verbose
        self.echo = echo              # 是否把对话打印到控制台（GUI 模式关掉）
        self.confirm_fn = confirm_fn  # 危险操作确认回调（默认命令行 input；GUI 传弹窗）
        self.on_state = None          # 状态回调：idle/listening/thinking/speaking（桌宠用）
        self.on_config_reload = None  # 配置被热重载时的回调（控制台据此在状态栏提示）
        self.persona = load_persona(cfg)
        self.memory = Memory(cfg["memory"]["db_path"])
        # emotion 可由调用方注入（控制台里情感页与对话**共用同一个实例**，
        # 否则两边各持一份内存状态，手动微调与实时演化会互相看不见）
        self.emotion = (emotion if emotion is not None
                        else EmotionModel(cfg, db_path=cfg["memory"]["db_path"]))
        self._owns_emotion = emotion is None  # 外部注入的实例由注入方负责关闭
        self._last_scene = ""          # 最近一轮的情境描述，给 TTS 导演模式用
        self._context_head: list[tuple[str, str]] = []  # 上一轮 system prompt 的分块
        self.last_context: dict = {}   # 上一轮实际发出去的上下文快照（控制台可视化用）
        self.stats = UsageStats(cfg.get("stats", {}).get("path", "data/stats.json"))
        self.stats.record_session()
        self.llm = llm_mod.make_llm(cfg, system_prompt=self.persona)
        self.asr = make_asr(cfg)
        self.tts = make_tts(cfg)
        self.session_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        # 配置热重载（D1）：记住这次用的是哪个配置文件、内容指纹是什么
        self._config_path = Path(config_file) if config_file else config_path()
        self._cfg_fingerprint = self._read_cfg_fingerprint()
        # Harness：任务编排与执行框架
        self._harness_queue: TaskQueue | None = None
        self._harness_executor: AsyncExecutor | None = None
        self._task_output_buffer: dict[str, list[str]] = {}  # task_id -> output lines
        self._init_harness()

    # ---------- Harness 任务框架 ----------
    def _init_harness(self) -> None:
        """初始化 harness（如果配置启用）。"""
        from core.harness import make_harness
        self._harness_queue, self._harness_executor = make_harness(self.cfg)
        if self._harness_executor is not None:
            # 注册 pi_agent 执行器
            self._harness_executor.register_executor("pi_agent", self._harness_pi_executor)
            # 注册任务状态回调
            if self._harness_queue:
                self._harness_queue.set_on_update(self._on_task_update)
            if self.verbose:
                print("  ✅ Harness 任务框架已启用", flush=True)

    def _harness_pi_executor(self, task: Task, cfg: dict) -> tuple[bool, any, str]:
        """harness 调用的 pi_agent 执行器。"""
        from core import agent_backend

        backend_name = str(cfg.get("pi", {}).get("backend") or "pi")
        backend = agent_backend.get_backend(backend_name, cfg)
        if backend is None:
            return False, None, f"没有注册名为「{backend_name}」的 agent 后端"
        if not backend.available():
            return False, None, f"{backend.describe()} 不可用"

        # 使用 run_with_cancel 支持取消
        res = backend.run_with_cancel(
            task.args.get("task", ""),
            read_only=bool(task.args.get("read_only", cfg.get("pi", {}).get("read_only", False))),
            cancel_event=task._cancel_event,
            on_output_line=lambda line: task.output_lines.append(line),
        )
        if not res.ok:
            return False, None, res.error or "无输出"
        return True, res.text, ""

    def _on_task_update(self, task: Task) -> None:
        """任务状态变更回调。"""
        if not self.cfg.get("harness", {}).get("auto_broadcast", True):
            return
        # 只对终态广播
        if task.state.is_terminal:
            if self.verbose:
                print(f"  📋 {task.summary()}", flush=True)

    def submit_task(self, name: str, args: dict, backend: str = "") -> tuple[bool, str, str]:
        """提交任务到 harness。返回 (成功, 消息, task_id)。"""
        if self._harness_queue is None:
            return False, "Harness 未启用（请在 config.json 开启 harness.enabled）", ""
        task = Task(name=name, args=args, backend=backend)
        ok, msg = self._harness_queue.enqueue(task)
        return ok, msg, task.id if ok else ""

    def get_task_status(self, task_id: str) -> str | None:
        """获取任务状态摘要。"""
        if self._harness_queue is None:
            return None
        task = self._harness_queue.get(task_id)
        return task.summary() if task else None

    def cancel_task(self, task_id: str) -> tuple[bool, str]:
        """取消任务。"""
        if self._harness_queue is None:
            return False, "Harness 未启用"
        return self._harness_queue.cancel(task_id)

    def list_tasks(self, state_filter: TaskState | None = None) -> list[dict]:
        """列出所有任务（可选按状态过滤）。"""
        if self._harness_queue is None:
            return []
        tasks = self._harness_queue.all_tasks()
        if state_filter is not None:
            tasks = [t for t in tasks if t.state == state_filter]
        return [t.to_dict() for t in tasks]

    def get_task_result(self, task_id: str) -> tuple[bool, any, str]:
        """获取任务结果。返回 (存在, 结果, 错误)。"""
        if self._harness_queue is None:
            return False, None, "Harness 未启用"
        task = self._harness_queue.get(task_id)
        if task is None:
            return False, None, f"未找到任务 {task_id}"
        if task.state == TaskState.COMPLETED:
            return True, task.result, ""
        if task.state == TaskState.FAILED:
            return True, None, task.error
        return True, None, f"任务状态：{task.state.display}"

    # ---------- 配置热重载（D1） ----------
    def _read_cfg_fingerprint(self) -> str:
        """config.json 的内容指纹（sha1）。用内容而不是 mtime：
        mtime 在同秒内改写、或文件大小不变时会漏判，内容哈希永远准。"""
        try:
            return hashlib.sha1(self._config_path.read_bytes()).hexdigest()
        except OSError:
            return ""

    def reload_config_if_changed(self, force: bool = False) -> list[str]:
        """每轮对话前探一下 config.json；变了就地热重载，返回"改了什么"的人话列表。

        解决 D1：命令行（--text）与语音模式启动时只读一次配置，
        在控制台改完音色却以为"设置没生效"，非得重启程序才行。
        """
        if not force and self._read_cfg_fingerprint() == self._cfg_fingerprint:
            return []
        try:
            new_cfg = load_config(self._config_path)
        except Exception as exc:  # noqa: BLE001 —— 读坏了就继续用旧的，别把对话搞断
            return [f"新配置读取失败，仍用旧配置（{exc}）"]
        changes = self._apply_config(new_cfg)
        self._cfg_fingerprint = self._read_cfg_fingerprint()
        return changes

    def _apply_config(self, new_cfg: dict) -> list[str]:
        """把新配置应用到运行中的实例：重建 TTS/ASR/大脑、刷新人设与情绪设置。"""
        old_cfg = self.cfg
        changes = self._describe_cfg_changes(old_cfg, new_cfg)
        self.cfg = new_cfg
        self.persona = load_persona(new_cfg)
        # 这三样都是"纯配置派生"的，直接重建最省心（下一轮才用到，不会打断本轮）
        self.llm = llm_mod.make_llm(new_cfg, system_prompt=self.persona)
        self.asr = make_asr(new_cfg)
        self.tts = make_tts(new_cfg)
        # 情绪**不能重建**：亲密度是长期聊出来的，重建会把累积状态抹掉
        if self.emotion is not None:
            try:
                self.emotion.apply_config(new_cfg)
            except Exception:  # noqa: BLE001
                pass
        old_db = (old_cfg.get("memory", {}) or {}).get("db_path")
        new_db = (new_cfg.get("memory", {}) or {}).get("db_path")
        if old_db != new_db:
            try:
                self.memory.close()
            except Exception:  # noqa: BLE001
                pass
            self.memory = Memory(new_db)
            changes.append("记忆库路径已切换")
        # Harness 配置变更
        old_harness = (old_cfg.get("harness", {}) or {}).get("enabled", False)
        new_harness = (new_cfg.get("harness", {}) or {}).get("enabled", False)
        if old_harness != new_harness:
            if self._harness_executor is not None:
                self._harness_executor.stop()
            self._init_harness()
            changes.append("Harness 任务框架" + ("已启用" if new_harness else "已停用"))
        return changes

    # 热重载时要报给用户看的字段（人话名称 -> (配置段, 键)）。
    # 只报用户"能感知到"的，不把 _说明/_字段 这类注释也算作变化。
    _WATCH_FIELDS = (
        ("合成方式", ("tts", "model")),
        ("音色", ("tts", "voice")),
        ("音色描述/风格指令", ("tts", "voice_instruction")),
        ("参考音频", ("tts", "reference_audio_path")),
        ("语音合成地址", ("tts", "base_url")),
        ("大脑模型", ("llm", "model")),
        ("大脑地址", ("llm", "base_url")),
        ("性格随机度", ("llm", "temperature")),
        ("携带对话轮数", ("llm", "max_history_turns")),
        ("情绪开关", ("emotion", "enabled")),
        ("情绪风格模式", ("emotion", "style_mode")),
        ("情绪推断方式", ("emotion", "infer_with_llm")),
        ("情绪注入上下文", ("emotion", "inject_to_context")),
        ("情绪回复前预判", ("emotion", "pre_hint")),
        ("每轮召回往事条数", ("memory", "recall_top_k")),
        ("重要记忆常驻", ("memory", "pin_important")),
        ("常驻记忆门槛", ("memory", "pin_min_importance")),
        ("常驻记忆条数", ("memory", "pin_limit")),
        ("唤醒词", ("wake", "keyword")),
        ("录音静音阈值", ("audio", "silence_threshold")),
        ("说完停顿判定", ("audio", "tail_silence_seconds")),
        ("允许语音打断", ("audio", "barge_in")),
        ("只读模式", ("pi", "read_only")),
    )

    def _describe_cfg_changes(self, old: dict, new: dict) -> list[str]:
        def val(cfg: dict, sec: str, key: str):
            return (cfg.get(sec) or {}).get(key)

        out: list[str] = []
        for label, (sec, key) in self._WATCH_FIELDS:
            before, after = val(old, sec, key), val(new, sec, key)
            if before != after:
                out.append(f"{label} {self._fmt_cfg_val(before)} → {self._fmt_cfg_val(after)}")
        # API Key 只报"换了没换"，绝不回显内容（安全）
        old_key = val(old, "mimo", "api_key") or val(old, "tts", "api_key")
        new_key = val(new, "mimo", "api_key") or val(new, "tts", "api_key")
        if old_key != new_key:
            out.append("API Key 已更新（内容不回显）")
        if (old.get("persona_path") or "") != (new.get("persona_path") or ""):
            out.append("人设文件路径已切换")
        return out

    @staticmethod
    def _fmt_cfg_val(v) -> str:
        if v is None or v == "":
            return "（空）"
        if isinstance(v, bool):
            return "开" if v else "关"
        return str(v)

    def close_emotion(self) -> None:
        """关闭情绪实例——只关自己创建的那份；外部注入的由注入方负责关闭。"""
        if self.emotion is not None and getattr(self, "_owns_emotion", True):
            self.emotion.close()

    def _set_state(self, s: str) -> None:
        if self.on_state:
            try:
                self.on_state(s)
            except Exception:
                pass

    def _say_line(self, text: str) -> None:
        if self.echo:
            print(text, flush=True)

    def _notify_reload(self, notes: list[str]) -> None:
        """把热重载的变更告诉用户：控制台/语音模式打印一行；
        GUI 侧通过 on_config_reload 回调在状态栏显示。
        真的没改任何东西就别刷屏（每轮都会探一次指纹）。
        """
        if not notes:
            return
        msg = "（配置已热重载：" + "；".join(notes) + "）"
        self._say_line(msg)
        cb = getattr(self, "on_config_reload", None)
        if cb:
            try:
                cb(notes)
            except Exception:  # noqa: BLE001
                pass

    def _tts_instruction(self) -> str:
        """拼出 role=user 的自然语言指令（按 MiMo 官方规范，指令放 user、正文放 assistant）。

        - voicedesign 模型：这段是「音色设计描述」
        - 其它模型：这段是「发音风格指令」
        这里把用户在配置里写的音色描述与**实时情绪**合并成一段。
        """
        base = str((self.cfg.get("tts", {}) or {}).get("voice_instruction", "") or "").strip()
        if self.emotion is None or not self.emotion.enabled:
            return base
        try:
            emo = self.emotion.style_instruction(scene=self._last_scene)
        except Exception:  # noqa: BLE001
            return base
        if not base:
            return emo
        return f"{base}。\n此刻的语气与情绪（请务必照此演绎）：\n{emo}"

    # ---------- 核心对话 ----------
    def _pre_emotion(self, user_text: str) -> None:
        """P1-6：回复生成前先做一次关键词预判，让**本轮**语气就跟上来。

        轮末仍会照常跑完整推断（可能用大模型精修），这里只是"先垫一步"，
        解决"用户说很累、第一句回应却还是轻快"的慢一拍问题。
        """
        emo = self.emotion
        if emo is None or not emo.enabled:
            return
        try:
            emo.pre_turn_hint(user_text)
        except Exception:  # noqa: BLE001
            pass

    def _mood_context(self) -> str:
        """把情绪翻译成给大模型看的「措辞级」状态行（P1-4）。

        与 `_tts_instruction()` 的分工：
        - `_tts_instruction()` 管**怎么念**（语速/气息/尾音），发给 TTS
        - 本方法管**说什么**（措辞/态度），拼进 system prompt
        没有这一步的话，情绪只影响语气、不影响措辞 → 人格不一致。
        """
        emo = self.emotion
        if emo is None or not emo.enabled:
            return ""
        if not bool((self.cfg.get("emotion", {}) or {}).get("inject_to_context", True)):
            return ""
        try:
            return emo.context_line() if emo.inject_to_context else ""
        except Exception:  # noqa: BLE001
            return ""

    def _system_prompt(self, recall: str) -> str:
        mood = self._mood_context()
        parts = [self.persona, "", actions.DESCRIPTIONS]
        blocks: list[tuple[str, str]] = [("人设", self.persona),
                                         ("动作说明", actions.DESCRIPTIONS)]
        if recall:
            parts += ["", "【你记得的与当前话题相关的往事】", recall,
                      "（自然地运用这些记忆，不要生硬地复述，也不要说你查了数据库）"]
            blocks.append(("往事召回", recall))
        if mood:
            parts += ["", mood]
            blocks.append(("此刻心情", mood))
        # Harness 任务上下文
        if self._harness_queue is not None:
            from core.harness_middleware import build_task_context
            task_ctx = build_task_context(self._harness_queue)
            if task_ctx:
                parts += ["", task_ctx]
                blocks.append(("任务状况", task_ctx))
        self._context_head = blocks   # 留给「本轮上下文」可视化（P2-8）
        return "\n".join(parts)

    def _prepare_messages(self, user_text: str) -> list[dict]:
        """对话前的公共准备：载人设、预判情绪、召回往事、拼上下文，返回 messages。

        顺序很重要：
        1. **先召回、再把用户这句话入库**——反过来做的话，刚写进去的这句话
           会被自己的检索命中，在上下文的「往事」里重复出现一遍，挤占真实记忆的额度。
        2. **情绪预判要在拼 system prompt 之前**——否则本轮的心情注入的是上一轮的值。
        """
        self.stats.record_message("user")
        # 配置热重载（D1）：改完音色/模型不必重启程序，本轮就用新的
        self._notify_reload(self.reload_config_if_changed())
        # 人设每次重新读取：改 persona 文件立即生效（F-05 热切换）
        self.persona = load_persona(self.cfg)
        self._pre_emotion(user_text)   # P1-6：本轮语气先跟上
        mem_cfg = self.cfg.get("memory", {}) or {}
        recall = self.memory.build_recall_block(
            user_text,
            top_k=int(mem_cfg.get("recall_top_k", 5)),
            pin=bool(mem_cfg.get("pin_important", True)),
            pin_min=int(mem_cfg.get("pin_min_importance", 8)),
            pin_limit=int(mem_cfg.get("pin_limit", 5)),
        )
        self.memory.add(self.session_id, "user", user_text)
        system_prompt = self._system_prompt(recall)
        self.llm.system_prompt = system_prompt
        # 近 N 轮在入库之后取，这样模型能看到用户当前这句话
        history = self.memory.recent(
            self.session_id, limit=int(self.cfg["llm"].get("max_history_turns", 20))
        )
        messages = [{"role": h["role"], "content": h["content"]} for h in history]
        self._snapshot_context(system_prompt, messages)
        return messages

    def _snapshot_context(self, system_prompt: str, messages: list[dict]) -> None:
        """记录本轮实际发出去的东西，供控制台「🔍 本轮上下文」展示（P2-8）。

        这是唯一能让用户**亲眼确认**"情感/记忆到底注没注入"的手段。
        """
        blocks = [{"title": t, "text": x, "chars": len(x)} for t, x in self._context_head]
        hist_tokens = sum(estimate_tokens(m["content"]) for m in messages)
        self.last_context = {
            "blocks": blocks,
            "history": messages,
            "history_turns": len(messages),
            "history_tokens": hist_tokens,
            "history_chars": sum(len(m["content"]) for m in messages),
            "system_chars": len(system_prompt),
            "system_tokens": estimate_tokens(system_prompt),
            "est_tokens": estimate_tokens(system_prompt) + hist_tokens,
            "model": str((self.cfg.get("llm", {}) or {}).get("model", "")),
            "temperature": (self.cfg.get("llm", {}) or {}).get("temperature"),
            "recall_top_k": int((self.cfg.get("memory", {}) or {}).get("recall_top_k", 5)),
            "max_history_turns": int(self.cfg.get("llm", {}).get("max_history_turns", 20)),
            "emotion_injected": any(t == "此刻心情" for t, _ in self._context_head),
            "recall_injected": any(t == "往事召回" for t, _ in self._context_head),
            "memory_total": self.memory.count(),
        }

    def _finalize_action_text(self, messages: list[dict], text: str, acts: list[dict],
                              auto_confirm: bool) -> str:
        """执行动作（含确认）并返回最终应说出的文本；可能触发第二次 LLM 调用。"""
        # 检查是否有 pi_agent 动作且 harness 启用
        pi_acts = [a for a in acts if a.get("name") == "pi_agent"]
        other_acts = [a for a in acts if a.get("name") != "pi_agent"]

        # 如果有 pi_agent 且 harness 启用，路由到 harness
        if pi_acts and self._harness_queue is not None:
            results = []
            for a in pi_acts:
                task_args = a.get("args") or {}
                # 仍然需要确认（硬闸口）
                prompt = safety.format_confirm_list([a])
                if not (self.confirm_fn and self.confirm_fn(prompt)):
                    safety.audit(self.cfg, {"session_id": self.session_id,
                                            "action": "pi_agent", "args": task_args,
                                            "result": "用户拒绝", "risk": "high", "hard": True})
                    results.append("pi_agent 已取消")
                    continue
                # 提交到 harness
                ok, msg, task_id = self.submit_task("pi_agent", task_args)
                if ok:
                    safety.audit(self.cfg, {"session_id": self.session_id,
                                            "action": "pi_agent", "args": task_args,
                                            "result": "已提交异步执行", "risk": "high", "hard": True})
                    results.append(f"pi_agent 已提交异步执行（ID: {task_id}）")
                else:
                    results.append(f"pi_agent 提交失败：{msg}")
            note = f"（操作结果：{'；'.join(results)[:400]}）"
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": f"系统提示：{note} 请用自然语言简短告诉用户结果。"})
            try:
                return self.llm.chat(messages).strip()
            except Exception:
                return (text + " " + note).strip()

        # 其他动作走原有流程
        require_any = any(safety.needs_confirm(self.cfg, a.get("name", ""), a.get("args") or {}) for a in other_acts)
        hard_any = any(safety.is_hard(self.cfg, a.get("name", ""), a.get("args") or {}) for a in other_acts)
        allowed = True
        if require_any:
            prompt = safety.format_confirm_list(other_acts)
            if hard_any:
                allowed = bool(self.confirm_fn and self.confirm_fn(prompt))
            else:
                allowed = True if auto_confirm else bool(self.confirm_fn and self.confirm_fn(prompt))
        if not allowed:
            for a in other_acts:
                safety.audit(self.cfg, {"session_id": self.session_id,
                                        "action": a.get("name", ""), "args": a.get("args") or {},
                                        "result": "用户拒绝", "risk": "high"})
            note = f"（用户在确认清单上取消了全部 {len(other_acts)} 个操作）"
        else:
            results = []
            for a in other_acts:  # 已在清单上确认过 → 逐个执行
                ok, out = actions.execute(self.cfg, a, auto_confirm=True,
                                          session_id=self.session_id,
                                          confirm_fn=self.confirm_fn)
                results.append(f"{a.get('name')} {'成功' if ok else '失败'}：{str(out)[:150]}")
            note = f"（操作结果：{'；'.join(results)[:400]}）"
        messages.append({"role": "assistant", "content": text})
        messages.append({"role": "user", "content": f"系统提示：{note} 请用自然语言简短告诉用户结果。"})
        try:
            return self.llm.chat(messages).strip()
        except Exception:
            return (text + " " + note).strip()

    def _record_turn(self, user_text: str, text: str, acts: list[dict] | None = None) -> None:
        """一轮结束后的收尾：记回复、记动作、后台异步演化情绪。"""
        self.memory.add(self.session_id, "assistant", text)
        self.stats.record_message("assistant")
        for a in (acts or []):
            self.stats.record_action(a.get("name", "unknown"))
        # 情绪在后台线程演化（不阻塞主回复与播报，每轮省一次完整 LLM 往返）
        if self.emotion is not None and self.emotion.enabled:
            try:
                self._last_scene = f"用户刚说：『{user_text[:40]}』，此刻正要回应他。"
                self.emotion.update_from_turn_async(user_text, text)
            except Exception:  # noqa: BLE001
                pass

    def respond(self, user_text: str, auto_confirm: bool = False) -> str:
        """非流式一轮对话（文本 / GUI 路径）：同步返回最终回复文本。"""
        # 检查是否是任务状态查询
        task_reply = self._check_task_query(user_text)
        if task_reply is not None:
            self._record_turn(user_text, task_reply)
            return task_reply

        self._set_state("thinking")
        try:
            messages = self._prepare_messages(user_text)
            reply = self.llm.chat(messages)
            text, acts = llm_mod.extract_actions(reply)
            if acts:
                text = self._finalize_action_text(messages, text, acts, auto_confirm)
            self._record_turn(user_text, text, acts)
            return text
        finally:
            self._set_state("idle")

    def _check_task_query(self, user_text: str) -> str | None:
        """检查是否是任务状态查询，如果是则直接返回结果。"""
        if self._harness_queue is None:
            return None
        from core.harness_middleware import is_task_query, is_cancel_request, format_task_list, format_task_detail

        # 取消请求
        is_cancel, cancel_id = is_cancel_request(user_text)
        if is_cancel:
            if cancel_id:
                ok, msg = self.cancel_task(cancel_id)
                return msg
            else:
                # 取消所有
                tasks = self.list_tasks()
                cancelled = 0
                for t in tasks:
                    if t.get('state') in ('pending', 'running'):
                        self.cancel_task(t['id'])
                        cancelled += 1
                return f"已取消 {cancelled} 个任务" if cancelled else "没有需要取消的任务"

        # 状态查询
        if is_task_query(user_text):
            # 检查是否查询特定任务
            import re
            m = re.search(r"[0-9a-f]{8}", user_text.lower())
            if m:
                task_id = m.group(0)
                status = self.get_task_status(task_id)
                return status if status else f"未找到任务 {task_id}"
            # 列出所有任务
            tasks = self.list_tasks()
            if not tasks:
                return "当前没有任何任务在执行。"
            from core.harness_middleware import format_task_list
            return format_task_list(tasks)

        return None

    def respond_stream(self, user_text: str, auto_confirm: bool = False) -> tuple[str, bool]:
        """语音路径：流式 LLM + 按句合成、抢先播报。返回 (最终文本, 是否被插话打断)。

        与 respond 的区别：LLM 用 stream=True 边生成边切句，后台线程按句合成并播放，
        使「第 N 句在播、第 N+1 句在合成」，首句到耳朵的延迟大幅下降。
        流式失败时自动回退到非流式整段播报。
        """
        self._set_state("thinking")
        try:
            if not self.speak:
                return self.respond(user_text, auto_confirm), False

            messages = self._prepare_messages(user_text)
            self._last_scene = f"用户刚说：『{user_text[:40]}』，此刻正要回应他。"
            speaker = _StreamSpeaker(self)
            full_reply: list[str] = []
            try:
                for delta in self.llm.chat_stream(messages):
                    full_reply.append(delta)
                    speaker.feed_delta(delta)
            except Exception:  # noqa: BLE001 —— 流式失败，回退非流式
                speaker.cancel()
                speaker.join(timeout=5)  # 等后台播报线程退干净，避免残留/抢麦
                reply = self.llm.chat(messages)
                text, acts = llm_mod.extract_actions(reply)
                if acts:
                    text = self._finalize_action_text(messages, text, acts, auto_confirm)
                self._record_turn(user_text, text, acts)
                return text, self.say(text)

            text, acts = llm_mod.extract_actions("".join(full_reply))
            if acts:
                # 带动作：不播流式积压句，改走「动作执行 + 整段播报结果」
                speaker.finish(has_action=True)
                text = self._finalize_action_text(messages, text, acts, auto_confirm)
                self._record_turn(user_text, text, acts)
                return text, self.say(text)

            # 无动作：正常收尾，播最后积压句（已在后台合成/播放前面的句子）
            speaker.finish(has_action=False)
            if self.echo:
                print(f"\n🧚 Firefly：{text.strip()}\n", flush=True)
            speaker.join()
            self._record_turn(user_text, text, acts)
            return text, speaker.interrupted.is_set()
        finally:
            self._set_state("idle")

    # ---------- 显式调用外部 agent（Pi） ----------
    def run_pi_task(self, task: str, use_harness: bool | None = None) -> str:
        """用户明确要求时，把任务交给外部 agent（默认 Pi）。

        长期记忆只在陪伴端：这里只把任务转出去，结果回来后写进**陪伴端自己的**记忆。
        该动作是硬闸口——每次调用都必须当面确认。

        use_harness: None=自动判断（harness 启用就异步），True=强制异步，False=强制同步。
        """
        from core import agent_backend

        task = (task or "").strip()
        if not task:
            return "想让 Pi 做什么？可以这样写：/pi 帮我看看这个项目的结构"

        # 安全闸口：pi.enabled 关闭时直接拒绝，不尝试初始化后端
        if not self.cfg.get("pi", {}).get("enabled", True):
            return "Pi 功能已关闭。请在配置中打开 pi.enabled 后重试。"

        backend_name = str(self.cfg.get("pi", {}).get("backend") or "pi")
        backend = agent_backend.get_backend(backend_name, self.cfg)
        if backend is None:
            return f"没有注册名为「{backend_name}」的 agent 后端。"
        if not backend.available():
            return (f"没找到可用的 {backend_name} 命令行——{backend.describe()}\n"
                    "（请先安装 Pi，或在 config.json 的 pi.cli_path 填绝对路径）")

        # 确认闸口（硬闸口，无法绕过）
        prompt = (f"即将调用「{backend_name}」执行任务：\n  {task}\n"
                  "它会读写文件、执行命令，可能改动你的项目。")
        confirm = self.confirm_fn or safety.confirm_interactive
        self.memory.add(self.session_id, "user", f"/pi {task}")
        if not confirm(prompt):
            safety.audit(self.cfg, {"session_id": self.session_id, "action": "pi_agent",
                                    "args": {"task": task, "backend": backend_name},
                                    "result": "用户拒绝", "risk": "high", "hard": True})
            return "好，那我不调用 Pi 了。"

        # 判断是否使用 harness 异步执行
        _use_harness = use_harness
        if _use_harness is None:
            _use_harness = self._harness_queue is not None

        if _use_harness and self._harness_queue is not None:
            # 异步模式：提交到 harness，立即返回
            ok, msg, task_id = self.submit_task(
                "pi_agent",
                {"task": task, "read_only": bool(self.cfg.get("pi", {}).get("read_only", False))},
                backend=backend_name,
            )
            if ok:
                safety.audit(self.cfg, {"session_id": self.session_id, "action": "pi_agent",
                                        "args": {"task": task, "backend": backend_name},
                                        "result": "已提交异步执行", "risk": "high", "hard": True})
                return f"任务已提交（ID: {task_id}），正在后台执行。你可以继续聊天，随时问我「任务做得怎么样了」查看进度。"
            else:
                return f"任务提交失败：{msg}"

        # 同步模式（原有行为）
        self._set_state("thinking")
        if self.verbose:
            print(f"  🔧 正在调用 {backend_name}……（长任务可能几分钟，请稍候）", flush=True)
        try:
            res = backend.run(
                task,
                read_only=bool(self.cfg.get("pi", {}).get("read_only", False)),
            )
        finally:
            self._set_state("idle")

        safety.audit(self.cfg, {"session_id": self.session_id, "action": "pi_agent",
                                "args": {"task": task, "backend": backend_name},
                                "result": ("成功" if res.ok else "失败") + f"｜耗时 {res.duration:.1f}s",
                                "risk": "high", "hard": True})
        if not res.ok:
            note = f"Pi 没能完成：{res.error or '无输出'}"
            self.memory.add(self.session_id, "assistant", note)
            return note

        self.stats.record_action("pi_agent")
        # 只把「摘要」写进陪伴端记忆（不整段存，免得超长 diff 撑爆记忆库）
        summary = self._summarize_pi_result(task, res.text)
        self.memory.add(self.session_id, "assistant",
                        f"（Pi 任务：{task[:60]}）{summary}", category="笔记")
        return res.text

    def _summarize_pi_result(self, task: str, body: str, limit: int = 240) -> str:
        """把 Pi 的长结果压成短摘要再入库；摘要失败就退化为截断，绝不丢结论。"""
        text = (body or "").strip()
        if not text:
            return "（无输出）"
        if len(text) <= limit:
            return text
        try:
            brain = llm_mod.make_llm(self.cfg, system_prompt="你是文本摘要助手，只输出摘要正文。")
            brain.temperature = 0.3
            prompt = (
                f"请把下面这段「Pi（编程智能体）执行任务的结果」压缩成不超过 {limit} 字的中文摘要。\n"
                "要求：保留关键结论、涉及的文件或路径、发现的问题与后续建议；"
                "删掉代码块、日志噪音和过程描述。只输出摘要正文，不要开场白。\n\n"
                f"任务：{task[:200]}\n\n结果：\n{text[:6000]}"
            )
            summary = (brain.chat([{"role": "user", "content": prompt}]) or "").strip()
            if summary:
                return summary[: limit * 2]
        except Exception:  # noqa: BLE001
            pass
        return text[:limit] + "…（原文过长，已截断）"

    # ---------- 语音链路 ----------
    def listen(self) -> str:
        a = self.cfg["audio"]
        self._set_state("listening")
        if self.verbose:
            print("  🎤 聆听中……（说完停一下即可）", flush=True)
        data = audio_io.record_until_silence(
            sample_rate=int(a.get("sample_rate", 16000)),
            silence_threshold=float(a.get("silence_threshold", 0.012)),
            max_seconds=float(a.get("max_record_seconds", 20)),
            min_seconds=float(a.get("min_record_seconds", 0.4)),
            tail_silence_seconds=float(a.get("tail_silence_seconds", 0.5)),
            device=a.get("input_device"),
        )
        if data.size < 1600:  # 小于 0.1 秒视为无效
            print("  ⚠️ 没录到声音：麦克风可能被静音，或选错了设备（跑 python main.py --devices 查看）", flush=True)
            return ""
        rms, peak = audio_io.level_stats(data)
        if peak < 0.02:
            print(
                f"  ⚠️ 声音很小（峰值 {peak:.4f}）。建议在 config.json 把 silence_threshold 调小到 "
                f"{max(0.003, rms * 0.6):.3f}，或把系统麦克风音量调大", flush=True,
            )
        wav = audio_io.to_wav_bytes(data, int(a.get("sample_rate", 16000)))
        text = self.asr.transcribe(wav)
        if not text:
            print("  ⚠️ 没听清（识别结果为空），请再说一次", flush=True)
        return text

    def say(self, text: str) -> bool:
        """播报回复。返回 True 表示被用户插话打断，False 表示自然播完。"""
        if self.echo:
            print(f"\n🧚 Firefly：{text}\n", flush=True)
        self._set_state("speaking")
        try:
            if not self.speak:
                return False
            try:
                # role=user 的自然语言指令：音色描述 + 实时情绪风格（MiMo 官方规范）
                voice_instruction = self._tts_instruction()
                reference_audio_path = self.cfg.get("tts", {}).get("reference_audio_path", "")
                wav_bytes = self.tts.synth(text, voice_instruction, reference_audio_path)
                a = self.cfg["audio"]
                tail = float(a.get("output_tail_silence", 0.8))
                if a.get("barge_in", True):
                    return audio_io.play_wav_bytes_interruptible(
                        wav_bytes,
                        input_device=a.get("input_device"),
                        output_device=a.get("output_device"),
                        tail_silence=tail,
                        mic_threshold=float(a.get("barge_in_threshold", 0.02)),
                    )
                audio_io.play_wav_bytes(wav_bytes, device=a.get("output_device"), tail_silence=tail)
                return False
            except Exception as exc:  # noqa: BLE001
                print(f"  （语音播报失败：{exc}）", flush=True)
                return False
        finally:
            self._set_state("idle")

    # ---------- 主循环 ----------
    def _maybe_start_pet(self) -> None:
        """按配置启动桌宠并把对话状态接到它的队列（pet.enabled=false 或失败都不影响对话）。"""
        if not self.cfg.get("pet", {}).get("enabled", True):
            return
        try:
            import tkinter  # noqa: F401 — 先检测 tkinter 是否可用
            from core.pet import start_pet_thread

            pet_q = start_pet_thread(self.cfg)
            self.on_state = lambda s: pet_q.put(s)
            print("🧚 桌宠已上线：可拖拽、拖到屏幕边缘贴边隐藏；右键有菜单，双击可预览四种状态", flush=True)
        except ImportError:  # noqa: BLE001
            print("（桌宠未能启动：缺少 tkinter —— 你的 Python 可能是精简版，没有自带 GUI 库。"
                  "不影响对话。）", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"（桌宠未能启动，不影响对话：{exc}）", flush=True)

    def run_voice(self) -> None:
        self._maybe_start_pet()

        waker = WakeListener(self.cfg["wake"], sample_rate=int(self.cfg["audio"]["sample_rate"]),
                             device=self.cfg["audio"].get("input_device"))
        mode = waker.start()
        if mode == "porcupine":
            print(f"\n✅ 语音唤醒已就绪：说「{self.cfg['wake'].get('keyword','Hi Firefly')}」即可叫我", flush=True)
        else:
            print("\n⚠️ 未配置 Picovoice 语音唤醒，已启用【空格键说话】兜底模式", flush=True)
            print("   → 按【空格】后直接说你要说的话（不用喊 Hi Firefly），说完停顿约 1 秒自动结束", flush=True)
            print("   → 按【Q】退出", flush=True)
            print("   → 注意：黑窗口被鼠标点过后会进入「标记模式」吞掉按键，按一下 Esc 可解除", flush=True)
            print("   → 想用真·语音唤醒「Hi Firefly」，请看 README 里的 3 步配置指引", flush=True)

        print(f"   会话 ID：{self.session_id}｜历史记忆：{self.memory.count()} 条", flush=True)
        print("   改了 config.json（音色/模型等）不用重启：下一句自动生效\n", flush=True)

        try:
            while True:
                sig = waker.wait()
                if sig == "quit":
                    break
                if sig != "wake":
                    continue
                try:
                    audio_io.play_beep(device=self.cfg["audio"].get("output_device"))
                except Exception:
                    pass
                try:
                    user_text = self.listen()
                except Exception as exc:  # noqa: BLE001
                    print(f"  （录音/识别失败：{exc}）", flush=True)
                    try:
                        user_text = input("  ⌨️ 识别服务不可用，改用键盘输入这句话（直接回车跳过）：").strip()
                    except (EOFError, KeyboardInterrupt):
                        user_text = ""
                if not user_text:
                    continue
                # 云端兜底：即使没有本地唤醒引擎，喊「Hi Firefly」也会立刻回应
                if is_wake_phrase(user_text):
                    print(f"\n🗣 你：{user_text}", flush=True)
                    self.say("我在呢，说吧。")
                    user_text = self.listen()
                    if not user_text or is_wake_phrase(user_text):
                        continue
                print(f"\n🗣 你：{user_text}", flush=True)
                if user_text.startswith("/pi"):
                    out = self.run_pi_task(user_text[3:])
                    spoken = out if len(out) <= 200 else out[:200] + "……内容比较长，我就不全念了。"
                    self.say(spoken)
                    continue
                if _is_exit(user_text):
                    self.say("好，我先去休息啦，随时叫我。")
                    break
                # 对话 + 播报，支持被打断后立刻接着说（最多连续 3 轮）
                for _ in range(3):
                    try:
                        reply, interrupted = self.respond_stream(user_text)
                    except Exception as exc:  # noqa: BLE001
                        print(f"  （对话失败：{exc}）", flush=True)
                        break
                    if not interrupted:
                        break
                    # 被用户插话打断 → 立即接着听他说
                    print("  🎤 你打断了 Firefly，请继续说……", flush=True)
                    try:
                        user_text = self.listen()
                    except Exception as exc:  # noqa: BLE001
                        print(f"  （录音/识别失败：{exc}）", flush=True)
                        break
                    if not user_text or is_wake_phrase(user_text):
                        break
                    if _is_exit(user_text):
                        self.say("好，我先去休息啦，随时叫我。")
                        return
                    print(f"\n🗣 你：{user_text}", flush=True)
        except KeyboardInterrupt:
            print("\n已退出。", flush=True)
        finally:
            waker.close()
            self.memory.close()
            self.close_emotion()

    def run_text(self) -> None:
        self._maybe_start_pet()
        print("\n【键盘模式】直接打字回车即可对话；输入 q 退出。", flush=True)
        print("   想让 Pi 帮忙：输入 /pi 任务（例如「/pi 帮我看看这个项目的结构」）", flush=True)
        print("   改了 config.json（音色/模型等）不用重启：下一句会自动生效，"
              "也可输入 /reload 立刻重载", flush=True)
        print(f"   会话 ID：{self.session_id}｜历史记忆：{self.memory.count()} 条\n", flush=True)
        while True:
            try:
                user_text = input("你：").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not user_text:
                continue
            if user_text.lower() in ("q", "quit", "exit"):
                break
            if user_text.lower() in ("/reload", "/reload config"):
                notes = self.reload_config_if_changed(force=True)
                print("（已重载 config.json）" + ("\n  · " + "\n  · ".join(notes) if notes
                                                else " 没有变化"), flush=True)
                continue
            if user_text.startswith("/pi"):
                t0 = time.time()
                reply = self.run_pi_task(user_text[3:])
                print(f"\n🧚 Firefly（{time.time()-t0:.1f}s）：{reply}\n", flush=True)
                continue
            t0 = time.time()
            try:
                reply = self.respond(user_text)
            except Exception as exc:  # noqa: BLE001
                print(f"（对话失败：{exc}）", flush=True)
                continue
            print(f"\n🧚 Firefly（{time.time()-t0:.1f}s）：{reply}\n", flush=True)
            if self.speak:
                self.say(reply)
        self.memory.close()
        self.close_emotion()


def _is_exit(text: str) -> bool:
    return any(k in text for k in ("退出", "再见", "结束吧", "拜拜", "退下吧", "关闭助手"))


def is_wake_phrase(text: str) -> bool:
    """识别结果是否只是在叫名字（Hi Firefly），用于云端兜底唤醒。"""
    if not text:
        return False
    t = re.sub(r"[^\w\u4e00-\u9fff]", "", text.lower())
    if len(t) > 14:
        return False
    return any(k in t for k in ("firefly", "菲儿", "菲瑞", "飞儿", "费尔瑞"))


def diag(cfg: dict) -> None:
    """云端服务体检：分别测 ASR / TTS / LLM，给出人话结论。"""
    import numpy as np

    wav = audio_io.to_wav_bytes(np.zeros(3200, dtype=np.int16), 16000)

    print("\n=== 云端服务体检 ===")

    # 1. ASR
    print("\n1) 语音识别 ASR", end="：", flush=True)
    try:
        text = make_asr(cfg).transcribe(wav)
        print(f"✅ 正常（测试返回：{text[:30] or '空（静音正常）'}）")
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 失败\n   {exc}")

    # 2. TTS
    print("2) 语音合成 TTS", end="：", flush=True)
    try:
        b = self_tts(cfg).synth("你好，我是流萤。")
        print(f"✅ 正常（收到 {len(b)} 字节音频）")
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 失败\n   {exc}")

    # 3. LLM
    print("3) 大脑 LLM", end="：", flush=True)
    try:
        reply = llm_mod.make_llm(cfg).chat([{"role": "user", "content": "只回复两个字：收到"}])
        print(f"✅ 正常（模型回复：{reply[:30]}）")
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 失败\n   {exc}")

    print("\n（哪一项 ❌ 就处理哪一项，三项全绿就可以正常对话了）")


def self_tts(cfg: dict):
    return make_tts(cfg)


def mic_test(cfg: dict) -> None:
    """麦克风音量体检：录 3 秒，报告电平并给出阈值建议。"""
    a = cfg["audio"]
    print("\n=== 麦克风体检 ===")
    print("请马上对着麦克风，用正常音量说 3 秒钟话……", flush=True)
    data = audio_io.record_seconds(3.0, int(a.get("sample_rate", 16000)), device=a.get("input_device"))
    rms, peak = audio_io.level_stats(data)
    print(f"\n平均音量 RMS = {rms:.4f}   峰值 = {peak:.4f}")
    if peak < 0.01:
        print("❌ 几乎没收到声音：检查麦克风是否被静音 / 是否为系统默认设备 / 权限是否开启。")
    elif peak < 0.05:
        print("⚠️ 声音偏小：建议提高系统麦克风音量（录音设备 → 级别）。")
    else:
        print("✅ 麦克风正常。")
    suggest = round(max(0.003, min(0.05, rms * 0.6)), 3)
    print(f"建议 config.json 的 silence_threshold 设为：{suggest}（当前 {a.get('silence_threshold')}）")


def pi_check(cfg: dict) -> None:
    """外部 agent（Pi）体检：命令行是否可用、配置是否合理。"""
    from core import agent_backend

    print("\n=== 外部 Agent（Pi）体检 ===")
    pcfg = cfg.get("pi", {}) or {}
    print(f"配置：enabled={pcfg.get('enabled', True)}｜cli_path={pcfg.get('cli_path') or '(自动在 PATH 查找)'}")
    print(f"      cwd={pcfg.get('cwd')}｜read_only={pcfg.get('read_only', False)}｜timeout={pcfg.get('timeout', 600)}s")
    backends = agent_backend.list_backends(cfg)
    if not backends:
        print("❌ 没有注册任何后端")
        return
    for name, b in backends.items():
        print(f"{'✅' if b.available() else '❌'} 后端 {name}：{b.describe()}")
    print("\n提示：只有用户明确输入 /pi <任务>（或点名要求）时才会调用 Pi；平时陪伴端完全独立运行。")


def emotion_check(cfg: dict) -> None:
    """情绪体检：显示当前情绪状态与将要发给 MiMo-TTS 的风格指令。"""
    emo = EmotionModel(cfg, db_path=cfg["memory"]["db_path"])
    s = emo.snapshot()
    print("\n=== 情绪状态（程序化）===")
    print(f"启用={s['enabled']}｜风格模式={s['style_mode']}｜累计轮次={s['turns']}")
    print(f"愉悦度 {s['valence']:+.2f}   （-1 低落 ~ +1 开心）")
    print(f"唤醒度 {s['arousal']:.2f}    （0 慵懒疲惫 ~ 1 激动亢奋）")
    print(f"亲密度 {s['intimacy']:.2f}   （0 陌生 ~ 1 很熟）")
    print(f"主情绪：{s['label']}｜复合情绪：{s['compound']}")
    print(f"推断依据：{s['reason'] or '（暂无）'}")
    print("\n--- 将发给 MiMo-TTS 的风格指令（role=user）---")
    print(s["style_preview"])
    print("\n--- 最近变化 ---")
    for h in emo.history(8):
        ts = time.strftime("%m-%d %H:%M", time.localtime(h["ts"]))
        print(f"  [{ts}] {h['label']}  v={h['valence']:+.2f} a={h['arousal']:.2f} i={h['intimacy']:.2f}")
    emo.close()


def run_app(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Firefly 本地语音助手")
    ap.add_argument("--text", action="store_true", help="键盘输入模式")
    ap.add_argument("--devices", action="store_true", help="列出音频设备")
    ap.add_argument("--search", metavar="关键词", help="检索历史对话")
    ap.add_argument("--selftest", action="store_true", help="离线自检")
    ap.add_argument("--no-speak", action="store_true", help="不播报语音（只显示文字）")
    ap.add_argument("--mic-test", action="store_true", help="麦克风音量体检（录 3 秒并给出阈值建议）")
    ap.add_argument("--diag", action="store_true", help="云端服务体检（分别测 ASR/TTS/LLM）")
    ap.add_argument("--pet", action="store_true", help="只启动桌宠（不进入语音对话）")
    ap.add_argument("--stats", action="store_true", help="查看使用统计")
    ap.add_argument("--pi-check", action="store_true", help="外部 agent（Pi）体检")
    ap.add_argument("--emotion", action="store_true", help="查看当前情绪状态与 TTS 风格指令")
    args = ap.parse_args(argv)

    cfg = load_config()

    if args.pet:
        from core.pet import run_pet

        run_pet(cfg)
        return 0

    if args.stats:
        stats = UsageStats(cfg.get("stats", {}).get("path", "data/stats.json"))
        s = stats.summary()
        print("\n=== 使用统计 ===")
        for k, v in s.items():
            if k == "最常用操作":
                print(f"  {k}：")
                if v:
                    for name, cnt in v:
                        print(f"    {name}: {cnt} 次")
                else:
                    print("    （暂无）")
            elif k == "分类分布":
                print(f"  {k}：")
                for cat, cnt in v.items():
                    if cnt > 0:
                        print(f"    {cat}: {cnt} 条")
            else:
                print(f"  {k}：{v}")
        print()
        return 0

    if args.diag:
        diag(cfg)
        return 0

    if args.mic_test:
        mic_test(cfg)
        return 0

    if args.devices:
        for d in audio_io.list_devices():
            print(f"[{d['index']}] {d['name']}  输入{d['inputs']} / 输出{d['outputs']}")
        return 0

    if args.search:
        mem = Memory(cfg["memory"]["db_path"])
        hits = mem.search(args.search, limit=10)
        print(f"检索「{args.search}」命中 {len(hits)} 条：")
        for h in hits:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(h["ts"]))
            print(f"  [{ts}] {h['role']}：{h['content'][:120]}")
        mem.close()
        return 0

    if args.pi_check:
        pi_check(cfg)
        return 0

    if args.emotion:
        emotion_check(cfg)
        return 0

    if args.selftest:
        from tests.smoke_test import run_selftest
        return run_selftest(cfg)

    firefly = Firefly(cfg, speak=not args.no_speak)
    if args.text:
        firefly.run_text()
    else:
        firefly.run_voice()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run_app())
    except Exception:  # noqa: BLE001
        print("\n❌ 程序异常退出，以下是错误详情（可截图发我）：", flush=True)
        traceback.print_exc()
        try:
            input("\n按回车键关闭窗口……")
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)
