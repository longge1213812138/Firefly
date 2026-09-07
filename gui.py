"""流萤 · 控制台（GUI 操作台，对应需求 F-03）。

四个标签页：
  ① 对话  —— 文字聊天（与语音模式同一套大脑/记忆/安全闸口），可点「🎙 说话」
             用麦克风说一句；危险操作会弹窗确认（撤销清单）。
  ② 记忆  —— 浏览与检索全部历史对话（本地 SQLite）。
  ③ 配置  —— API Key（掩码显示）、模型、音色、唤醒词、录音阈值、桌宠开关、
             开机自启、人设编辑器。保存后自动重载大脑（人设即时生效）。
  ④ 状态  —— 一键体检（ASR/TTS/LLM）、离线自检、审计日志查看、数据位置说明。

仅依赖 Python 自带 tkinter；聊天与大模型调用都在后台线程，界面不卡。
弹窗策略（本期约定）：危险操作=askyesno 弹窗；保存成功=提示框；退出=确认框。
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
    from tkinter.scrolledtext import ScrolledText
except Exception:  # pragma: no cover
    tk = None

from core.config import ROOT, load_config, load_persona  # noqa: E402
from core.memory import Memory  # noqa: E402
from core.tts import list_available_voices, get_voice_info  # noqa: E402

FONT = ("Microsoft YaHei UI", 10)
FONT_BOLD = ("Microsoft YaHei UI", 10, "bold")
FONT_SMALL = ("Microsoft YaHei UI", 9)

AUTOSTART_NAME = "流萤Fairy助手.bat"


# ---------------------------------------------------------------- 工具函数
def autostart_path() -> Path:
    startup = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup"
    return startup / AUTOSTART_NAME


def autostart_enabled() -> bool:
    return autostart_path().exists()


def set_autostart(enable: bool) -> str:
    """开机自启：往「启动」文件夹放/删一个 bat（GBK+CRLF，双击系统可识别）。"""
    p = autostart_path()
    if enable:
        content = (
            "@echo off\r\n"
            f"cd /d \"{ROOT}\"\r\n"
            "start \"\" \"启动助手.bat\"\r\n"
        )
        with open(p, "w", encoding="gbk", newline="") as f:
            f.write(content)
    elif p.exists():
        p.unlink()
    return str(p)


def _tail(path: Path, n: int = 200) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-n:]
    except OSError:
        return []


# ---------------------------------------------------------------- 聊天后台线程
class ChatWorker(threading.Thread):
    """单一后台线程：构造 Fairy、跑对话/录音，结果经 ui 队列交还界面。"""

    def __init__(self, ui: "queue.Queue"):
        super().__init__(daemon=True, name="fairy-gui-chat")
        self.ui = ui
        self.jobs: "queue.Queue[tuple]" = queue.Queue()
        self.fairy = None
        self._confirm_box: dict = {}

    # --- 主线程调用的入口 ---
    def submit(self, *job) -> None:
        self.jobs.put(job)

    def confirm(self, prompt: str) -> bool:
        """危险操作确认：请求主线程弹窗，阻塞等答案（后台线程安全）。"""
        ev = threading.Event()
        box: dict = {}
        self.ui.put(("confirm", prompt, ev, box))
        ev.wait()
        return bool(box.get("ok"))

    # --- 线程主体 ---
    def run(self) -> None:
        while True:
            job = self.jobs.get()
            kind = job[0]
            try:
                if kind == "reload":
                    from core.config import load_config as _lc

                    self.fairy = None
                    self._ensure_fairy(_lc())
                    self.ui.put(("status", "大脑已重载"))
                elif kind == "chat":
                    _, text, speak = job
                    self._ensure_fairy(load_config())
                    f = self.fairy
                    f.echo = False
                    f.speak = speak
                    f.confirm_fn = self.confirm
                    f.on_state = lambda s: self.ui.put(("pet_state", s))
                    self.ui.put(("chat_user", text))
                    self.ui.put(("status", "思考中…"))
                    t0 = time.time()
                    reply = f.respond(text)
                    self.ui.put(("chat_fairy", f"{reply}"))
                    self.ui.put(("status", f"就绪｜本轮 {time.time()-t0:.1f}s"))
                elif kind == "voice_input":
                    (_, speak) = job
                    self._ensure_fairy(load_config())
                    f = self.fairy
                    f.echo = False
                    f.speak = speak
                    f.confirm_fn = self.confirm
                    f.on_state = lambda s: self.ui.put(("pet_state", s))
                    self.ui.put(("status", "正在聆听…（说完停顿 1 秒自动结束）"))
                    text = f.listen()
                    if not text:
                        self.ui.put(("chat_sys", "没听清（识别结果为空），请再试一次或直接打字。"))
                        self.ui.put(("status", "就绪"))
                        continue
                    self.ui.put(("chat_user", text))
                    self.ui.put(("status", "思考中…"))
                    t0 = time.time()
                    reply = f.respond(text)
                    self.ui.put(("chat_fairy", reply))
                    self.ui.put(("status", f"就绪｜本轮 {time.time()-t0:.1f}s"))
            except Exception as exc:  # noqa: BLE001
                self.ui.put(("chat_sys", f"出错了：{exc}"))
                self.ui.put(("status", "出错（见对话区）"))
            finally:
                self.ui.put(("busy", False))

    def _ensure_fairy(self, cfg: dict) -> None:
        if self.fairy is None:
            from main import Fairy

            self.fairy = Fairy(cfg, speak=True, verbose=False, echo=False,
                               confirm_fn=self.confirm)
            self.ui.put(("status", f"大脑就绪：{cfg.get('llm', {}).get('model', '?')}"
                                   f"｜记忆 {self.fairy.memory.count()} 条"))


# ---------------------------------------------------------------- 主界面
class ConsoleApp:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.ui: "queue.Queue" = queue.Queue()
        self.worker = ChatWorker(self.ui)
        self.worker.start()
        self.busy = False
        self._hist_mem: Memory | None = None

        self.root = tk.Tk()
        self.root.title("流萤 · 控制台")
        self.root.geometry("920x660")
        self.root.minsize(760, 560)
        try:
            self.root.call("tk", "scaling", 1.25)
        except Exception:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except Exception:
            pass
        style.configure("TNotebook.Tab", padding=(16, 6))
        style.configure("TButton", padding=(10, 4))

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=8, pady=8)
        self._build_chat_tab()
        self._build_memory_tab()
        self._build_config_tab()
        self._build_status_tab()

        self.root.after(150, self._poll_ui)

    # ============ ① 对话 ============
    def _build_chat_tab(self) -> None:
        f = ttk.Frame(self.nb)
        self.nb.add(f, text=" 对话 ")

        self.chat_box = ScrolledText(f, height=22, font=FONT, state="disabled",
                                     wrap="word", relief="flat", background="#fbfbf7")
        self.chat_box.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        for tag, color, kw in (
            ("user", "#2b5fb8", {}),
            ("fairy", "#1f7a3d", {}),
            ("sys", "#8a8f98", {"font": FONT_SMALL}),
        ):
            self.chat_box.tag_configure(tag, foreground=color, **kw)
        self._chat_append("sys", "这里是和 Fairy 聊天的地方（与语音模式共用同一份记忆）。"
                                 "涉及删除/覆盖/外发的操作会先弹窗让你确认。\n")

        row = ttk.Frame(f)
        row.pack(fill="x", padx=8, pady=(2, 2))
        self.speak_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="回复后语音播报", variable=self.speak_var).pack(side="left")
        self.btn_voice = ttk.Button(row, text="🎙 说话（麦克风说一句）", command=self._voice_input)
        self.btn_voice.pack(side="right")
        self.btn_clear = ttk.Button(row, text="清空显示", command=self._clear_chat)
        self.btn_clear.pack(side="right", padx=(0, 8))

        row2 = ttk.Frame(f)
        row2.pack(fill="x", padx=8, pady=(2, 8))
        self.entry = ttk.Entry(row2, font=FONT)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda e: self._send())
        self.btn_send = ttk.Button(row2, text="发送", command=self._send)
        self.btn_send.pack(side="left", padx=(8, 0))

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.status_var, font=FONT_SMALL,
                  foreground="#6b7280", anchor="w").pack(fill="x", padx=12, pady=(0, 6))

    def _chat_append(self, who: str, text: str) -> None:
        prefix = {"user": "你：", "fairy": "Fairy：", "sys": "· "}[who]
        ts = datetime.now().strftime("%H:%M")
        self.chat_box.configure(state="normal")
        self.chat_box.insert("end", f"[{ts}] ", "sys")
        self.chat_box.insert("end", f"{prefix}{text}\n\n", who)
        self.chat_box.configure(state="disabled")
        self.chat_box.yview("end")

    def _send(self) -> None:
        text = self.entry.get().strip()
        if not text or self.busy:
            return
        self.entry.delete(0, "end")
        self._set_busy(True)
        self.worker.submit("chat", text, bool(self.speak_var.get()))

    def _voice_input(self) -> None:
        if self.busy:
            return
        self._set_busy(True)
        self.worker.submit("voice_input", bool(self.speak_var.get()))

    def _clear_chat(self) -> None:
        if messagebox.askyesno("清空显示", "只清空窗口里的聊天显示，不会删除本地记忆。确定？"):
            self.chat_box.configure(state="normal")
            self.chat_box.delete("1.0", "end")
            self.chat_box.configure(state="disabled")

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.btn_send.configure(state=state)
        self.btn_voice.configure(state=state)

    # ============ ② 记忆 ============
    def _build_memory_tab(self) -> None:
        f = ttk.Frame(self.nb)
        self.nb.add(f, text=" 记忆 ")

        top = ttk.Frame(f)
        top.pack(fill="x", padx=8, pady=8)
        self.search_var = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.search_var, font=FONT, width=32)
        e.pack(side="left")
        e.bind("<Return>", lambda ev: self._do_search())
        ttk.Button(top, text="🔍 搜索", command=self._do_search).pack(side="left", padx=6)
        ttk.Button(top, text="最近对话", command=self._show_recent).pack(side="left")
        self.mem_count_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.mem_count_var, font=FONT_SMALL,
                  foreground="#6b7280").pack(side="right")

        self.mem_box = ScrolledText(f, font=FONT, state="disabled", wrap="word",
                                    relief="flat", background="#fbfbf7")
        self.mem_box.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.mem_box.tag_configure("role_u", foreground="#2b5fb8", font=FONT_BOLD)
        self.mem_box.tag_configure("role_a", foreground="#1f7a3d", font=FONT_BOLD)
        self.mem_box.tag_configure("ts", foreground="#8a8f98", font=FONT_SMALL)
        self._show_recent()

    def _hist(self) -> Memory:
        if self._hist_mem is None:
            self._hist_mem = Memory(self.cfg["memory"]["db_path"])
        return self._hist_mem

    def _mem_render(self, rows: list[dict], title: str) -> None:
        self.mem_box.configure(state="normal")
        self.mem_box.delete("1.0", "end")
        self.mem_box.insert("end", f"{title}\n\n", "ts")
        if not rows:
            self.mem_box.insert("end", "（没有找到记录）\n")
        for h in rows:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(h["ts"]))
            who = "你" if h["role"] == "user" else "Fairy"
            tag = "role_u" if h["role"] == "user" else "role_a"
            self.mem_box.insert("end", f"[{ts}] ", "ts")
            self.mem_box.insert("end", f"{who}：", tag)
            self.mem_box.insert("end", f"{h['content']}\n\n")
        self.mem_box.configure(state="disabled")
        self.mem_box.yview("1.0")

    def _do_search(self) -> None:
        q = self.search_var.get().strip()
        if not q:
            return
        try:
            rows = self._hist().search(q, limit=20)
            self._mem_render(rows, f"搜索「{q}」命中 {len(rows)} 条：")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("检索失败", str(exc))

    def _show_recent(self) -> None:
        try:
            rows = self._recent_rows(50)
            self._mem_render(rows, f"最近 {len(rows)} 条对话（按时间正序展示）：")
            self.mem_count_var.set(f"记忆库共 {self._hist().count()} 条｜{self.cfg['memory']['db_path']}")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("读取失败", str(exc))

    def _recent_rows(self, n: int) -> list[dict]:
        cur = self._hist().conn.cursor()
        cur.execute("SELECT id, role, content, ts FROM messages ORDER BY id DESC LIMIT ?", (n,))
        rows = [dict(r) for r in cur.fetchall()]
        rows.reverse()
        return rows

    # ============ ③ 配置 ============
    def _build_config_tab(self) -> None:
        f = ttk.Frame(self.nb)
        self.nb.add(f, text=" 配置 ")

        wrap = ttk.Frame(f)
        wrap.pack(fill="both", expand=True, padx=10, pady=8)
        wrap.columnconfigure(1, weight=1)
        row = {"n": 0}

        def next_row() -> int:
            row["n"] += 1
            return row["n"]

        def label(text: str, **kw) -> None:
            ttk.Label(wrap, text=text, **kw).grid(row=next_row(), column=0,
                                                  sticky="e", padx=(0, 6), pady=3)

        mimo = self.cfg.get("mimo", {})
        llm = self.cfg.get("llm", {})

        # API Key（掩码：不回显明文）
        label("API Key（小米 tp-…）", font=FONT)
        self.key_var = tk.StringVar(value="")  # 安全：绝不回显已存的 Key
        ttk.Entry(wrap, textvariable=self.key_var, width=46, show="•").grid(
            row=row["n"], column=1, sticky="w", pady=3)
        has_key = "已配置 ✓（输入新值可更换，留空保持不变）" if mimo.get("api_key") else "尚未配置"
        ttk.Label(wrap, text=has_key, font=FONT_SMALL, foreground="#8a8f98").grid(
            row=row["n"], column=2, sticky="w", padx=6)

        label("大脑模型", font=FONT)
        self.model_var = tk.StringVar(value=llm.get("model", "mimo-v2.5"))
        ttk.Combobox(wrap, textvariable=self.model_var, width=43,
                     values=["mimo-v2.5", "mimo-v2.5-pro"]).grid(
            row=row["n"], column=1, sticky="w", pady=3)

        label("大脑接口地址", font=FONT)
        self.baseurl_var = tk.StringVar(value=llm.get("base_url", ""))
        ttk.Entry(wrap, textvariable=self.baseurl_var, width=46).grid(
            row=row["n"], column=1, sticky="w", pady=3)

        label("TTS 模型", font=FONT)
        self.tts_model_var = tk.StringVar(value=self.cfg.get("tts", {}).get("model", "mimo-v2.5-tts"))
        ttk.Combobox(wrap, textvariable=self.tts_model_var, width=43,
                     values=["mimo-v2.5-tts", "mimo-v2.5-tts-voicedesign", "mimo-v2.5-tts-voiceclone"],
                     state="readonly").grid(row=row["n"], column=1, sticky="w", pady=3)
        ttk.Label(wrap, text="预置音色/文本设计/音频复刻", font=FONT_SMALL,
                  foreground="#8a8f98").grid(row=row["n"], column=2, sticky="w", padx=6)

        label("音色（voice）", font=FONT)
        self.voice_var = tk.StringVar(
            value=self.cfg.get("tts", {}).get("voice") or mimo.get("voice", "mimo_default"))
        ttk.Entry(wrap, textvariable=self.voice_var, width=46).grid(
            row=row["n"], column=1, sticky="w", pady=3)
        ttk.Label(wrap, text="预置音色ID或自定义音色标识", font=FONT_SMALL,
                  foreground="#8a8f98").grid(row=row["n"], column=2, sticky="w", padx=6)

        # voicedesign音色描述
        self.voice_instruction_var = tk.StringVar(value=self.cfg.get("tts", {}).get("voice_instruction", ""))
        self.voice_instruction_frame = ttk.Frame(wrap)
        self.voice_instruction_frame.grid(row=next_row(), columnspan=3, sticky="we", pady=3)
        ttk.Label(self.voice_instruction_frame, text="音色描述（voicedesign用）", font=FONT).pack(side="left")
        ttk.Entry(self.voice_instruction_frame, textvariable=self.voice_instruction_var, width=40).pack(side="left", padx=5)
        ttk.Label(self.voice_instruction_frame, text="例：温柔甜美的年轻女性，语速适中", font=FONT_SMALL,
                  foreground="#8a8f98").pack(side="left", padx=5)

        # voiceclone参考音频
        self.ref_audio_var = tk.StringVar(value=self.cfg.get("tts", {}).get("reference_audio_path", ""))
        self.ref_audio_frame = ttk.Frame(wrap)
        self.ref_audio_frame.grid(row=next_row(), columnspan=3, sticky="we", pady=3)
        ttk.Label(self.ref_audio_frame, text="参考音频（voiceclone用）", font=FONT).pack(side="left")
        ttk.Entry(self.ref_audio_frame, textvariable=self.ref_audio_var, width=35).pack(side="left", padx=5)
        ttk.Button(self.ref_audio_frame, text="浏览...", command=self._browse_ref_audio).pack(side="left", padx=5)
        ttk.Label(self.ref_audio_frame, text="10-30秒清晰人声音频", font=FONT_SMALL,
                  foreground="#8a8f98").pack(side="left", padx=5)

        label("性格随机度 temperature", font=FONT)
        self.temp_var = tk.StringVar(value=str(llm.get("temperature", 0.9)))
        ttk.Entry(wrap, textvariable=self.temp_var, width=46).grid(
            row=row["n"], column=1, sticky="w", pady=3)
        ttk.Label(wrap, text="0~1，越大越发散", font=FONT_SMALL,
                  foreground="#8a8f98").grid(row=row["n"], column=2, sticky="w", padx=6)

        label("唤醒词", font=FONT)
        self.keyword_var = tk.StringVar(value=self.cfg.get("wake", {}).get("keyword", "Hi Fairy"))
        ttk.Entry(wrap, textvariable=self.keyword_var, width=46).grid(
            row=row["n"], column=1, sticky="w", pady=3)

        label("录音静音阈值", font=FONT)
        self.thresh_var = tk.StringVar(value=str(self.cfg.get("audio", {}).get("silence_threshold", 0.008)))
        ttk.Entry(wrap, textvariable=self.thresh_var, width=46).grid(
            row=row["n"], column=1, sticky="w", pady=3)
        ttk.Label(wrap, text="越小越灵敏；安静环境 0.008~0.015", font=FONT_SMALL,
                  foreground="#8a8f98").grid(row=row["n"], column=2, sticky="w", padx=6)

        label("说完停顿判定（秒）", font=FONT)
        self.tail_var = tk.StringVar(value=str(self.cfg.get("audio", {}).get("tail_silence_seconds", 1.0)))
        ttk.Entry(wrap, textvariable=self.tail_var, width=46).grid(
            row=row["n"], column=1, sticky="w", pady=3)
        ttk.Label(wrap, text="话没说完就被截断就调大", font=FONT_SMALL,
                  foreground="#8a8f98").grid(row=row["n"], column=2, sticky="w", padx=6)

        self.barge_var = tk.BooleanVar(value=bool(self.cfg.get("audio", {}).get("barge_in", True)))
        ttk.Checkbutton(wrap, text="允许语音打断（说话时插话它就闭嘴）",
                        variable=self.barge_var).grid(row=next_row(), column=1, sticky="w", pady=3)

        self.pet_var = tk.BooleanVar(value=bool(self.cfg.get("pet", {}).get("enabled", True)))
        ttk.Checkbutton(wrap, text="语音模式启动时显示桌宠",
                        variable=self.pet_var).grid(row=next_row(), column=1, sticky="w", pady=3)

        self.autostart_var = tk.BooleanVar(value=autostart_enabled())
        ttk.Checkbutton(wrap, text="开机自动启动助手", variable=self.autostart_var,
                        command=self._toggle_autostart).grid(row=next_row(), column=1, sticky="w", pady=3)

        # 人设编辑器
        ttk.Label(wrap, text="人设 / 性格（persona）", font=FONT_BOLD).grid(
            row=next_row(), column=0, sticky="e", pady=(10, 2))
        ttk.Label(wrap, text="改完点「保存人设」立即生效，无需重启", font=FONT_SMALL,
                  foreground="#8a8f98").grid(row=row["n"], column=1, sticky="w", pady=(10, 2))
        self.persona_box = ScrolledText(wrap, width=64, height=9, font=FONT, wrap="word")
        self.persona_box.grid(row=next_row(), columnspan=3, sticky="we", pady=4)
        self.persona_box.insert("1.0", load_persona(self.cfg))

        btns = ttk.Frame(f)
        btns.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(btns, text="保存人设", command=self._save_persona).pack(side="right")
        ttk.Button(btns, text="保存配置（并重载大脑）", command=self._save_config).pack(side="right", padx=8)

    def _save_persona(self) -> None:
        text = self.persona_box.get("1.0", "end").rstrip() + "\n"
        p = Path(self.cfg.get("persona_path", ""))
        if not messagebox.askyesno("保存人设", f"将写入：\n{p}\n\n确定？"):
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        messagebox.showinfo("已保存", "人设已保存，下一句对话立即生效。")

    def _save_config(self) -> None:
        import json

        try:
            thresh = float(self.thresh_var.get())
            tail = float(self.tail_var.get())
            temp = float(self.temp_var.get())
        except ValueError:
            messagebox.showerror("格式不对", "阈值/温度请填数字（例如 0.008、1.0、0.9）")
            return
        cfg_path = ROOT / "config.json"
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("读取 config.json 失败", str(exc))
            return
        raw.setdefault("mimo", {})
        new_key = self.key_var.get().strip()
        if new_key:
            raw["mimo"]["api_key"] = new_key  # 留空 = 保持原 Key 不变
        raw.setdefault("llm", {})
        raw["llm"]["model"] = self.model_var.get().strip()
        raw["llm"]["base_url"] = self.baseurl_var.get().strip()
        raw["llm"]["temperature"] = temp
        raw.setdefault("tts", {})
        raw["tts"]["model"] = self.tts_model_var.get().strip()
        raw["tts"]["voice"] = self.voice_var.get().strip()
        raw["tts"]["voice_instruction"] = self.voice_instruction_var.get().strip()
        raw["tts"]["reference_audio_path"] = self.ref_audio_var.get().strip()
        raw.setdefault("wake", {})
        raw["wake"]["keyword"] = self.keyword_var.get().strip()
        raw.setdefault("audio", {})
        raw["audio"]["silence_threshold"] = thresh
        raw["audio"]["tail_silence_seconds"] = tail
        raw["audio"]["barge_in"] = bool(self.barge_var.get())
        raw.setdefault("pet", {})
        raw["pet"]["enabled"] = bool(self.pet_var.get())
        cfg_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        self.worker.submit("reload")
        messagebox.showinfo("已保存", "配置已保存，大脑正在重载。\n（人设即时生效；换 Key/模型/音色后下一句对话用新配置）")

    def _toggle_autostart(self) -> None:
        enable = bool(self.autostart_var.get())
        verb = "开启" if enable else "关闭"
        if not messagebox.askyesno("开机自启", f"{verb}开机自动启动助手？\n（做法：在系统「启动」文件夹放/删一个快捷脚本）"):
            self.autostart_var.set(not enable)
            return
        try:
            p = set_autostart(enable)
            messagebox.showinfo("已设置", f"开机自启已{verb}。\n脚本位置：{p}")
        except Exception as exc:  # noqa: BLE001
            self.autostart_var.set(not enable)
            messagebox.showerror("设置失败", str(exc))

    def _browse_ref_audio(self) -> None:
        """浏览选择参考音频文件（voiceclone用）。"""
        from tkinter import filedialog
        filetypes = [
            ("音频文件", "*.wav *.mp3 *.flac *.ogg *.m4a"),
            ("所有文件", "*.*"),
        ]
        path = filedialog.askopenfilename(
            title="选择参考音频文件（10-30秒清晰人声）",
            filetypes=filetypes,
            initialdir=str(ROOT / "data"),
        )
        if path:
            self.ref_audio_var.set(path)

    # ============ ④ 状态 ============
    def _build_status_tab(self) -> None:
        f = ttk.Frame(self.nb)
        self.nb.add(f, text=" 状态 ")

        top = ttk.Frame(f)
        top.pack(fill="x", padx=8, pady=8)
        self.btn_diag = ttk.Button(top, text="🩺 云端三关体检", command=lambda: self._run_tool("--diag"))
        self.btn_diag.pack(side="left")
        self.btn_selftest = ttk.Button(top, text="🧪 离线自检", command=lambda: self._run_tool("--selftest"))
        self.btn_selftest.pack(side="left", padx=8)
        ttk.Button(top, text="刷新审计日志", command=self._refresh_audit).pack(side="left", padx=8)

        self.tool_box = ScrolledText(f, height=12, font=("Consolas", 9), state="disabled",
                                     wrap="word", relief="flat", background="#f6f6f2")
        self.tool_box.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        self.audit_box = ScrolledText(f, height=10, font=("Consolas", 9), state="disabled",
                                      wrap="word", relief="flat", background="#f6f6f2")
        self.audit_box.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.audit_box.insert("end", "审计日志（每一次电脑操作都留痕，删除/覆盖/外发会标记 hard=true）：\n\n")
        self._refresh_audit()

        info = ("数据都在本地：记忆 data/memory.db ｜ 审计 data/audit.log ｜ 人设 persona/default.md ｜ 配置 config.json\n"
                "除调用云端 API（识别/合成/大脑）外，程序不向任何其他地址发送数据。")
        ttk.Label(self.root, text=info, font=FONT_SMALL, foreground="#6b7280",
                  anchor="w").pack(fill="x", padx=12, pady=(0, 6))

    def _run_tool(self, flag: str) -> None:
        if getattr(self, "_tool_running", False):
            return
        self._tool_running = True
        self.btn_diag.configure(state="disabled")
        self.btn_selftest.configure(state="disabled")
        self.tool_box.configure(state="normal")
        self.tool_box.delete("1.0", "end")
        self.tool_box.insert("end", f"运行中：python main.py {flag} …\n\n")
        self.tool_box.configure(state="disabled")

        def work():
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            proc = subprocess.Popen(
                [sys.executable, "main.py", flag],
                cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            )
            for line in proc.stdout or []:
                self.ui.put(("log_line", line.rstrip()))
            proc.wait()
            self.ui.put(("tool_done", proc.returncode))

        threading.Thread(target=work, daemon=True).start()

    def _refresh_audit(self) -> None:
        try:
            self.cfg = load_config()
            lines = _tail(Path(self.cfg["safety"]["audit_log"]), 100)
            self.audit_box.configure(state="normal")
            self.audit_box.delete("2.0", "end")
            self.audit_box.insert("end", "\n".join(lines) if lines else "（还没有操作记录）")
            self.audit_box.configure(state="disabled")
        except Exception as exc:  # noqa: BLE001
            self.audit_box.configure(state="normal")
            self.audit_box.insert("end", f"\n读取失败：{exc}")
            self.audit_box.configure(state="disabled")

    # ============ 事件循环 ============
    def _poll_ui(self) -> None:
        while True:
            try:
                msg = self.ui.get_nowait()
            except queue.Empty:
                break
            kind = msg[0]
            if kind == "chat_user":
                self._chat_append("user", msg[1])
            elif kind == "chat_fairy":
                self._chat_append("fairy", msg[1])
            elif kind == "chat_sys":
                self._chat_append("sys", msg[1])
            elif kind == "status":
                self.status_var.set(msg[1])
            elif kind == "busy":
                self._set_busy(bool(msg[1]))
            elif kind == "confirm":
                _, prompt, ev, box = msg
                box["ok"] = messagebox.askyesno("⚠️ 危险操作确认", prompt, icon="warning")
                ev.set()
            elif kind == "pet_state":
                pass  # 预留：GUI 内嵌状态指示
            elif kind == "log_line":
                self.tool_box.configure(state="normal")
                self.tool_box.insert("end", msg[1] + "\n")
                self.tool_box.see("end")
                self.tool_box.configure(state="disabled")
            elif kind == "tool_done":
                self._tool_running = False
                self.btn_diag.configure(state="normal")
                self.btn_selftest.configure(state="normal")
                self.tool_box.configure(state="normal")
                self.tool_box.insert("end", f"\n—— 结束（退出码 {msg[1]}）——\n")
                self.tool_box.see("end")
                self.tool_box.configure(state="disabled")
        self.root.after(150, self._poll_ui)

    def _on_close(self) -> None:
        if self.busy and not messagebox.askyesno(
                "退出", "Fairy 正在回复中，退出将中断本轮对话。确定退出？"):
            return
        self.root.destroy()


def main() -> int:
    if tk is None:
        print("当前 Python 缺少 tkinter，无法打开控制台。请用系统 Python 或完整版 Python 运行。")
        return 1
    try:
        ConsoleApp()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        try:
            input("\n出错了，按回车关闭……")
        except Exception:  # noqa: BLE001
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
