"""流萤 · 控制台（GUI 操作台，对应需求 F-03）。

六个标签页：
  ① 对话  —— 文字聊天（与语音模式同一套大脑/记忆/安全闸口），可点「🎙 说话」
             用麦克风说一句；危险操作会弹窗确认（撤销清单）；支持输入 /pi 任务。
  ② 记忆  —— 浏览与检索全部历史对话（本地 SQLite）。
  ③ 情感  —— 程序化情绪模型（愉悦度/唤醒度/亲密度）实时状态、情绪曲线、
             发给 MiMo-TTS 的风格指令预览（对齐小米官方情绪方案）、手动微调。
  ④ 配置  —— API Key（掩码显示）、模型、声音设置（合成方式/预置音色/音色描述/参考音频，
             可试听、可现场录制、可单独保存）、唤醒词、录音阈值、
             桌宠（开关/大小/不透明度/初始位置/演示模式，可一键重启应用）、
             开机自启、人设编辑器。保存后自动重载大脑（人设即时生效）。
  ⑤ 统计  —— 使用统计（对话次数、操作频率、分类分布、记忆库信息）。
  ⑥ 状态  —— 一键体检（ASR/TTS/LLM）、离线自检、审计日志查看、数据位置说明。

仅依赖 Python 自带 tkinter；聊天与大模型调用都在后台线程，界面不卡。
弹窗策略（本期约定）：危险操作=askyesno 弹窗；保存成功=提示框；退出=确认框。
系统托盘（需安装 pystray + pillow）：关闭窗口最小化到托盘，右键托盘图标恢复/退出。
"""
from __future__ import annotations

import json
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
    from tkinter import filedialog, messagebox, simpledialog, ttk
    from tkinter.scrolledtext import ScrolledText
except Exception:  # pragma: no cover
    tk = None

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

from core.config import (config_path, is_frozen, load_config,  # noqa: E402
                         load_persona, resolve_root)

# 打包成 exe 后，配置 / 数据 / 日志都要落在 exe 同级目录，而不是临时解包目录 _MEIPASS
APP_ROOT = resolve_root()
from core.memory import Memory  # noqa: E402
from core.stats import UsageStats  # noqa: E402
from core.tts import (TTS_MODELS, list_available_voices, get_voice_info,  # noqa: E402
                      tts_field_states, tts_settings_issues, validate_reference_audio,
                      voice_choices, voice_display_for_id, voice_id_for_display)

FONT = ("Microsoft YaHei UI", 10)
FONT_BOLD = ("Microsoft YaHei UI", 10, "bold")
FONT_SMALL = ("Microsoft YaHei UI", 9)

AUTOSTART_NAME = "流萤Firefly助手.bat"


# ---------------------------------------------------------------- 工具函数
def autostart_path() -> Path:
    startup = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup"
    return startup / AUTOSTART_NAME


def autostart_enabled() -> bool:
    return autostart_path().exists()


def set_autostart(enable: bool) -> str:
    """开机自启：往「启动」文件夹放/删一个 bat（GBK+CRLF，双击系统可识别）。

    打包成 exe 后没有「启动助手.bat」，改为直接拉起「流萤.exe --text」。
    """
    p = autostart_path()
    if enable:
        if is_frozen():
            launch = f'start "" "{APP_ROOT / "流萤.exe"}" --text\r\n'
        else:
            launch = 'start "" "启动助手.bat"\r\n'
        content = "@echo off\r\n" + f'cd /d "{APP_ROOT}"\r\n' + launch
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


def _safe_json_list(raw) -> list[str]:
    """把数据库里存的 JSON 标签串安全地转成字符串列表。"""
    try:
        val = json.loads(raw) if isinstance(raw, str) else (raw or [])
        return [str(x) for x in val] if isinstance(val, list) else []
    except Exception:  # noqa: BLE001
        return []


def coerce_number(raw, default: float) -> tuple[bool, float]:
    """把界面输入框里的文本转成数字；不合法就回退 default 并返回 (False, default)。

    用于「保存配置」的解耦：某个字段填错不该把整份配置（包括音色）一起挡下来。
    """
    try:
        return True, float(str(raw).strip())
    except (TypeError, ValueError):
        return False, float(default)


def coerce_optional_int(raw, default):
    """可留空的整数字段（如桌宠初始坐标）：空 → (True, None)，非法 → (False, default)。"""
    text = str(raw or "").strip()
    if not text:
        return True, None
    try:
        return True, int(float(text))
    except (TypeError, ValueError):
        return False, default


# ---------------------------------------------------------------- 聊天后台线程
class ChatWorker(threading.Thread):
    """单一后台线程：构造 Firefly、跑对话/录音，结果经 ui 队列交还界面。"""

    def __init__(self, ui: "queue.Queue"):
        super().__init__(daemon=True, name="firefly-gui-chat")
        self.ui = ui
        self.jobs: "queue.Queue[tuple]" = queue.Queue()
        self.firefly = None
        self.emotion = None  # 与对话共享的**唯一**情绪实例（情感页也用它）
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

                    old = self.firefly
                    self.firefly = None
                    if old is not None:
                        # 旧 Firefly 的 SQLite 连接要先还回去，否则反复「保存配置」
                        # 会一路攒着连接不释放（情绪实例由 _drop_emotion 另行关闭）
                        try:
                            old.memory.close()
                        except Exception:  # noqa: BLE001
                            pass
                    self._drop_emotion()  # 情绪实例也要按新配置重建
                    self._ensure_firefly(_lc())
                    self.ui.put(("status", "大脑已重载"))
                    self._push_emotion()
                elif kind == "emotion_snapshot":
                    cfg = load_config()
                    self._ensure_emotion(cfg)
                    self._push_emotion()
                elif kind == "emotion_nudge":
                    _, action, dv, da, di = job
                    self._ensure_emotion(load_config())
                    if self.emotion is None:
                        self.ui.put(("emotion_error", "情绪模型未启用"))
                        continue
                    if action == "reset":
                        self.emotion.reset()
                        note = "情绪已重置到基准值（对话立即生效）"
                    else:
                        self.emotion.nudge(dv, da, di)
                        note = "情绪已手动微调（对话立即生效）"
                    self._push_emotion(reload_from_db=False)
                    self.ui.put(("status", note))
                elif kind == "context":
                    # 只看不发：不主动创建 Firefly（免得"还没聊过"就白开一个会话）
                    if self.firefly is None:
                        self.ui.put(("context", {"empty": True}))
                    else:
                        self.ui.put(("context", dict(self.firefly.last_context or {})))
                elif kind == "chat":
                    _, text, speak = job
                    self._ensure_firefly(load_config())
                    f = self.firefly
                    f.echo = False
                    f.speak = speak
                    f.confirm_fn = self.confirm
                    f.on_state = lambda s: self.ui.put(("pet_state", s))
                    f.on_config_reload = lambda notes: self.ui.put(("reload_note", notes))
                    self.ui.put(("chat_user", text))
                    t0 = time.time()
                    if text.startswith("/pi"):
                        if not f.cfg.get("pi", {}).get("enabled", True):
                            reply = "Pi 功能已关闭。请到「配置」页打开「允许 /pi 调用」后重试。"
                        else:
                            self.ui.put(("status", "正在调用 Pi…（长任务可能几分钟，请稍候）"))
                            reply = f.run_pi_task(text[3:])
                    else:
                        self.ui.put(("status", "思考中…"))
                        reply = f.respond(text)
                    self.ui.put(("chat_firefly", f"{reply}"))
                    # 语音播报（与 main.py 的 run_text / run_voice 对齐）
                    if speak and reply:
                        self.ui.put(("status", "播报中…"))
                        f.say(reply)
                    self.ui.put(("status", f"就绪｜本轮 {time.time()-t0:.1f}s"))
                elif kind == "voice_input":
                    (_, speak) = job
                    self._ensure_firefly(load_config())
                    f = self.firefly
                    f.echo = False
                    f.speak = speak
                    f.confirm_fn = self.confirm
                    f.on_state = lambda s: self.ui.put(("pet_state", s))
                    f.on_config_reload = lambda notes: self.ui.put(("reload_note", notes))
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
                    self.ui.put(("chat_firefly", reply))
                    # 语音播报（与 main.py 的 run_text / run_voice 对齐）
                    if speak and reply:
                        self.ui.put(("status", "播报中…"))
                        f.say(reply)
                    self.ui.put(("status", f"就绪｜本轮 {time.time()-t0:.1f}s"))
            except Exception as exc:  # noqa: BLE001
                self.ui.put(("chat_sys", f"出错了：{exc}"))
                self.ui.put(("status", "出错（见对话区）"))
            finally:
                self.ui.put(("busy", False))

    def _ensure_emotion(self, cfg: dict):
        """保证有一个情绪实例（与对话共用的那个，不额外造第二份）。"""
        if self.emotion is None:
            from core.emotion import EmotionModel

            self.emotion = EmotionModel(cfg, db_path=cfg["memory"]["db_path"])
        return self.emotion

    def _drop_emotion(self) -> None:
        if self.emotion is not None:
            try:
                self.emotion.close()
            except Exception:  # noqa: BLE001
                pass
        self.emotion = None

    def _push_emotion(self, reload_from_db: bool = True) -> None:
        """把情绪快照 + 变化曲线交给界面（情感页据此渲染，不再是另一份内存状态）。"""
        emo = self.emotion
        if emo is None:
            self.ui.put(("emotion_error", "情绪模型尚未初始化"))
            return
        try:
            # 重读库：对话在后台线程写进去的情绪、别的窗口的手动微调都能看见
            if reload_from_db and emo.enabled:
                emo.load()
            self.ui.put(("emotion", {"snapshot": emo.snapshot(), "history": emo.history(40)}))
        except Exception as exc:  # noqa: BLE001
            self.ui.put(("emotion_error", str(exc)))

    def _ensure_firefly(self, cfg: dict) -> None:
        if self.firefly is None:
            from main import Firefly

            self._ensure_emotion(cfg)
            self.firefly = Firefly(cfg, speak=True, verbose=False, echo=False,
                               confirm_fn=self.confirm, emotion=self.emotion)
            self.ui.put(("status", f"大脑就绪：{cfg.get('llm', {}).get('model', '?')}"
                                   f"｜记忆 {self.firefly.memory.count()} 条"))


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
        self._build_emotion_tab()
        self._build_config_tab()
        self._build_stats_tab()
        self._build_status_tab()

        # 桌宠随控制台同步启动（pet.enabled 可关）；对话状态会实时转发给它
        self.pet_q: "queue.Queue[str] | None" = None
        self._start_pet_if_enabled()

        self.root.after(150, self._poll_ui)

    # ============ 桌宠联动 ============
    def _start_pet_if_enabled(self) -> None:
        """控制台启动时把桌宠一起带出来——双击 exe 后两者同步出现。"""
        from core.pet import resolve_pet_options, start_pet_thread

        if not resolve_pet_options(self.cfg)["enabled"]:
            return
        try:
            self.pet_q = start_pet_thread(self.cfg)
            self.status_var.set("就绪｜桌宠已同步启动")
        except Exception as exc:  # noqa: BLE001
            self.status_var.set(f"桌宠未能启动（不影响控制台使用）：{exc}")

    def _current_pet_options(self) -> dict:
        """读取配置页上当前填写的桌宠设置（未保存也能先用于临时重启）。"""
        from core.pet import resolve_pet_options

        return resolve_pet_options({"pet": {
            "enabled": bool(self.pet_var.get()),
            "demo": bool(self.pet_demo_var.get()),
            "scale": self.pet_scale_var.get(),
            "opacity": self.pet_opacity_var.get(),
            "start_x": self.pet_x_var.get().strip() or None,
            "start_y": self.pet_y_var.get().strip() or None,
        }})

    def _restart_pet(self) -> None:
        """关掉现有桌宠，按配置页当前填写的值重新启动（不写盘，保存配置才永久生效）。"""
        from core.pet import start_pet_thread, stop_pet

        opts = self._current_pet_options()
        stop_pet(self.pet_q)
        self.pet_q = None
        if not opts["enabled"]:
            self.status_var.set("桌宠已关闭｜想重新打开：勾选「自动显示桌宠」后再点重启")
            return
        cfg = dict(self.cfg)
        cfg["pet"] = dict(opts)
        try:
            self.pet_q = start_pet_thread(cfg)
            self.status_var.set("桌宠已按当前设置重启｜要永久生效请点「保存配置」")
        except Exception as exc:  # noqa: BLE001
            self.status_var.set(f"桌宠重启失败：{exc}")

    def _preview_pet(self) -> None:
        """让桌宠轮流展示四种状态一轮，方便调整大小/透明度时看效果。"""
        if self.pet_q is None:
            messagebox.showinfo("桌宠未在运行", "先勾选「启动程序时自动显示桌宠」，再点「重启桌宠」。")
            return
        from core.pet import STATES

        q = self.pet_q

        def work() -> None:
            for s in STATES:
                q.put(s)
                time.sleep(1.2)
            q.put("idle")

        threading.Thread(target=work, daemon=True, name="firefly-pet-preview").start()
        self.status_var.set("桌宠正在演示四种状态……")

    # ============ ① 对话 ============
    def _build_chat_tab(self) -> None:
        f = ttk.Frame(self.nb)
        self.nb.add(f, text=" 对话 ")

        self.chat_box = ScrolledText(f, height=22, font=FONT, state="disabled",
                                     wrap="word", relief="flat", background="#fbfbf7")
        self.chat_box.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        for tag, color, kw in (
            ("user", "#2b5fb8", {}),
            ("firefly", "#1f7a3d", {}),
            ("sys", "#8a8f98", {"font": FONT_SMALL}),
        ):
            self.chat_box.tag_configure(tag, foreground=color, **kw)
        self._chat_append("sys", "这里是和 Firefly 聊天的地方（与语音模式共用同一份记忆）。"
                                 "涉及删除/覆盖/外发的操作会先弹窗让你确认。\n"
                                 "想让 Pi 帮忙：输入「/pi 任务」，例如「/pi 帮我看看这个项目的结构」"
                                 "（只在明确要求时调用，且每次都会弹窗确认）。\n")

        # 任务状态面板（可折叠）
        self._task_panel_visible = tk.BooleanVar(value=False)
        self._build_task_panel(f)

        row = ttk.Frame(f)
        row.pack(fill="x", padx=8, pady=(2, 2))
        self.speak_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="回复后语音播报", variable=self.speak_var).pack(side="left")
        self.btn_voice = ttk.Button(row, text="🎙 说话（麦克风说一句）", command=self._voice_input)
        self.btn_voice.pack(side="right")
        self.btn_clear = ttk.Button(row, text="清空显示", command=self._clear_chat)
        self.btn_clear.pack(side="right", padx=(0, 8))
        self.btn_ctx = ttk.Button(row, text="🔍 本轮上下文", command=self._show_context_click)
        self.btn_ctx.pack(side="right", padx=(0, 8))
        self.btn_tasks = ttk.Button(row, text="📋 任务面板", command=self._toggle_task_panel)
        self.btn_tasks.pack(side="right", padx=(0, 8))

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

    def _build_task_panel(self, parent: ttk.Frame) -> None:
        """构建任务状态面板（初始隐藏）。"""
        self._task_frame = ttk.LabelFrame(parent, text="📋 任务状态", padding=(8, 4))
        # 初始不 pack，等用户点击按钮时再显示

        # 任务列表
        self._task_list_var = tk.StringVar(value="暂无任务")
        self._task_list_label = ttk.Label(self._task_frame, textvariable=self._task_list_var,
                                          font=FONT_SMALL, justify="left", wraplength=600)
        self._task_list_label.pack(fill="x", padx=4, pady=4)

        # 操作按钮行
        btn_row = ttk.Frame(self._task_frame)
        btn_row.pack(fill="x", padx=4, pady=(0, 4))
        ttk.Button(btn_row, text="刷新", command=self._refresh_tasks).pack(side="left")
        ttk.Button(btn_row, text="取消全部", command=self._cancel_all_tasks).pack(side="left", padx=(8, 0))

    def _toggle_task_panel(self) -> None:
        """切换任务面板显示/隐藏。"""
        if self._task_panel_visible.get():
            self._task_frame.pack_forget()
            self._task_panel_visible.set(False)
        else:
            self._task_frame.pack(fill="x", padx=8, pady=(4, 2), before=self.chat_box.master.winfo_children()[1]
                                  if len(self.chat_box.master.winfo_children()) > 1 else None)
            self._task_panel_visible.set(True)
            self._refresh_tasks()

    def _refresh_tasks(self) -> None:
        """刷新任务列表显示。"""
        try:
            from core.harness_middleware import format_task_list
            tasks = self.worker.firefly.list_tasks() if hasattr(self.worker, 'firefly') else []
            if tasks:
                from core.harness_middleware import format_task_list
                self._task_list_var.set(format_task_list(tasks))
            else:
                self._task_list_var.set("暂无任务")
        except Exception as exc:
            self._task_list_var.set(f"刷新失败：{exc}")

    def _cancel_all_tasks(self) -> None:
        """取消所有任务。"""
        if not messagebox.askyesno("确认", "确定要取消所有任务吗？"):
            return
        try:
            tasks = self.worker.firefly.list_tasks() if hasattr(self.worker, 'firefly') else []
            for t in tasks:
                if t.get('state') in ('pending', 'running'):
                    self.worker.firefly.cancel_task(t.get('id', ''))
            self._refresh_tasks()
        except Exception as exc:
            messagebox.showerror("错误", f"取消失败：{exc}")

    def _chat_append(self, who: str, text: str) -> None:
        prefix = {"user": "你：", "firefly": "Firefly：", "sys": "· "}[who]
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

    def _show_context_click(self) -> None:
        """「🔍 本轮上下文」：向后台要一份上一轮真正发出去的上下文快照（P2-8）。"""
        self.worker.submit("context")

    def _show_context(self, payload: dict | None) -> None:
        """把上下文快照摊开给用户看——这是"情感/记忆到底注没注入"的唯一硬证据。"""
        payload = payload or {}
        if payload.get("empty") or not payload.get("blocks"):
            messagebox.showinfo("本轮上下文", "还没有进行过对话。\n"
                                              "先在下面发一句，回来再点这里，就能看到"
                                              "实际发给大模型的人设 / 往事 / 心情 / 对话历史。")
            return

        # 只保留一个上下文窗口：反复点按钮不该开出一堆一模一样的窗口
        old = getattr(self, "_ctx_win", None)
        if old is not None:
            try:
                old.destroy()
            except Exception:  # noqa: BLE001
                pass

        win = tk.Toplevel(self.root)
        self._ctx_win = win
        win.title("本轮上下文（实际发给大模型的内容）")
        win.geometry("760x620")
        box = ScrolledText(win, font=FONT_SMALL, wrap="word", state="normal")
        box.pack(fill="both", expand=True, padx=8, pady=8)

        def add(text: str, tag: str = "") -> None:
            box.insert("end", text, tag)

        box.tag_configure("h", foreground="#1f7a3d", font=FONT_BOLD)
        box.tag_configure("dim", foreground="#8a8f98")
        box.tag_configure("on", foreground="#2b5fb8")
        box.tag_configure("off", foreground="#b23b3b")

        add(f"模型 {payload.get('model', '?')}｜温度 {payload.get('temperature', '?')}"
            f"｜估算约 {payload.get('est_tokens', 0)} tokens"
            f"（system {payload.get('system_chars', 0)} 字 / "
            f"{payload.get('system_tokens', 0)} tokens + "
            f"历史 {payload.get('history_turns', 0)} 条 / "
            f"{payload.get('history_tokens', 0)} tokens）\n", "dim")
        add(f"召回条数上限 recall_top_k={payload.get('recall_top_k', '?')}"
            f"｜携带轮数 max_history_turns={payload.get('max_history_turns', '?')}"
            f"｜记忆库共 {payload.get('memory_total', 0)} 条\n", "dim")
        add("情绪注入上下文：")
        add("已开启 ✓\n" if payload.get("emotion_injected") else "未注入 ✗\n",
            "on" if payload.get("emotion_injected") else "off")
        add("往事召回：")
        add("本轮有命中 ✓\n" if payload.get("recall_injected") else "本轮无命中（可能未达门槛）\n",
            "on" if payload.get("recall_injected") else "off")
        add("（情绪/往事任一项没出现，就去「配置」页对应分组找开关："
            "「把『此刻心情』注入大模型」/「重要记忆常驻注入」）\n\n", "dim")

        for blk in payload.get("blocks", []):
            add(f"【{blk.get('title')}】{blk.get('chars', 0)} 字\n", "h")
            add((blk.get("text") or "") + "\n\n")

        add(f"【近 {payload.get('history_turns', 0)} 轮对话】\n", "h")
        for m in payload.get("history", []) or []:
            who = "你" if m.get("role") == "user" else "Firefly"
            add(f"  {who}：{m.get('content', '')}\n")
        box.configure(state="disabled")
        box.yview_moveto(0)

    # ============ ② 记忆 ============
    def _build_memory_tab(self) -> None:
        f = ttk.Frame(self.nb)
        self.nb.add(f, text=" 记忆 ")

        bar = ttk.Frame(f)
        bar.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(bar, text="关键词", font=FONT).pack(side="left")
        self.search_var = tk.StringVar()
        ent = ttk.Entry(bar, textvariable=self.search_var, font=FONT, width=22)
        ent.pack(side="left", padx=(4, 6))
        ent.bind("<Return>", lambda ev: self._mem_reload(reset_page=True))
        ttk.Button(bar, text="🔍 搜索",
                   command=lambda: self._mem_reload(reset_page=True)).pack(side="left")
        ttk.Button(bar, text="最近", command=self._mem_recent).pack(side="left", padx=4)
        ttk.Button(bar, text="重置筛选", command=self._mem_reset_filter).pack(side="left")

        ttk.Label(bar, text="  分类", font=FONT).pack(side="left")
        self.mem_cat_var = tk.StringVar(value="全部")
        self.mem_cat_box = ttk.Combobox(bar, textvariable=self.mem_cat_var, width=10,
                                        state="readonly", values=["全部"])
        self.mem_cat_box.pack(side="left", padx=4)
        self.mem_cat_box.bind("<<ComboboxSelected>>",
                              lambda ev: self._mem_reload(reset_page=True))

        ttk.Label(bar, text="  重要度≥", font=FONT).pack(side="left")
        self.mem_imp_var = tk.StringVar(value="0")
        imp_box = ttk.Combobox(bar, textvariable=self.mem_imp_var, width=4,
                               state="readonly", values=["0", "3", "5", "8"])
        imp_box.pack(side="left", padx=4)
        imp_box.bind("<<ComboboxSelected>>", lambda ev: self._mem_reload(reset_page=True))

        act = ttk.Frame(f)
        act.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Button(act, text="⭐ 提高重要度", command=lambda: self._mem_bump(1)).pack(side="left")
        ttk.Button(act, text="🔽 降低重要度", command=lambda: self._mem_bump(-1)).pack(side="left", padx=4)
        ttk.Button(act, text="🏷 加标签", command=self._mem_add_tag).pack(side="left")
        ttk.Button(act, text="🗑 删除选中", command=self._mem_delete).pack(side="left", padx=4)
        ttk.Button(act, text="⬇ 导出 JSON", command=lambda: self._mem_export("json")).pack(side="left")
        ttk.Button(act, text="⬇ 导出 CSV", command=lambda: self._mem_export("csv")).pack(side="left", padx=4)
        ttk.Button(act, text="🧹 清理过期", command=self._mem_cleanup).pack(side="left")
        self.mem_count_var = tk.StringVar(value="")
        ttk.Label(act, textvariable=self.mem_count_var, font=FONT_SMALL,
                  foreground="#6b7280").pack(side="right")

        body = ttk.Panedwindow(f, orient="vertical")
        body.pack(fill="both", expand=True, padx=8, pady=(0, 4))

        top = ttk.Frame(body)
        cols = ("ts", "role", "category", "importance", "content")
        self.mem_tree = ttk.Treeview(top, columns=cols, show="headings", height=12,
                                     selectmode="browse")
        for key, text, width, anchor, stretch in (
            ("ts", "时间", 118, "w", False),
            ("role", "角色", 56, "center", False),
            ("category", "分类", 62, "center", False),
            ("importance", "重要度", 58, "center", False),
            ("content", "内容（双击下方看全文）", 520, "w", True),
        ):
            self.mem_tree.heading(key, text=text)
            self.mem_tree.column(key, width=width, anchor=anchor, stretch=stretch)
        vs = ttk.Scrollbar(top, orient="vertical", command=self.mem_tree.yview)
        self.mem_tree.configure(yscrollcommand=vs.set)
        self.mem_tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.mem_tree.tag_configure("user", foreground="#2b5fb8")
        self.mem_tree.tag_configure("assistant", foreground="#1f7a3d")
        self.mem_tree.tag_configure("important", background="#fdf6e3")
        self.mem_tree.bind("<<TreeviewSelect>>", lambda ev: self._mem_show_detail())
        body.add(top, weight=3)

        det = ttk.Frame(body)
        ttk.Label(det, text="选中条目的完整内容", font=FONT_SMALL,
                  foreground="#8a8f98").pack(anchor="w")
        self.mem_detail = ScrolledText(det, height=7, font=FONT, wrap="word")
        self.mem_detail.pack(fill="both", expand=True)
        body.add(det, weight=2)

        page = ttk.Frame(f)
        page.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(page, text="← 上一页", command=lambda: self._mem_page(-1)).pack(side="left")
        self.mem_page_var = tk.StringVar(value="")
        ttk.Label(page, textvariable=self.mem_page_var, font=FONT_SMALL).pack(side="left", padx=8)
        ttk.Button(page, text="下一页 →", command=lambda: self._mem_page(1)).pack(side="left")
        ttk.Label(page, text="每页", font=FONT_SMALL).pack(side="left", padx=(16, 2))
        self.mem_size_var = tk.StringVar(value="50")
        size_box = ttk.Combobox(page, textvariable=self.mem_size_var, width=5,
                                state="readonly", values=["20", "50", "100", "200"])
        size_box.pack(side="left")
        size_box.bind("<<ComboboxSelected>>", lambda ev: self._mem_reload(reset_page=True))
        self.mem_db_var = tk.StringVar(value="")
        ttk.Label(page, textvariable=self.mem_db_var, font=FONT_SMALL,
                  foreground="#8a8f98").pack(side="right")

        self._mem_page_no = 0
        self._mem_refresh_categories()
        self._mem_reload(reset_page=True)

    def _hist(self) -> Memory:
        if self._hist_mem is None:
            self._hist_mem = Memory(self.cfg["memory"]["db_path"])
        return self._hist_mem

    def _mem_refresh_categories(self) -> None:
        try:
            cats = ["全部"] + [c for c in self._hist().categories() if c != "全部"]
        except Exception:  # noqa: BLE001
            cats = ["全部"]
        self.mem_cat_box.configure(values=cats)

    def _mem_imp(self) -> int:
        try:
            return int(self.mem_imp_var.get() or 0)
        except ValueError:
            return 0

    def _mem_size(self) -> int:
        try:
            return int(self.mem_size_var.get() or 50)
        except ValueError:
            return 50

    def _mem_reload(self, reset_page: bool = False) -> None:
        if reset_page:
            self._mem_page_no = 0
        try:
            mem = self._hist()
            kw = self.search_var.get().strip()
            cat = self.mem_cat_var.get().strip()
            total = mem.count_query(kw, cat, self._mem_imp())
            size = self._mem_size()
            pages = max(1, (total + size - 1) // size)
            self._mem_page_no = max(0, min(self._mem_page_no, pages - 1))
            rows = mem.query(kw, cat, self._mem_imp(),
                             offset=self._mem_page_no * size, limit=size)

            self.mem_tree.delete(*self.mem_tree.get_children())
            for r in rows:
                ts = time.strftime("%m-%d %H:%M", time.localtime(r["ts"]))
                who = "你" if r["role"] == "user" else "Firefly"
                tags = _safe_json_list(r.get("tags"))
                body = str(r["content"]).replace("\n", " ")
                if tags:
                    body = f"[{'/'.join(tags)}] " + body
                marks = [r["role"]]
                if int(r.get("importance") or 0) >= 8:
                    marks.append("important")
                self.mem_tree.insert(
                    "", "end", iid=str(r["id"]),
                    values=(ts, who, r.get("category") or "对话",
                            r.get("importance", 5), body),
                    tags=tuple(marks),
                )
            self.mem_detail.delete("1.0", "end")
            self.mem_page_var.set(f"第 {self._mem_page_no + 1} / {pages} 页")
            self.mem_count_var.set(f"命中 {total} 条｜库内共 {mem.count()} 条")
            self.mem_db_var.set(str(self.cfg["memory"]["db_path"]))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("读取失败", str(exc))

    def _mem_recent(self) -> None:
        self.search_var.set("")
        self.mem_cat_var.set("全部")
        self.mem_imp_var.set("0")
        self._mem_reload(reset_page=True)

    def _mem_reset_filter(self) -> None:
        self._mem_recent()

    def _mem_page(self, delta: int) -> None:
        self._mem_page_no = max(0, self._mem_page_no + delta)
        self._mem_reload()

    def _mem_selected_id(self) -> int | None:
        sel = self.mem_tree.selection()
        return int(sel[0]) if sel else None

    def _mem_show_detail(self) -> None:
        mid = self._mem_selected_id()
        if mid is None:
            return
        try:
            cur = self._hist().conn.cursor()
            cur.execute("SELECT role, content, ts, category, importance, tags "
                        "FROM messages WHERE id=?", (mid,))
            row = cur.fetchone()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("读取失败", str(exc))
            return
        self.mem_detail.delete("1.0", "end")
        if not row:
            return
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["ts"]))
        who = "你" if row["role"] == "user" else "Firefly"
        tags = _safe_json_list(row["tags"])
        head = (f"#{mid}　{ts}　{who}　分类：{row['category'] or '对话'}　"
                f"重要度：{row['importance']}　标签：{', '.join(tags) or '无'}\n"
                + "-" * 56 + "\n")
        self.mem_detail.insert("end", head)
        self.mem_detail.insert("end", str(row["content"]))

    def _mem_bump(self, delta: int) -> None:
        mid = self._mem_selected_id()
        if mid is None:
            messagebox.showinfo("提示", "请先在列表里选中一条记忆。")
            return
        try:
            mem = self._hist()
            cur = mem.conn.cursor()
            cur.execute("SELECT importance FROM messages WHERE id=?", (mid,))
            row = cur.fetchone()
            if not row:
                return
            mem.update_importance(mid, max(0, min(10, int(row["importance"] or 0) + delta)))
            self._mem_reload()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("修改失败", str(exc))

    def _mem_add_tag(self) -> None:
        mid = self._mem_selected_id()
        if mid is None:
            messagebox.showinfo("提示", "请先在列表里选中一条记忆。")
            return
        tag = simpledialog.askstring("加标签", "输入一个标签（例如 重要 / 待办 / 家人）：",
                                     parent=self.root)
        if not tag or not tag.strip():
            return
        try:
            self._hist().add_tag(mid, tag.strip())
            self._mem_reload()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("加标签失败", str(exc))

    def _mem_delete(self) -> None:
        mid = self._mem_selected_id()
        if mid is None:
            messagebox.showinfo("提示", "请先在列表里选中一条记忆。")
            return
        if not messagebox.askyesno("删除记忆",
                                   f"确定删除第 #{mid} 条记忆？此操作不可撤销。",
                                   icon="warning"):
            return
        try:
            self._hist().delete(mid)
            self._mem_reload()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("删除失败", str(exc))

    def _mem_export(self, fmt: str) -> None:
        kw = self.search_var.get().strip()
        cat = self.mem_cat_var.get().strip()
        path = filedialog.asksaveasfilename(
            title="导出记忆", defaultextension=f".{fmt}",
            initialfile=f"firefly_memory.{fmt}",
            filetypes=[("JSON", "*.json")] if fmt == "json" else [("CSV", "*.csv")])
        if not path:
            return
        try:
            rows = self._hist().query(kw, cat, self._mem_imp(), offset=0, limit=100000)
            if fmt == "json":
                Path(path).write_text(
                    json.dumps(rows, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8-sig")
            else:
                import csv
                with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                    writer = csv.writer(fh)
                    writer.writerow(["id", "时间", "角色", "分类", "重要度", "标签", "内容"])
                    for r in rows:
                        writer.writerow([
                            r["id"],
                            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])),
                            r["role"], r.get("category") or "",
                            r.get("importance") or 0,
                            "/".join(_safe_json_list(r.get("tags"))),
                            r["content"],
                        ])
            messagebox.showinfo("导出完成", f"已导出 {len(rows)} 条到：\n{path}")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("导出失败", str(exc))

    def _mem_cleanup(self) -> None:
        self._cleanup_expired()
        self._mem_reload()

    def _recent_rows(self, n: int) -> list[dict]:
        cur = self._hist().conn.cursor()
        cur.execute("SELECT id, role, content, ts FROM messages ORDER BY id DESC LIMIT ?", (n,))
        rows = [dict(r) for r in cur.fetchall()]
        rows.reverse()
        return rows

    # ============ ③ 情感（程序化情绪模型） ============
    def _build_emotion_tab(self) -> None:
        f = ttk.Frame(self.nb)
        self.nb.add(f, text=" 情感 ")
        # 情绪实例由 ChatWorker 统一持有（与对话共用），这里只负责显示与下发操作，
        # 避免界面和对话各持一份内存状态、互相看不见对方的改动。
        self._emo = None

        head = ttk.Frame(f)
        head.pack(fill="x", padx=10, pady=(10, 2))
        self.emo_title_var = tk.StringVar(value="")
        ttk.Label(head, textvariable=self.emo_title_var, font=FONT_BOLD).pack(side="left")
        self.emo_reason_var = tk.StringVar(value="")
        ttk.Label(head, textvariable=self.emo_reason_var, font=FONT_SMALL,
                  foreground="#8a8f98").pack(side="left", padx=10)

        # 生效状态行 + 跳转引导：关掉情绪后用户要知道"去哪开、什么时候生效"（审查报告 D7）
        strow = ttk.Frame(f)
        strow.pack(fill="x", padx=10, pady=(0, 4))
        self.emo_state_var = tk.StringVar(value="情绪模型：读取中…")
        self.emo_state_label = ttk.Label(strow, textvariable=self.emo_state_var,
                                         font=FONT_SMALL, foreground="#8a8f98")
        self.emo_state_label.pack(side="left")
        ttk.Button(strow, text="⚙ 去配置页改情绪设置",
                   command=self._goto_config_tab).pack(side="right")

        self.emo_canvas = tk.Canvas(f, height=112, highlightthickness=0, background="#fbfbf7")
        self.emo_canvas.pack(fill="x", padx=10, pady=(4, 4))

        row = ttk.Frame(f)
        row.pack(fill="x", padx=10, pady=(0, 4))
        ttk.Button(row, text="🔄 刷新", command=self._refresh_emotion).pack(side="left")
        ttk.Button(row, text="🔁 重置到基准", command=self._reset_emotion).pack(side="left", padx=6)
        ttk.Button(row, text="😊 开心一点", command=lambda: self._nudge_emotion(0.15, 0.10, 0)).pack(side="left")
        ttk.Button(row, text="🌙 安静一点", command=lambda: self._nudge_emotion(-0.05, -0.18, 0)).pack(side="left", padx=6)
        ttk.Button(row, text="💗 更亲近", command=lambda: self._nudge_emotion(0, 0, 0.05)).pack(side="left")
        ttk.Label(row, text="微调立刻影响对话播报的语气", font=FONT_SMALL,
                  foreground="#8a8f98").pack(side="left", padx=8)

        ttk.Label(f, text="将发给 MiMo-TTS 的风格指令（按官方规范放 role=user，可编辑后复制）",
                  font=FONT_BOLD).pack(anchor="w", padx=10, pady=(6, 2))
        self.emo_style_box = ScrolledText(f, height=7, font=FONT, wrap="word")
        self.emo_style_box.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        ttk.Label(f, text="将注入大模型上下文的心情（决定它「说什么」，与上面管「怎么念」是两件事）",
                  font=FONT_BOLD).pack(anchor="w", padx=10, pady=(2, 2))
        self.emo_ctx_box = ScrolledText(f, height=4, font=FONT, wrap="word")
        self.emo_ctx_box.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        ttk.Label(f, text="情绪变化（蓝＝愉悦度，绿＝唤醒度）", font=FONT_SMALL,
                  foreground="#8a8f98").pack(anchor="w", padx=10)
        self.emo_hist_canvas = tk.Canvas(f, height=88, highlightthickness=0, background="#fbfbf7")
        self.emo_hist_canvas.pack(fill="x", padx=10, pady=(2, 8))
        self.worker.submit("emotion_snapshot")  # 首屏向后台要一份真实状态

    def _draw_bar(self, c: "tk.Canvas", y: int, label: str, value: float,
                  lo: float, hi: float, color: str, fmt: str) -> None:
        width = max(360, c.winfo_width() or 600)
        left, right = 92, width - 58
        c.create_text(8, y + 8, text=label, anchor="w", font=FONT_SMALL, fill="#444441")
        c.create_rectangle(left, y, right, y + 16, outline="#d3d1c7", fill="#f1efe8")
        frac = max(0.0, min(1.0, (value - lo) / (hi - lo) if hi > lo else 0.0))
        x = left + (right - left) * frac
        if lo < 0 <= hi:  # 有零点：从中点画，红右绿左之外还是用单色更清楚
            zero = left + (right - left) * ((0 - lo) / (hi - lo))
            c.create_rectangle(min(zero, x), y, max(zero, x), y + 16, outline="", fill=color)
            c.create_line(zero, y - 3, zero, y + 19, fill="#888780")
        else:
            c.create_rectangle(left, y, x, y + 16, outline="", fill=color)
        c.create_text(right + 6, y + 8, text=fmt.format(value), anchor="w",
                      font=FONT_SMALL, fill="#2c2c2a")

    def _draw_emotion_history(self, rows: list[dict] | None = None) -> None:
        c = self.emo_hist_canvas
        c.delete("all")
        if rows is None:
            rows = []
        if len(rows) < 2:
            c.create_text(8, 42, anchor="w", text="（还看不出变化，多聊几句就会出现曲线）",
                          font=FONT_SMALL, fill="#8a8f98")
            return
        w, h, pad = max(360, c.winfo_width() or 600), 82, 8
        c.create_line(pad, h / 2, w - pad, h / 2, fill="#d3d1c7")

        def xy(i: int, val: float, lo: float, hi: float) -> tuple[float, float]:
            x = pad + (w - 2 * pad) * (i / max(1, len(rows) - 1))
            y = h - pad - (h - 2 * pad) * ((val - lo) / (hi - lo))
            return x, y

        for key, lo, hi, color in (("valence", -1.0, 1.0, "#378ADD"),
                                   ("arousal", 0.0, 1.0, "#1D9E75")):
            pts: list[float] = []
            for i, r in enumerate(rows):
                px, py = xy(i, float(r.get(key) or 0.0), lo, hi)
                pts.extend((px, py))
            if len(pts) >= 4:
                c.create_line(*pts, fill=color, width=1.5)

    def _refresh_emotion(self) -> None:
        """向后台要一份最新情绪快照（后台会重新读库，所以对话里的变化也能看到）。"""
        self.worker.submit("emotion_snapshot")

    def _render_emotion(self, payload: dict | None, error: str = "") -> None:
        if error:
            self.emo_title_var.set(f"读取情绪失败：{error}")
            return
        if not payload:
            return
        s = payload.get("snapshot") or {}
        self._emo_last = s          # 留给「重置到基准」弹窗显示 当前值→基准值
        enabled = bool(s.get("enabled", True))
        self.emo_title_var.set(f"当前情绪：{s.get('label', '?')}（{s.get('compound', '')}）"
                               f"｜累计 {s.get('turns', 0)} 轮")
        self.emo_reason_var.set(s.get("reason") or "")
        if enabled:
            extra = "" if s.get("inject_to_context", True) else "（只影响语音语气，未注入大模型）"
            self.emo_state_var.set(f"情绪模型：已启用{extra}｜手动微调与配置改动即刻生效")
            self.emo_state_label.configure(foreground="#1f7a3d")
        else:
            self.emo_title_var.set("当前情绪：—（情绪模型已关闭）")
            self.emo_state_var.set("情绪模型：已关闭——语音与文字都不再带情绪。"
                                   "点右侧按钮去「配置」页打开，再点「保存配置」即刻生效。")
            self.emo_state_label.configure(foreground="#b23b3b")
        c = self.emo_canvas
        c.delete("all")
        self._draw_bar(c, 4, "愉悦度", float(s.get("valence", 0.0)), -1.0, 1.0, "#378ADD", "{:+.2f}")
        self._draw_bar(c, 42, "唤醒度", float(s.get("arousal", 0.0)), 0.0, 1.0, "#1D9E75", "{:.2f}")
        self._draw_bar(c, 80, "亲密度", float(s.get("intimacy", 0.0)), 0.0, 1.0, "#BA7517", "{:.2f}")
        self.emo_style_box.delete("1.0", "end")
        self.emo_style_box.insert("1.0", s.get("style_preview", ""))
        self.emo_ctx_box.delete("1.0", "end")
        self.emo_ctx_box.insert("1.0", s.get("context_preview")
                                or "（已关闭「把此刻心情注入大模型」，情绪只影响语音语气）")
        self._draw_emotion_history(payload.get("history") or [])

    def _reset_emotion(self) -> None:
        """重置到基准值——**会连累积的亲密度一起丢掉**，所以必须先确认。"""
        s = getattr(self, "_emo_last", None) or {}
        base = s.get("baseline") or {}
        if s:
            detail = (f"当前：愉悦度 {float(s.get('valence', 0)):+.2f}｜"
                      f"唤醒度 {float(s.get('arousal', 0)):.2f}｜"
                      f"亲密度 {float(s.get('intimacy', 0)):.2f}\n"
                      f"重置为：愉悦度 {float(base.get('valence', 0)):+.2f}｜"
                      f"唤醒度 {float(base.get('arousal', 0)):.2f}｜"
                      f"亲密度 {float(base.get('intimacy', 0)):.2f}\n\n"
                      "亲密度是长期聊天一点点攒起来的，重置后会一起归零到基准，且无法撤销。")
        else:
            detail = "会把三维情绪全部恢复成基准值，无法撤销。"
        if not messagebox.askyesno("重置到基准值", f"{detail}\n\n确定重置吗？", icon="warning"):
            return
        self.worker.submit("emotion_nudge", "reset", 0.0, 0.0, 0.0)

    def _nudge_emotion(self, dv: float, da: float, di: float) -> None:
        self.worker.submit("emotion_nudge", "nudge", dv, da, di)

    def _goto_config_tab(self) -> None:
        """跳到「配置」页（情绪开关、注入开关都在那里）——D7 的跳转引导。"""
        try:
            for i in range(self.nb.index("end")):
                if "配置" in str(self.nb.tab(i, "text")):
                    self.nb.select(i)
                    return
        except Exception:  # noqa: BLE001
            pass

    def _refresh_emo_effect_hint(self) -> None:
        """情绪开关的生效时机提示（D7）：明确告诉用户"改完什么时候生效、以什么方式生效"。"""
        try:
            if bool(self.emo_enabled_var.get()):
                self.emo_effect_var.set(
                    "生效方式：勾上后，语音语气与文字措辞都会跟着情绪走。"
                    "改完点下方「保存配置」即刻生效（会重载大脑，无需重启程序）。")
                self.emo_effect_label.configure(foreground="#1f7a3d")
            else:
                self.emo_effect_var.set(
                    "生效方式：取消勾选后，语音与文字都不再带情绪，情感页会显示「已关闭」。"
                    "点下方「保存配置」即刻生效。")
                self.emo_effect_label.configure(foreground="#b23b3b")
        except Exception:  # noqa: BLE001
            pass

    # ============ ④ 配置 ============
    # 配置页里「改了必须点保存才生效」的字段：(人话名称, 界面变量属性名)。
    # 只登记界面变量本身、不做配置键映射——避免和 _save_config 的映射各写一份、日久走样。
    # 刻意排除：API Key（只写不回显）、开机自启（本来就是即时生效、有自己的确认）。
    _CFG_WATCH = (
        ("大脑模型", "model_var"),
        ("大脑接口地址", "baseurl_var"),
        ("TTS 接口地址", "tts_baseurl_var"),
        ("合成方式", "tts_model_var"),
        ("音色", "voice_var"),
        ("音色描述/风格指令", "voice_instruction_var"),
        ("参考音频", "ref_audio_var"),
        ("ASR 服务商", "asr_provider_var"),
        ("ASR 接口地址", "asr_baseurl_var"),
        ("ASR 模型", "asr_model_var"),
        ("ASR 语言", "asr_lang_var"),
        ("性格随机度", "temp_var"),
        ("唤醒词", "keyword_var"),
        ("录音静音阈值", "thresh_var"),
        ("说完停顿判定", "tail_var"),
        ("允许语音打断", "barge_var"),
        ("桌宠开关", "pet_var"),
        ("桌宠演示模式", "pet_demo_var"),
        ("桌宠大小", "pet_scale_var"),
        ("桌宠不透明度", "pet_opacity_var"),
        ("桌宠初始位置X", "pet_x_var"),
        ("桌宠初始位置Y", "pet_y_var"),
        ("每轮召回往事条数", "recall_k_var"),
        ("携带对话轮数", "hist_turns_var"),
        ("重要记忆常驻", "pin_var"),
        ("常驻记忆门槛", "pin_min_var"),
        ("常驻记忆条数", "pin_limit_var"),
        ("启用情绪模型", "emo_enabled_var"),
        ("情绪用大模型推断", "emo_llm_var"),
        ("情绪风格模式", "emo_mode_var"),
        ("情绪基准·愉悦度", "emo_base_v_var"),
        ("情绪基准·唤醒度", "emo_base_a_var"),
        ("情绪基准·亲密度", "emo_base_i_var"),
        ("情绪每小时平复", "emo_decay_var"),
        ("情绪注入上下文", "emo_inject_var"),
        ("情绪回复前预判", "emo_prehint_var"),
        ("允许 /pi 调用", "pi_enabled_var"),
        ("Pi 只读模式", "pi_ro_var"),
        ("Pi 超时", "pi_timeout_var"),
        ("Pi 输出模式", "pi_mode_var"),
        ("pi 路径", "pi_cli_var"),
        ("Pi 工作目录", "pi_cwd_var"),
    )

    def _cfg_watch_items(self) -> list[tuple[str, object]]:
        out: list[tuple[str, object]] = []
        for label, attr in self._CFG_WATCH:
            var = getattr(self, attr, None)
            if var is not None:
                out.append((label, var))
        return out

    def _ui_cfg_snapshot(self) -> dict:
        """当前界面上的配置值快照（全部取字符串，比较稳定、不受类型影响）。"""
        return {label: str(var.get()) for label, var in self._cfg_watch_items()}

    def _cfg_dirty_changes(self) -> list[str]:
        """哪些配置字段改了但还没点「保存配置」（防止"以为改了其实没生效"）。"""
        snap = getattr(self, "_cfg_snapshot", None)
        if snap is None:
            return []
        now = self._ui_cfg_snapshot()
        return [label for label, val in now.items() if snap.get(label) != val]

    def _persona_dirty(self) -> bool:
        """人设文本框里是否有未点「保存人设」的修改。"""
        try:
            text = self.persona_box.get("1.0", "end").rstrip() + "\n"
            p = Path(self.cfg.get("persona_path", ""))
            return p.exists() and p.read_text(encoding="utf-8") != text
        except Exception:  # noqa: BLE001
            return False

    def _refresh_cfg_dirty_hint(self) -> None:
        try:
            changed = self._cfg_dirty_changes()
            self.cfg_dirty_var.set(
                f"● 有 {len(changed)} 项修改未保存，点右边「保存配置」才生效" if changed else "")
        except Exception:  # noqa: BLE001
            pass

    def _on_tab_changed(self, event=None) -> None:
        """标签页切换时的处理：更新脏数据提示 + 管理鼠标滚轮绑定。"""
        self._refresh_cfg_dirty_hint()
        # 管理鼠标滚轮绑定：只在配置页时启用
        if hasattr(self, '_config_canvas') and hasattr(self, '_config_mousewheel_handler'):
            current_tab = self.nb.select()
            config_tab = str(self._config_canvas.master)  # canvas 的父级就是 f
            # 通过比较 tab id 来判断是否在配置页
            try:
                tab_text = self.nb.tab(current_tab, "text").strip()
                if tab_text == "配置":
                    # 在配置页，绑定滚轮
                    self._config_canvas.bind_all("<MouseWheel>", self._config_mousewheel_handler)
                else:
                    # 不在配置页，解绑滚轮
                    self._config_canvas.unbind_all("<MouseWheel>")
            except Exception:
                pass

    def _build_config_tab(self) -> None:
        f = ttk.Frame(self.nb)
        self.nb.add(f, text=" 配置 ")

        # 底部固定按钮栏（始终可见，不随内容滚动）
        btns = ttk.Frame(f)
        btns.pack(side="bottom", fill="x", padx=10, pady=(0, 10))
        self.cfg_dirty_var = tk.StringVar(value="")
        ttk.Label(btns, textvariable=self.cfg_dirty_var, font=FONT_SMALL,
                  foreground="#b23b3b").pack(side="left")
        ttk.Button(btns, text="保存人设", command=self._save_persona).pack(side="right")
        ttk.Button(btns, text="保存配置（并重载大脑）", command=self._save_config).pack(side="right", padx=8)

        # 创建 Canvas 和 Scrollbar 实现滚动功能
        canvas = tk.Canvas(f, highlightthickness=0)
        scrollbar = ttk.Scrollbar(f, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)

        # 放置 Canvas 和 Scrollbar
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="top", fill="both", expand=True)

        # 创建内部框架
        wrap = ttk.Frame(canvas)
        canvas_window = canvas.create_window((0, 0), window=wrap, anchor="nw", tags="inner")

        # 配置滚动区域
        wrap.columnconfigure(1, weight=1)
        row = {"n": 0}

        # 当内部框架大小改变时更新滚动区域
        def on_frame_configure(event):
            canvas.configure(scrollregion=canvas.bbox("all"))

        wrap.bind("<Configure>", on_frame_configure)

        # 当 Canvas 大小改变时调整内部框架宽度
        def on_canvas_configure(event):
            canvas.itemconfig(canvas_window, width=event.width)

        canvas.bind("<Configure>", on_canvas_configure)

        # 绑定鼠标滚轮事件
        def on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        # 存储 canvas 引用以便后续清理
        self._config_canvas = canvas
        self._config_mousewheel_handler = on_mousewheel

        # 初始绑定（如果当前在配置页）
        try:
            current_tab_text = self.nb.tab(self.nb.select(), "text").strip()
            if current_tab_text == "配置":
                canvas.bind_all("<MouseWheel>", on_mousewheel)
        except Exception:
            pass

        def next_row() -> int:
            row["n"] += 1
            return row["n"]

        def label(text: str, **kw) -> None:
            ttk.Label(wrap, text=text, **kw).grid(row=next_row(), column=0,
                                                  sticky="e", padx=(0, 6), pady=3)

        mimo = self.cfg.get("mimo", {})
        llm = self.cfg.get("llm", {})

        # ========== 共用 API Key（小米，可选） ==========
        label("共用 API Key（小米 tp-…）", font=FONT)
        self.key_var = tk.StringVar(value="")  # 安全：绝不回显已存的 Key
        ttk.Entry(wrap, textvariable=self.key_var, width=46, show="•").grid(
            row=row["n"], column=1, sticky="w", pady=3)
        has_key = "已配置 ✓（输入新值可更换，留空保持不变）" if mimo.get("api_key") else "尚未配置"
        ttk.Label(wrap, text=has_key, font=FONT_SMALL, foreground="#8a8f98").grid(
            row=row["n"], column=2, sticky="w", padx=6)
        key_btn_frame = ttk.Frame(wrap)
        key_btn_frame.grid(row=next_row(), column=1, columnspan=2, sticky="w", pady=(0, 4))
        ttk.Button(key_btn_frame, text="💾 保存共用 Key",
                   command=self._save_api_key).pack(side="left")
        self.key_status_var = tk.StringVar(value="")
        ttk.Label(key_btn_frame, textvariable=self.key_status_var,
                  font=FONT_SMALL, foreground="#8a8f98").pack(side="left", padx=8)
        ttk.Label(key_btn_frame, text="LLM/TTS/ASR 各自没填 Key 时自动复用这个",
                  font=FONT_SMALL, foreground="#8a8f98").pack(side="left", padx=8)

        # ========== LLM（大脑模型） ==========
        llm_box = ttk.LabelFrame(wrap, text=" LLM（大脑模型） ")
        llm_box.grid(row=next_row(), column=0, columnspan=3, sticky="we", pady=(10, 4))

        llm_row0 = ttk.Frame(llm_box)
        llm_row0.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Label(llm_row0, text="API Key", font=FONT).pack(side="left")
        self.llm_key_var = tk.StringVar(value="")
        ttk.Entry(llm_row0, textvariable=self.llm_key_var, width=36, show="•").pack(
            side="left", padx=(4, 8))
        llm_has_key = "已配置 ✓（留空复用共用 Key）" if llm.get("api_key") else "留空 → 复用共用 Key"
        self.llm_key_hint = ttk.Label(llm_row0, text=llm_has_key,
                                      font=FONT_SMALL, foreground="#8a8f98")
        self.llm_key_hint.pack(side="left")

        llm_row1 = ttk.Frame(llm_box)
        llm_row1.pack(fill="x", padx=8, pady=2)
        ttk.Label(llm_row1, text="模型", font=FONT).pack(side="left")
        self.model_var = tk.StringVar(value=llm.get("model", "mimo-v2.5"))
        ttk.Combobox(llm_row1, textvariable=self.model_var, width=28,
                     values=["mimo-v2.5", "mimo-v2.5-pro"]).pack(side="left", padx=(4, 8))
        ttk.Label(llm_row1, text="mimo-v2.5=快速日常 / mimo-v2.5-pro=更聪明但更慢",
                  font=FONT_SMALL, foreground="#8a8f98").pack(side="left")

        llm_row2 = ttk.Frame(llm_box)
        llm_row2.pack(fill="x", padx=8, pady=2)
        ttk.Label(llm_row2, text="接口地址", font=FONT).pack(side="left")
        self.baseurl_var = tk.StringVar(value=llm.get("base_url", ""))
        ttk.Entry(llm_row2, textvariable=self.baseurl_var, width=42).pack(
            side="left", padx=(4, 8))
        ttk.Label(llm_row2, text="留空=使用共用地址", font=FONT_SMALL,
                  foreground="#8a8f98").pack(side="left")

        llm_row3 = ttk.Frame(llm_box)
        llm_row3.pack(fill="x", padx=8, pady=(4, 6))
        ttk.Button(llm_row3, text="💾 保存 LLM 设置",
                   command=self._save_llm_settings).pack(side="left")
        self.llm_status_var = tk.StringVar(value="")
        ttk.Label(llm_row3, textvariable=self.llm_status_var,
                  font=FONT_SMALL, foreground="#8a8f98").pack(side="left", padx=8)

        # ========== TTS（语音合成） ==========
        vbox = ttk.LabelFrame(wrap, text=" TTS（语音合成） ")
        vbox.grid(row=next_row(), column=0, columnspan=3, sticky="we", pady=(10, 4))

        tts = self.cfg.get("tts", {})
        vrow_key = ttk.Frame(vbox)
        vrow_key.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Label(vrow_key, text="API Key", font=FONT).pack(side="left")
        self.tts_key_var = tk.StringVar(value="")
        ttk.Entry(vrow_key, textvariable=self.tts_key_var, width=30, show="•").pack(
            side="left", padx=(4, 8))
        tts_has_key = "已配置 ✓（留空复用共用 Key）" if tts.get("api_key") else "留空 → 复用共用 Key"
        self.tts_key_hint = ttk.Label(vrow_key, text=tts_has_key,
                                      font=FONT_SMALL, foreground="#8a8f98")
        self.tts_key_hint.pack(side="left")

        vrow_url = ttk.Frame(vbox)
        vrow_url.pack(fill="x", padx=8, pady=2)
        ttk.Label(vrow_url, text="接口地址", font=FONT).pack(side="left")
        self.tts_baseurl_var = tk.StringVar(value=tts.get("base_url", ""))
        ttk.Entry(vrow_url, textvariable=self.tts_baseurl_var, width=38).pack(
            side="left", padx=(4, 8))
        ttk.Label(vrow_url, text="留空=使用共用地址；TTS 不可用时可单独改",
                  font=FONT_SMALL, foreground="#8a8f98").pack(side="left")

        TTS_MODEL_DISPLAY = {
            "mimo-v2.5-tts": "预置音色（官方精品声音，开箱即用）",
            "mimo-v2.5-tts-voicedesign": "音色设计（用文字描述，定制一个声音）",
            "mimo-v2.5-tts-voiceclone": "声音克隆（用一段录音，复刻你的声音）",
        }
        self._tts_model_display = TTS_MODEL_DISPLAY

        vrow1 = ttk.Frame(vbox)
        vrow1.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Label(vrow1, text="合成方式", font=FONT).pack(side="left")
        cur_model = self.cfg.get("tts", {}).get("model", "mimo-v2.5-tts")
        self.tts_model_var = tk.StringVar(value=TTS_MODEL_DISPLAY.get(cur_model, cur_model))
        ttk.Combobox(vrow1, textvariable=self.tts_model_var, width=34, state="readonly",
                     values=list(TTS_MODEL_DISPLAY.values())).pack(side="left", padx=(4, 8))
        self.tts_model_hint_var = tk.StringVar()
        ttk.Label(vrow1, textvariable=self.tts_model_hint_var, font=FONT_SMALL,
                  foreground="#8a8f98").pack(side="left")

        vrow2 = ttk.Frame(vbox)
        vrow2.pack(fill="x", padx=8, pady=2)
        self.voice_label = ttk.Label(vrow2, text="音色", font=FONT)
        self.voice_label.pack(side="left")
        cur_voice = self.cfg.get("tts", {}).get("voice") or mimo.get("voice", "mimo_default")
        self.voice_var = tk.StringVar(value=voice_display_for_id(cur_voice))
        self.voice_box = ttk.Combobox(vrow2, textvariable=self.voice_var, width=28,
                                      values=[d for d, _ in voice_choices()])
        self.voice_box.pack(side="left", padx=(4, 8))
        self.voice_hint = ttk.Label(vrow2, text="", font=FONT_SMALL, foreground="#8a8f98")
        self.voice_hint.pack(side="left")

        vrow3 = ttk.Frame(vbox)
        vrow3.pack(fill="x", padx=8, pady=2)
        self.voice_instr_label = ttk.Label(vrow3, text="音色描述", font=FONT)
        self.voice_instr_label.pack(side="left")
        self.voice_instruction_var = tk.StringVar(
            value=self.cfg.get("tts", {}).get("voice_instruction", ""))
        self.voice_instr_entry = ttk.Entry(vrow3, textvariable=self.voice_instruction_var,
                                           width=42)
        self.voice_instr_entry.pack(side="left", padx=(4, 8))
        self.voice_instr_hint = ttk.Label(vrow3, text="", font=FONT_SMALL,
                                          foreground="#8a8f98")
        self.voice_instr_hint.pack(side="left")

        vrow4 = ttk.Frame(vbox)
        vrow4.pack(fill="x", padx=8, pady=2)
        self.ref_label = ttk.Label(vrow4, text="参考音频", font=FONT)
        self.ref_label.pack(side="left")
        self.ref_audio_var = tk.StringVar(
            value=self.cfg.get("tts", {}).get("reference_audio_path", ""))
        self.ref_entry = ttk.Entry(vrow4, textvariable=self.ref_audio_var, width=34)
        self.ref_entry.pack(side="left", padx=(4, 4))
        self.btn_ref_browse = ttk.Button(vrow4, text="浏览…", command=self._browse_ref_audio)
        self.btn_ref_browse.pack(side="left")
        self.btn_ref_record = ttk.Button(vrow4, text="🎙 现场录制",
                                        command=self._record_ref_audio)
        self.btn_ref_record.pack(side="left", padx=(4, 8))
        self.ref_hint = ttk.Label(vrow4, text="", font=FONT_SMALL, foreground="#8a8f98")
        self.ref_hint.pack(side="left")

        vrow5 = ttk.Frame(vbox)
        vrow5.pack(fill="x", padx=8, pady=(4, 2))
        self.btn_voice_test = ttk.Button(vrow5, text="🔊 试听当前声音", command=self._test_voice)
        self.btn_voice_test.pack(side="left")
        self.btn_ref_validate = ttk.Button(vrow5, text="✔ 校验参考音频",
                                          command=self._validate_ref_audio)
        self.btn_ref_validate.pack(side="left", padx=6)
        ttk.Button(vrow5, text="💾 保存 TTS 设置", command=self._save_voice_settings).pack(side="left")

        vrow6 = ttk.Frame(vbox)
        vrow6.pack(fill="x", padx=8, pady=(0, 6))
        self.voice_status_var = tk.StringVar(value="（改完先「试听」确认效果，满意后点「保存 TTS 设置」）")
        self.voice_status_label = ttk.Label(vrow6, textvariable=self.voice_status_var,
                                            font=FONT_SMALL, foreground="#8a8f98")
        self.voice_status_label.pack(side="left")

        self.tts_model_var.trace_add("write", self._on_tts_model_change)
        self._on_tts_model_change()

        # ========== ASR（语音识别） ==========
        asr_cfg = self.cfg.get("asr", {})
        abox = ttk.LabelFrame(wrap, text=" ASR（语音识别） ")
        abox.grid(row=next_row(), column=0, columnspan=3, sticky="we", pady=(10, 4))

        arow0 = ttk.Frame(abox)
        arow0.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Label(arow0, text="API Key", font=FONT).pack(side="left")
        self.asr_key_var = tk.StringVar(value="")
        ttk.Entry(arow0, textvariable=self.asr_key_var, width=30, show="•").pack(
            side="left", padx=(4, 8))
        asr_has_key = "已配置 ✓（留空复用共用 Key）" if asr_cfg.get("api_key") else "留空 → 复用共用 Key"
        self.asr_key_hint = ttk.Label(arow0, text=asr_has_key,
                                      font=FONT_SMALL, foreground="#8a8f98")
        self.asr_key_hint.pack(side="left")

        arow1 = ttk.Frame(abox)
        arow1.pack(fill="x", padx=8, pady=2)
        ttk.Label(arow1, text="服务商", font=FONT).pack(side="left")
        self.asr_provider_var = tk.StringVar(value=asr_cfg.get("provider", "mimo"))
        ttk.Combobox(arow1, textvariable=self.asr_provider_var, width=28, state="readonly",
                     values=["mimo", "whisper_api"]).pack(side="left", padx=(4, 8))
        ttk.Label(arow1, text="mimo=小米 ASR｜whisper_api=OpenAI 兼容接口",
                  font=FONT_SMALL, foreground="#8a8f98").pack(side="left")

        arow2 = ttk.Frame(abox)
        arow2.pack(fill="x", padx=8, pady=2)
        ttk.Label(arow2, text="接口地址", font=FONT).pack(side="left")
        self.asr_baseurl_var = tk.StringVar(value=asr_cfg.get("base_url", ""))
        ttk.Entry(arow2, textvariable=self.asr_baseurl_var, width=38).pack(
            side="left", padx=(4, 8))
        ttk.Label(arow2, text="留空=使用共用地址", font=FONT_SMALL,
                  foreground="#8a8f98").pack(side="left")

        arow3 = ttk.Frame(abox)
        arow3.pack(fill="x", padx=8, pady=2)
        ttk.Label(arow3, text="模型", font=FONT).pack(side="left")
        self.asr_model_var = tk.StringVar(value=asr_cfg.get("model", "mimo-v2.5-asr"))
        ttk.Entry(arow3, textvariable=self.asr_model_var, width=28).pack(
            side="left", padx=(4, 8))
        ttk.Label(arow3, text="mimo-v2.5-asr / whisper-1 / 其它兼容模型",
                  font=FONT_SMALL, foreground="#8a8f98").pack(side="left")

        arow4 = ttk.Frame(abox)
        arow4.pack(fill="x", padx=8, pady=2)
        ttk.Label(arow4, text="语言", font=FONT).pack(side="left")
        self.asr_lang_var = tk.StringVar(value=asr_cfg.get("language", "auto"))
        ttk.Combobox(arow4, textvariable=self.asr_lang_var, width=10, state="readonly",
                     values=["auto", "zh", "en", "ja"]).pack(side="left", padx=(4, 8))
        ttk.Label(arow4, text="auto=自动检测 / zh=中文 / en=英文 / ja=日文",
                  font=FONT_SMALL, foreground="#8a8f98").pack(side="left")

        arow5 = ttk.Frame(abox)
        arow5.pack(fill="x", padx=8, pady=(4, 6))
        ttk.Button(arow5, text="💾 保存 ASR 设置",
                   command=self._save_asr_settings).pack(side="left")
        self.asr_status_var = tk.StringVar(value="")
        ttk.Label(arow5, textvariable=self.asr_status_var,
                  font=FONT_SMALL, foreground="#8a8f98").pack(side="left", padx=8)

        label("性格随机度 temperature", font=FONT)
        self.temp_var = tk.StringVar(value=str(llm.get("temperature", 0.9)))
        ttk.Entry(wrap, textvariable=self.temp_var, width=46).grid(
            row=row["n"], column=1, sticky="w", pady=3)
        ttk.Label(wrap, text="0~1，越大越发散", font=FONT_SMALL,
                  foreground="#8a8f98").grid(row=row["n"], column=2, sticky="w", padx=6)

        label("唤醒词", font=FONT)
        self.keyword_var = tk.StringVar(value=self.cfg.get("wake", {}).get("keyword", "Hi Firefly"))
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

        self.autostart_var = tk.BooleanVar(value=autostart_enabled())
        ttk.Checkbutton(wrap, text="开机自动启动助手", variable=self.autostart_var,
                        command=self._toggle_autostart).grid(row=next_row(), column=1, sticky="w", pady=3)

        # ---------- 桌宠（小萤火虫） ----------
        from core.pet import resolve_pet_options as _pet_opts

        _po = _pet_opts(self.cfg)
        ttk.Label(wrap, text="桌宠（小萤火虫）", font=FONT_BOLD).grid(
            row=next_row(), column=0, columnspan=3, sticky="w", pady=(12, 2))

        petrow1 = ttk.Frame(wrap)
        petrow1.grid(row=next_row(), column=0, columnspan=3, sticky="we")
        self.pet_var = tk.BooleanVar(value=_po["enabled"])
        ttk.Checkbutton(petrow1, text="启动程序时自动显示桌宠（控制台 / 语音 / 键盘模式都遵守）",
                        variable=self.pet_var).pack(side="left")
        self.pet_demo_var = tk.BooleanVar(value=_po["demo"])
        ttk.Checkbutton(petrow1, text="演示模式（循环展示四种状态）",
                        variable=self.pet_demo_var).pack(side="left", padx=10)

        petrow2 = ttk.Frame(wrap)
        petrow2.grid(row=next_row(), column=0, columnspan=3, sticky="we", pady=(2, 3))
        ttk.Label(petrow2, text="大小", font=FONT).pack(side="left")
        self.pet_scale_var = tk.StringVar(value=f"{_po['scale']:g}")
        ttk.Combobox(petrow2, textvariable=self.pet_scale_var, width=5, state="readonly",
                     values=["0.6", "0.8", "1.0", "1.2", "1.5", "2.0"]).pack(side="left", padx=(2, 10))
        ttk.Label(petrow2, text="不透明度", font=FONT).pack(side="left")
        self.pet_opacity_var = tk.StringVar(value=f"{_po['opacity']:g}")
        ttk.Combobox(petrow2, textvariable=self.pet_opacity_var, width=5, state="readonly",
                     values=["0.5", "0.7", "0.85", "1.0"]).pack(side="left", padx=(2, 10))
        ttk.Label(petrow2, text="初始位置 X", font=FONT).pack(side="left")
        self.pet_x_var = tk.StringVar(value="" if _po["start_x"] is None else str(_po["start_x"]))
        ttk.Entry(petrow2, textvariable=self.pet_x_var, width=6).pack(side="left", padx=(2, 6))
        ttk.Label(petrow2, text="Y", font=FONT).pack(side="left")
        self.pet_y_var = tk.StringVar(value="" if _po["start_y"] is None else str(_po["start_y"]))
        ttk.Entry(petrow2, textvariable=self.pet_y_var, width=6).pack(side="left", padx=(2, 6))
        ttk.Label(petrow2, text="留空=屏幕右下角", font=FONT_SMALL,
                  foreground="#8a8f98").pack(side="left")

        petrow3 = ttk.Frame(wrap)
        petrow3.grid(row=next_row(), column=0, columnspan=3, sticky="we", pady=(0, 3))
        ttk.Button(petrow3, text="🐾 重启桌宠（按上方设置立刻生效）",
                   command=self._restart_pet).pack(side="left")
        ttk.Button(petrow3, text="👀 预览四种状态", command=self._preview_pet).pack(side="left", padx=8)
        ttk.Label(petrow3, text="重启只临时生效；点「保存配置」才会写入 config.json",
                  font=FONT_SMALL, foreground="#8a8f98").pack(side="left", padx=4)

        # ---------- 记忆与上下文注入 ----------
        ttk.Label(wrap, text="记忆与上下文注入（每轮实际发给大模型多少东西）", font=FONT_BOLD).grid(
            row=next_row(), column=0, columnspan=3, sticky="w", pady=(12, 2))
        m_cfg = self.cfg.get("memory", {}) or {}

        mrow1 = ttk.Frame(wrap)
        mrow1.grid(row=next_row(), column=0, columnspan=3, sticky="we")
        ttk.Label(mrow1, text="每轮召回往事", font=FONT).pack(side="left")
        self.recall_k_var = tk.StringVar(value=str(m_cfg.get("recall_top_k", 5)))
        ttk.Spinbox(mrow1, from_=1, to=15, width=4,
                    textvariable=self.recall_k_var).pack(side="left", padx=(2, 4))
        ttk.Label(mrow1, text="条", font=FONT).pack(side="left")
        ttk.Label(mrow1, text="（1~15，越大记得越多但更费 token）", font=FONT_SMALL,
                  foreground="#8a8f98").pack(side="left", padx=(2, 14))
        ttk.Label(mrow1, text="携带对话轮数", font=FONT).pack(side="left")
        self.hist_turns_var = tk.StringVar(
            value=str((self.cfg.get("llm", {}) or {}).get("max_history_turns", 20)))
        ttk.Spinbox(mrow1, from_=5, to=50, width=4,
                    textvariable=self.hist_turns_var).pack(side="left", padx=(2, 4))
        ttk.Label(mrow1, text="（5~50，越大越能接上文）", font=FONT_SMALL,
                  foreground="#8a8f98").pack(side="left", padx=2)

        mrow2 = ttk.Frame(wrap)
        mrow2.grid(row=next_row(), column=0, columnspan=3, sticky="we", pady=(2, 3))
        self.pin_var = tk.BooleanVar(value=bool(m_cfg.get("pin_important", True)))
        ttk.Checkbutton(mrow2, text="重要记忆常驻注入",
                        variable=self.pin_var).pack(side="left")
        ttk.Label(mrow2, text="重要度 ≥", font=FONT).pack(side="left", padx=(4, 2))
        self.pin_min_var = tk.StringVar(value=str(m_cfg.get("pin_min_importance", 8)))
        ttk.Spinbox(mrow2, from_=0, to=10, width=4,
                    textvariable=self.pin_min_var).pack(side="left")
        ttk.Label(mrow2, text="分，最多", font=FONT).pack(side="left", padx=(4, 2))
        self.pin_limit_var = tk.StringVar(value=str(m_cfg.get("pin_limit", 5)))
        ttk.Spinbox(mrow2, from_=1, to=10, width=4,
                    textvariable=self.pin_limit_var).pack(side="left")
        ttk.Label(mrow2, text="条（不命中关键词也会带上）", font=FONT_SMALL,
                  foreground="#8a8f98").pack(side="left", padx=4)

        mrow3 = ttk.Frame(wrap)
        mrow3.grid(row=next_row(), column=0, columnspan=3, sticky="we", pady=(0, 3))
        ttk.Label(mrow3, text="召回按「关键词相关度 + 重要度 + 分类」综合排序——"
                              "所以记忆页里把某条调到高分、归成「待办/笔记」，"
                              "真的会影响它出现在对话里的机会。改完点「保存配置」。",
                  font=FONT_SMALL, foreground="#8a8f98").pack(side="left")

        # ---------- 情绪模型 ----------
        ttk.Label(wrap, text="情绪模型（程序化）", font=FONT_BOLD).grid(
            row=next_row(), column=0, columnspan=3, sticky="w", pady=(12, 2))
        e_cfg = self.cfg.get("emotion", {}) or {}
        erow = ttk.Frame(wrap)
        erow.grid(row=next_row(), column=0, columnspan=3, sticky="we")
        self.emo_enabled_var = tk.BooleanVar(value=bool(e_cfg.get("enabled", True)))
        ttk.Checkbutton(erow, text="启用情绪模型", variable=self.emo_enabled_var).pack(side="left")
        self.emo_llm_var = tk.BooleanVar(value=bool(e_cfg.get("infer_with_llm", True)))
        ttk.Checkbutton(erow, text="用大模型推断情绪（关掉更省钱）",
                        variable=self.emo_llm_var).pack(side="left", padx=10)
        ttk.Label(erow, text="风格模式", font=FONT).pack(side="left", padx=(10, 2))
        self.emo_mode_var = tk.StringVar(value=str(e_cfg.get("style_mode", "director")))
        ttk.Combobox(erow, textvariable=self.emo_mode_var, width=10, state="readonly",
                     values=["director", "brief"]).pack(side="left")
        ttk.Label(erow, text="director=导演模式(角色/场景/指导)｜brief=一句话",
                  font=FONT_SMALL, foreground="#8a8f98").pack(side="left", padx=8)

        erow2 = ttk.Frame(wrap)
        erow2.grid(row=next_row(), column=0, columnspan=3, sticky="we", pady=(2, 3))
        e_base = e_cfg.get("baseline", {}) or {}
        ttk.Label(erow2, text="基准  愉悦度", font=FONT).pack(side="left")
        self.emo_base_v_var = tk.StringVar(value=str(e_base.get("valence", 0.25)))
        ttk.Entry(erow2, textvariable=self.emo_base_v_var, width=6).pack(side="left", padx=(2, 10))
        ttk.Label(erow2, text="唤醒度", font=FONT).pack(side="left")
        self.emo_base_a_var = tk.StringVar(value=str(e_base.get("arousal", 0.45)))
        ttk.Entry(erow2, textvariable=self.emo_base_a_var, width=6).pack(side="left", padx=(2, 10))
        ttk.Label(erow2, text="亲密度", font=FONT).pack(side="left")
        self.emo_base_i_var = tk.StringVar(value=str(e_base.get("intimacy", 0.3)))
        ttk.Entry(erow2, textvariable=self.emo_base_i_var, width=6).pack(side="left", padx=(2, 14))
        ttk.Label(erow2, text="每小时平复", font=FONT).pack(side="left")
        self.emo_decay_var = tk.StringVar(value=str(e_cfg.get("decay_per_hour", 0.12)))
        ttk.Entry(erow2, textvariable=self.emo_decay_var, width=6).pack(side="left", padx=(2, 4))
        ttk.Label(erow2, text="0~1，越大越快平静", font=FONT_SMALL,
                  foreground="#8a8f98").pack(side="left")

        erow3 = ttk.Frame(wrap)
        erow3.grid(row=next_row(), column=0, columnspan=3, sticky="we", pady=(0, 3))
        self.emo_inject_var = tk.BooleanVar(value=bool(e_cfg.get("inject_to_context", True)))
        ttk.Checkbutton(erow3, text="把「此刻心情」注入大模型（影响文字措辞）",
                        variable=self.emo_inject_var).pack(side="left")
        self.emo_prehint_var = tk.BooleanVar(value=bool(e_cfg.get("pre_hint", True)))
        ttk.Checkbutton(erow3, text="回复前先用关键词预判情绪（本轮语气就跟上）",
                        variable=self.emo_prehint_var).pack(side="left", padx=12)
        ttk.Label(erow3, text="两项都关掉的话，情绪只改变语音语气、且慢一拍",
                  font=FONT_SMALL, foreground="#8a8f98").pack(side="left", padx=4)

        # 生效时机提示：情绪设置是在「保存配置」重载大脑时才生效的（审查报告 D7）
        erow4 = ttk.Frame(wrap)
        erow4.grid(row=next_row(), column=0, columnspan=3, sticky="we", pady=(0, 3))
        self.emo_effect_var = tk.StringVar(value="")
        self.emo_effect_label = ttk.Label(erow4, textvariable=self.emo_effect_var,
                                          font=FONT_SMALL)
        self.emo_effect_label.pack(side="left")
        self.emo_enabled_var.trace_add("write", lambda *_: self._refresh_emo_effect_hint())
        self._refresh_emo_effect_hint()


        # ---------- Pi 协作 ----------
        ttk.Label(wrap, text="Pi 协作（只在明确要求时调用外部 agent）", font=FONT_BOLD).grid(
            row=next_row(), column=0, columnspan=3, sticky="w", pady=(12, 2))
        p_cfg = self.cfg.get("pi", {}) or {}
        prow = ttk.Frame(wrap)
        prow.grid(row=next_row(), column=0, columnspan=3, sticky="we")
        self.pi_enabled_var = tk.BooleanVar(value=bool(p_cfg.get("enabled", True)))
        ttk.Checkbutton(prow, text="允许 /pi 调用", variable=self.pi_enabled_var).pack(side="left")
        self.pi_ro_var = tk.BooleanVar(value=bool(p_cfg.get("read_only", False)))
        ttk.Checkbutton(prow, text="只读模式（不给改文件 / 执行命令）",
                        variable=self.pi_ro_var).pack(side="left", padx=10)
        ttk.Label(prow, text="超时(秒)", font=FONT).pack(side="left", padx=(10, 2))
        self.pi_timeout_var = tk.StringVar(value=str(p_cfg.get("timeout", 600)))
        ttk.Entry(prow, textvariable=self.pi_timeout_var, width=7).pack(side="left")
        ttk.Label(prow, text="输出", font=FONT).pack(side="left", padx=(10, 2))
        self.pi_mode_var = tk.StringVar(value=str(p_cfg.get("mode", "text")))
        ttk.Combobox(prow, textvariable=self.pi_mode_var, width=7, state="readonly",
                     values=["text", "json"]).pack(side="left")

        pirow1 = ttk.Frame(wrap)
        pirow1.grid(row=next_row(), column=0, columnspan=3, sticky="we", pady=(2, 3))
        ttk.Label(pirow1, text="pi 路径", font=FONT).pack(side="left")
        self.pi_cli_var = tk.StringVar(value=str(p_cfg.get("cli_path", "") or ""))
        ttk.Entry(pirow1, textvariable=self.pi_cli_var, width=38).pack(side="left", padx=4)
        ttk.Button(pirow1, text="浏览…", command=self._browse_pi_cli).pack(side="left")
        ttk.Label(pirow1, text="留空 = 自动在 PATH 里找 pi", font=FONT_SMALL,
                  foreground="#8a8f98").pack(side="left", padx=6)

        pirow2 = ttk.Frame(wrap)
        pirow2.grid(row=next_row(), column=0, columnspan=3, sticky="we", pady=(0, 3))
        ttk.Label(pirow2, text="工作目录", font=FONT).pack(side="left")
        # load_config 会把空的 cwd 解析成项目根；这里若是项目根就显示为空，避免误导
        cwd_show = str(p_cfg.get("cwd", "") or "")
        try:
            if cwd_show and Path(cwd_show).resolve() == Path(APP_ROOT).resolve():
                cwd_show = ""
        except OSError:
            pass
        self.pi_cwd_var = tk.StringVar(value=cwd_show)
        ttk.Entry(pirow2, textvariable=self.pi_cwd_var, width=38).pack(side="left", padx=4)
        ttk.Button(pirow2, text="浏览…", command=self._browse_pi_cwd).pack(side="left")
        ttk.Label(pirow2, text="Pi 在哪个目录里干活", font=FONT_SMALL,
                  foreground="#8a8f98").pack(side="left", padx=6)

        # 人设编辑器
        ttk.Label(wrap, text="人设 / 性格（persona）", font=FONT_BOLD).grid(
            row=next_row(), column=0, sticky="e", pady=(10, 2))
        ttk.Label(wrap, text="改完点「保存人设」立即生效，无需重启", font=FONT_SMALL,
                  foreground="#8a8f98").grid(row=row["n"], column=1, sticky="w", pady=(10, 2))
        self.persona_box = ScrolledText(wrap, width=64, height=9, font=FONT, wrap="word")
        self.persona_box.grid(row=next_row(), columnspan=3, sticky="we", pady=4)
        self.persona_box.insert("1.0", load_persona(self.cfg))

        # 切页面时提示"还有没保存的修改"（退出时也会再确认一次，见 _quit_app）
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._cfg_snapshot = self._ui_cfg_snapshot()

    def _save_persona(self) -> None:
        text = self.persona_box.get("1.0", "end").rstrip() + "\n"
        p = Path(self.cfg.get("persona_path", ""))
        if not messagebox.askyesno("保存人设", f"将写入：\n{p}\n\n确定？"):
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        messagebox.showinfo("已保存", "人设已保存，下一句对话立即生效。")

    def _save_api_key(self) -> None:
        """独立保存 API Key：写盘 → 立即重载 → 反馈。"""
        new_key = self.key_var.get().strip()
        if not new_key:
            self.key_status_var.set("留空不改变已有的 Key")
            return
        cfg_path = Path("config.json")
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            self.key_status_var.set(f"读取失败：{exc}")
            return
        old_key = str((raw.get("mimo") or {}).get("api_key") or "")
        if old_key and new_key != old_key:
            if not messagebox.askyesno(
                    "更换 API Key",
                    "你正在更换一个已经配置好的 API Key。\n\n"
                    "为了安全，旧 Key 保存后就不再显示；新 Key 一旦填错，"
                    "就得回小米开放平台重新复制一次。\n\n"
                    "选「否」= 不换 Key。\n\n确定更换吗？",
                    icon="warning"):
                self.key_var.set("")
                self.key_status_var.set("已取消更换，保留原 Key")
                return
        raw.setdefault("mimo", {})["api_key"] = new_key
        cfg_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        self.cfg = load_config()  # 内存配置立即刷新
        self._cfg_snapshot = self._ui_cfg_snapshot()
        self.key_var.set("")  # 清空输入框，安全不留痕
        self.key_status_var.set("✓ API Key 已保存并立即生效")
        self.worker.submit("reload")  # 通知后台重载大脑

    def _save_llm_settings(self) -> None:
        """独立保存 LLM API Key + 模型 + 接口地址：写盘 → 立即重载 → 反馈。"""
        model = self.model_var.get().strip()
        base_url = self.baseurl_var.get().strip()
        new_key = self.llm_key_var.get().strip()
        if not model:
            self.llm_status_var.set("模型名不能为空")
            return
        cfg_path = Path("config.json")
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            self.llm_status_var.set(f"读取失败：{exc}")
            return
        raw.setdefault("llm", {})
        raw["llm"]["model"] = model
        raw["llm"]["base_url"] = base_url
        if new_key:
            old_key = str(raw.get("llm", {}).get("api_key", ""))
            if old_key and new_key != old_key:
                if not messagebox.askyesno(
                        "更换 LLM API Key",
                        "你正在更换 LLM 的独立 API Key。\n\n"
                        "旧 Key 保存后就不再显示；新 Key 一旦填错，"
                        "得回供应商平台重新复制。\n\n确定更换吗？",
                        icon="warning"):
                    self.llm_key_var.set("")
                    self.llm_status_var.set("已取消更换，保留原 Key")
                    return
            raw["llm"]["api_key"] = new_key
        cfg_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        self.cfg = load_config()
        self._cfg_snapshot = self._ui_cfg_snapshot()
        self.llm_key_var.set("")  # 清空输入框，安全不留痕
        if raw["llm"].get("api_key"):
            self.llm_key_hint.config(text="已配置 ✓（留空复用共用 Key）")
        self.llm_status_var.set(f"✓ LLM 设置已保存（模型={model}），立即生效")
        self.worker.submit("reload")

    def _save_config(self) -> None:
        """保存配置。数字字段**逐项**校验：填错的那一项回退原值，绝不连带挡住其它设置。

        （此前任一字段填错就整份拒绝保存——用户改音色时会因为无关的阈值笔误而存不进去。）
        """
        cfg_now = self.cfg
        cur_audio = cfg_now.get("audio", {}) or {}
        cur_pet = cfg_now.get("pet", {}) or {}
        cur_emo = cfg_now.get("emotion", {}) or {}
        cur_emo_base = cur_emo.get("baseline", {}) or {}
        bad: list[str] = []
        notes: list[str] = []   # 非错误的提示（如"这次没换 Key"），跟 bad 一起展示

        def num(var, field: str, default) -> float:
            ok, val = coerce_number(var.get(), default)
            if not ok:
                bad.append(f"{field}（填的不是数字）")
            return val

        def clamp_int(var, field: str, default: int, lo: int, hi: int) -> int:
            """整数字段：非法回退原值，合法则钳制在 [lo, hi] 内。"""
            ok, val = coerce_optional_int(var.get(), default)
            if not ok:
                bad.append(f"{field}（要填整数）")
                return int(default)
            if val is None:
                return int(default)
            return max(lo, min(hi, int(val)))

        thresh = num(self.thresh_var, "录音静音阈值", cur_audio.get("silence_threshold", 0.008))
        tail = num(self.tail_var, "说完停顿判定（秒）", cur_audio.get("tail_silence_seconds", 1.0))
        temp = num(self.temp_var, "性格随机度 temperature",
                   cfg_now.get("llm", {}).get("temperature", 0.9))
        emo_v = num(self.emo_base_v_var, "情绪基准·愉悦度", cur_emo_base.get("valence", 0.25))
        emo_a = num(self.emo_base_a_var, "情绪基准·唤醒度", cur_emo_base.get("arousal", 0.45))
        emo_i = num(self.emo_base_i_var, "情绪基准·亲密度", cur_emo_base.get("intimacy", 0.30))
        emo_decay = num(self.emo_decay_var, "情绪平复比例", cur_emo.get("decay_per_hour", 0.12))
        pi_timeout = num(self.pi_timeout_var, "Pi 超时（秒）",
                         cfg_now.get("pi", {}).get("timeout", 600))
        pet_scale = num(self.pet_scale_var, "桌宠大小", cur_pet.get("scale", 1.0))
        pet_opacity = num(self.pet_opacity_var, "桌宠不透明度", cur_pet.get("opacity", 1.0))
        cur_mem = cfg_now.get("memory", {}) or {}
        recall_k = clamp_int(self.recall_k_var, "每轮召回往事条数",
                             cur_mem.get("recall_top_k", 5), 1, 15)
        hist_turns = clamp_int(self.hist_turns_var, "携带对话轮数",
                               cfg_now.get("llm", {}).get("max_history_turns", 20), 5, 50)
        pin_min = clamp_int(self.pin_min_var, "常驻记忆重要度门槛",
                            cur_mem.get("pin_min_importance", 8), 0, 10)
        pin_limit = clamp_int(self.pin_limit_var, "常驻记忆条数",
                              cur_mem.get("pin_limit", 5), 1, 10)

        ok_x, pet_x = coerce_optional_int(self.pet_x_var.get(), cur_pet.get("start_x"))
        if not ok_x:
            bad.append("桌宠初始位置 X（要整数或留空）")
        ok_y, pet_y = coerce_optional_int(self.pet_y_var.get(), cur_pet.get("start_y"))
        if not ok_y:
            bad.append("桌宠初始位置 Y（要整数或留空）")
        cfg_path = config_path()      # 配置在哪只由 core.config 说了算
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("读取 config.json 失败", str(exc))
            return
        raw.setdefault("mimo", {})
        new_key = self.key_var.get().strip()
        if new_key:
            old_key = str((raw.get("mimo") or {}).get("api_key") or "")
            if old_key and new_key != old_key:
                # 旧 Key 从不回显（安全设计），一旦覆盖就再也拿不回来了 → 必须先说清楚
                if not messagebox.askyesno(
                        "更换 API Key",
                        "你正在更换一个已经配置好的 API Key。\n\n"
                        "为了安全，旧 Key 保存后就不再显示；新 Key 一旦填错，"
                        "就得回小米开放平台重新复制一次。\n\n"
                        "选「否」= 不换 Key，其余设置照常保存。\n\n确定更换吗？",
                        icon="warning"):
                    self.key_var.set("")   # 清空输入 → 下面不会再写入，保持原 Key
                    notes.append("API Key 未更换（保留了原来的 Key）")
                    new_key = ""
            if new_key:
                raw["mimo"]["api_key"] = new_key  # 留空 = 保持原 Key 不变
        raw.setdefault("llm", {})
        llm_new_key = self.llm_key_var.get().strip()
        if llm_new_key:
            old_llm_key = str(raw.get("llm", {}).get("api_key", ""))
            if old_llm_key and llm_new_key != old_llm_key:
                if not messagebox.askyesno(
                        "更换 LLM API Key",
                        "你正在更换 LLM 的独立 API Key。\n\n"
                        "旧 Key 保存后就不再显示；新 Key 一旦填错，"
                        "得回供应商平台重新复制。\n\n选「否」= 不换 Key，其余设置照常保存。\n\n确定更换吗？",
                        icon="warning"):
                    self.llm_key_var.set("")
                    notes.append("LLM API Key 未更换（保留了原来的 Key）")
                    llm_new_key = ""
            if llm_new_key:
                raw["llm"]["api_key"] = llm_new_key
        raw["llm"]["model"] = self.model_var.get().strip()
        raw["llm"]["base_url"] = self.baseurl_var.get().strip()
        raw["llm"]["temperature"] = temp
        raw["llm"]["max_history_turns"] = hist_turns
        raw.setdefault("tts", {})
        t = self._current_tts_settings()
        raw["tts"]["model"] = t["model"]
        raw["tts"]["voice"] = t["voice"]
        raw["tts"]["voice_instruction"] = t["voice_instruction"]
        raw["tts"]["reference_audio_path"] = t["reference_audio_path"]
        tts_new_url = self.tts_baseurl_var.get().strip()
        if tts_new_url:
            raw["tts"]["base_url"] = tts_new_url
        tts_new_key = self.tts_key_var.get().strip()
        if tts_new_key:
            old_tts_key = str(raw.get("tts", {}).get("api_key", ""))
            if old_tts_key and tts_new_key != old_tts_key:
                if not messagebox.askyesno(
                        "更换 TTS API Key",
                        "你正在更换 TTS 的独立 API Key。\n\n"
                        "旧 Key 保存后就不再显示；新 Key 一旦填错，"
                        "得回供应商平台重新复制。\n\n选「否」= 不换 Key，其余设置照常保存。\n\n确定更换吗？",
                        icon="warning"):
                    self.tts_key_var.set("")
                    notes.append("TTS API Key 未更换（保留了原来的 Key）")
                    tts_new_key = ""
            if tts_new_key:
                raw["tts"]["api_key"] = tts_new_key
        # ASR
        raw.setdefault("asr", {})
        raw["asr"]["provider"] = self.asr_provider_var.get().strip() or "mimo"
        raw["asr"]["base_url"] = self.asr_baseurl_var.get().strip()
        raw["asr"]["model"] = self.asr_model_var.get().strip() or "mimo-v2.5-asr"
        raw["asr"]["language"] = self.asr_lang_var.get().strip() or "auto"
        asr_new_key = self.asr_key_var.get().strip()
        if asr_new_key:
            old_asr_key = str(raw.get("asr", {}).get("api_key", ""))
            if old_asr_key and asr_new_key != old_asr_key:
                if not messagebox.askyesno(
                        "更换 ASR API Key",
                        "你正在更换 ASR 的独立 API Key。\n\n"
                        "旧 Key 保存后就不再显示；新 Key 一旦填错，"
                        "得回供应商平台重新复制。\n\n选「否」= 不换 Key，其余设置照常保存。\n\n确定更换吗？",
                        icon="warning"):
                    self.asr_key_var.set("")
                    notes.append("ASR API Key 未更换（保留了原来的 Key）")
                    asr_new_key = ""
            if asr_new_key:
                raw["asr"]["api_key"] = asr_new_key
        raw.setdefault("wake", {})
        raw["wake"]["keyword"] = self.keyword_var.get().strip()
        raw.setdefault("audio", {})
        raw["audio"]["silence_threshold"] = thresh
        raw["audio"]["tail_silence_seconds"] = tail
        raw["audio"]["barge_in"] = bool(self.barge_var.get())
        # 记忆与上下文注入
        raw.setdefault("memory", {})
        raw["memory"]["recall_top_k"] = recall_k
        raw["memory"]["pin_important"] = bool(self.pin_var.get())
        raw["memory"]["pin_min_importance"] = pin_min
        raw["memory"]["pin_limit"] = pin_limit
        raw.setdefault("pet", {})
        raw["pet"]["enabled"] = bool(self.pet_var.get())
        raw["pet"]["demo"] = bool(self.pet_demo_var.get())
        raw["pet"]["scale"] = pet_scale
        raw["pet"]["opacity"] = pet_opacity
        raw["pet"]["start_x"] = pet_x
        raw["pet"]["start_y"] = pet_y
        # 情绪模型
        raw.setdefault("emotion", {})
        raw["emotion"]["enabled"] = bool(self.emo_enabled_var.get())
        raw["emotion"]["infer_with_llm"] = bool(self.emo_llm_var.get())
        raw["emotion"]["style_mode"] = self.emo_mode_var.get().strip() or "director"
        raw["emotion"]["inject_to_context"] = bool(self.emo_inject_var.get())
        raw["emotion"]["pre_hint"] = bool(self.emo_prehint_var.get())
        raw["emotion"]["decay_per_hour"] = emo_decay
        raw["emotion"].setdefault("baseline", {})
        raw["emotion"]["baseline"]["valence"] = emo_v
        raw["emotion"]["baseline"]["arousal"] = emo_a
        raw["emotion"]["baseline"]["intimacy"] = emo_i
        # Pi 协作
        raw.setdefault("pi", {})
        raw["pi"]["enabled"] = bool(self.pi_enabled_var.get())
        raw["pi"]["read_only"] = bool(self.pi_ro_var.get())
        raw["pi"]["cli_path"] = self.pi_cli_var.get().strip()
        raw["pi"]["cwd"] = self.pi_cwd_var.get().strip()
        raw["pi"]["timeout"] = pi_timeout
        raw["pi"]["mode"] = self.pi_mode_var.get().strip() or "text"
        cfg_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        self.cfg = load_config()  # 内存里的配置同步刷新（桌宠重启等会用到）
        self._cfg_snapshot = self._ui_cfg_snapshot()   # 记下"已保存"的界面状态（退出保护用）
        self._refresh_cfg_dirty_hint()
        # 清空所有 Key 输入框，安全不留痕
        self.key_var.set("")
        self.llm_key_var.set("")
        self.tts_key_var.set("")
        self.asr_key_var.set("")
        self.worker.submit("reload")
        note_tail = ("\n\n另外：\n· " + "\n· ".join(notes)) if notes else ""
        if bad:
            messagebox.showwarning(
                "已保存（有字段填错了）",
                "配置已保存并重载。以下字段不是合法数字，已保留它们原来的值，"
                "其余设置（包括声音/音色）照常生效：\n\n· " + "\n· ".join(bad) + note_tail)
        else:
            messagebox.showinfo("已保存", "配置已保存，大脑正在重载。\n"
                                          "（人设即时生效；换 Key/模型/音色/情绪设置后下一句对话用新配置；\n"
                                          "  LLM/TTS/ASR 各自的独立 API Key 也已一并保存；\n"
                                          "  桌宠设置点「重启桌宠」立即生效）" + note_tail)

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

    # ============ 声音设置（MiMo TTS） ============
    def _on_tts_model_change(self, *_args) -> None:
        disp = self.tts_model_var.get()
        hints = {
            "mimo-v2.5-tts": "在下方「音色」里选一个喜欢的官方声音即可",
            "mimo-v2.5-tts-voicedesign": "在下方「音色描述」写想要什么样的声音（必填），不用选音色",
            "mimo-v2.5-tts-voiceclone": "在下方选或录一段 10~30 秒清晰人声（wav/mp3，必填），流萤复刻它",
        }
        model = self._display_to_tts_model(disp)
        self.tts_model_hint_var.set(hints.get(model, ""))
        self._apply_tts_field_states(model)

    def _apply_tts_field_states(self, model: str | None = None) -> None:
        """按合成方式启用/禁用对应的输入项（审查报告 D3）。

        此前三个框一直全开，voiceclone 下还能选音色，容易让人以为"克隆要先选音色"。
        判断规则抽在 `core.tts.tts_field_states()` 里，GUI 与自检共用同一套说法。
        """
        if model is None:
            model = self._display_to_tts_model(self.tts_model_var.get())
        st = tts_field_states(model)
        try:
            self.voice_box.configure(state="normal" if st["voice_enabled"] else "disabled")
            self.voice_label.configure(text=st["voice_label"])
            self.voice_hint.configure(text=st["voice_hint"])

            self.voice_instr_entry.configure(
                state="normal" if st["instr_enabled"] else "disabled")
            self.voice_instr_label.configure(text=st["instr_label"])
            self.voice_instr_hint.configure(text=st["instr_hint"])

            ref_state = "normal" if st["ref_enabled"] else "disabled"
            for w in (self.ref_entry, self.btn_ref_browse, self.btn_ref_record,
                      self.btn_ref_validate):
                w.configure(state=ref_state)
            self.ref_hint.configure(text=st["ref_hint"])
        except Exception:  # noqa: BLE001 —— 界面尚未建好时静默跳过
            pass

    def _display_to_tts_model(self, disp: str) -> str:
        for mid, d in self._tts_model_display.items():
            if disp == d or disp == mid:
                return mid
        return (disp or "").strip()

    def _current_tts_settings(self) -> dict:
        """读取界面上当前填写的声音设置（未保存也能用于试听）。"""
        return {
            "model": self._display_to_tts_model(self.tts_model_var.get()),
            "voice": voice_id_for_display(self.voice_var.get()),
            "voice_instruction": self.voice_instruction_var.get().strip(),
            "reference_audio_path": self.ref_audio_var.get().strip(),
        }

    def _voice_feedback(self, ok: bool, text: str) -> None:
        self.voice_status_var.set(("✅ " if ok else "❌ ") + text)
        self.voice_status_label.configure(foreground="#1a7f37" if ok else "#c0392b")

    def _save_voice_settings(self) -> None:
        """保存 TTS API Key + 接口地址 + 声音设置，并立即重载大脑。"""
        t = self._current_tts_settings()
        issues = tts_settings_issues(t)
        if issues:
            self._voice_feedback(False, "保存失败：" + issues[0])
            messagebox.showerror("声音设置不完整", "请先解决以下问题再保存：\n\n· " + "\n· ".join(issues))
            return
        new_key = self.tts_key_var.get().strip()
        new_url = self.tts_baseurl_var.get().strip()
        cfg_path = config_path()
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            raw.setdefault("tts", {})
            raw["tts"].update(t)
            if new_url:
                raw["tts"]["base_url"] = new_url
            if new_key:
                old_key = str(raw.get("tts", {}).get("api_key", ""))
                if old_key and new_key != old_key:
                    if not messagebox.askyesno(
                            "更换 TTS API Key",
                            "你正在更换 TTS 的独立 API Key。\n\n"
                            "旧 Key 保存后就不再显示；新 Key 一旦填错，"
                            "得回供应商平台重新复制。\n\n确定更换吗？",
                            icon="warning"):
                        self.tts_key_var.set("")
                        self._voice_feedback(False, "已取消更换，保留原 Key")
                        return
                raw["tts"]["api_key"] = new_key
            cfg_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            self._voice_feedback(False, f"保存失败：{exc}")
            messagebox.showerror("保存失败", f"写入 config.json 时出错：\n{exc}")
            return
        self.cfg = load_config()
        self._cfg_snapshot = self._ui_cfg_snapshot()
        self._refresh_cfg_dirty_hint()
        self.tts_key_var.set("")
        if raw.get("tts", {}).get("api_key"):
            self.tts_key_hint.config(text="已配置 ✓（留空复用共用 Key）")
        self.worker.submit("reload")
        self._voice_feedback(True, f"TTS 设置已保存（{t['model']}），下一句对话生效")
        messagebox.showinfo("已保存", "TTS 设置已保存，下一句对话立即用新声音。")

    def _save_asr_settings(self) -> None:
        """独立保存 ASR API Key + 服务商 + 接口地址 + 模型 + 语言：写盘 → 立即重载 → 反馈。"""
        new_key = self.asr_key_var.get().strip()
        provider = self.asr_provider_var.get().strip()
        base_url = self.asr_baseurl_var.get().strip()
        model = self.asr_model_var.get().strip()
        language = self.asr_lang_var.get().strip()
        if not provider:
            self.asr_status_var.set("服务商不能为空")
            return
        cfg_path = Path("config.json")
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            self.asr_status_var.set(f"读取失败：{exc}")
            return
        raw.setdefault("asr", {})
        raw["asr"]["provider"] = provider
        raw["asr"]["base_url"] = base_url
        raw["asr"]["model"] = model
        raw["asr"]["language"] = language
        if new_key:
            old_key = str(raw.get("asr", {}).get("api_key", ""))
            if old_key and new_key != old_key:
                if not messagebox.askyesno(
                        "更换 ASR API Key",
                        "你正在更换 ASR 的独立 API Key。\n\n"
                        "旧 Key 保存后就不再显示；新 Key 一旦填错，"
                        "得回供应商平台重新复制。\n\n确定更换吗？",
                        icon="warning"):
                    self.asr_key_var.set("")
                    self.asr_status_var.set("已取消更换，保留原 Key")
                    return
            raw["asr"]["api_key"] = new_key
        cfg_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        self.cfg = load_config()
        self._cfg_snapshot = self._ui_cfg_snapshot()
        self.asr_key_var.set("")
        if raw["asr"].get("api_key"):
            self.asr_key_hint.config(text="已配置 ✓（留空复用共用 Key）")
        self.asr_status_var.set(f"✓ ASR 设置已保存（{provider}/{model}），立即生效")
        self.worker.submit("reload")

    def _test_voice(self) -> None:
        """用界面上当前填写的设置合成一句试听（不保存配置）。"""
        t = self._current_tts_settings()
        issues = tts_settings_issues(t)
        if issues:
            self._voice_feedback(False, "无法试听：" + issues[0])
            messagebox.showerror("还不能试听", "请先解决：\n\n· " + "\n· ".join(issues))
            return
        api_key = self.tts_key_var.get().strip() or self.key_var.get().strip() or self.cfg.get("tts", {}).get("api_key", "") or self.cfg.get("mimo", {}).get("api_key", "")
        if not api_key:
            self._voice_feedback(False, "无法试听：还没配置 API Key")
            messagebox.showerror("缺少 API Key", "请先填入 TTS 的 API Key（或上方的共用 Key）。")
            return
        self.btn_voice_test.configure(state="disabled")
        self._voice_feedback(True, "正在合成试听……（联网，通常几秒钟）")

        def work() -> None:
            try:
                from core.tts import make_tts
                from core import audio_io

                cfg2 = dict(self.cfg)
                cfg2["mimo"] = {**self.cfg.get("mimo", {}), "api_key": api_key}
                cfg2["tts"] = {**self.cfg.get("tts", {}), **t}
                wav = make_tts(cfg2).synth(
                    "你好，我是流萤，这是你当前选择的声音，听起来还满意吗？",
                    t["voice_instruction"], t["reference_audio_path"] or None)
                a = self.cfg.get("audio", {})
                audio_io.play_wav_bytes(wav, device=a.get("output_device"),
                                        tail_silence=float(a.get("output_tail_silence", 0.8)))
                self.root.after(0, lambda: self._voice_feedback(
                    True, f"试听成功：已用「{t['model']}」合成并播放"))
            except Exception as exc:  # noqa: BLE001
                reason = str(exc).strip() or exc.__class__.__name__
                self.root.after(0, lambda r=reason: self._voice_feedback(False, f"试听失败：{r[:120]}"))
                self.root.after(0, lambda r=reason: messagebox.showerror(
                    "试听失败", f"声音合成没有成功，原因：\n\n{r}\n\n"
                    "常见排查：API Key 是否正确 / 网络是否通畅 / 音色描述或参考音频是否符合要求。"))
            finally:
                self.root.after(0, lambda: self.btn_voice_test.configure(state="normal"))

        threading.Thread(target=work, daemon=True, name="firefly-voice-test").start()

    def _validate_ref_audio(self) -> None:
        ok, detail = validate_reference_audio(self.ref_audio_var.get())
        self._voice_feedback(ok, ("参考音频可用：" if ok else "参考音频有问题：") + detail)
        if ok:
            messagebox.showinfo("参考音频可用", detail)
        else:
            messagebox.showwarning("参考音频有问题", detail)

    def _record_ref_audio(self) -> None:
        """现场录制一段参考音频（声音克隆用），存到 data/reference_voice.wav。"""
        secs = simpledialog.askinteger(
            "录制参考音频", "录多少秒？（官方建议 10~30 秒清晰人声）",
            initialvalue=15, minvalue=5, maxvalue=30, parent=self.root)
        if not secs:
            return
        if not messagebox.askyesno(
                "准备录音", f"点「是」立刻开始录 {secs} 秒。\n"
                "请对着麦克风，用平时说话的音量念一段话（念什么都行，吐字清晰、环境安静最重要）。"):
            return
        self._voice_feedback(True, f"正在录音 {secs} 秒……请开始说话")

        def work() -> None:
            try:
                from core import audio_io

                a = self.cfg.get("audio", {})
                data = audio_io.record_seconds(float(secs), int(a.get("sample_rate", 16000)),
                                               device=a.get("input_device"))
                rms, peak = audio_io.level_stats(data)
                if peak < 0.01:
                    raise RuntimeError("几乎没录到声音：检查麦克风是否被静音、是否选错设备（状态页有「麦克风体检」）")
                out = APP_ROOT / "data" / "reference_voice.wav"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(audio_io.to_wav_bytes(data, int(a.get("sample_rate", 16000))))
                ok, detail = validate_reference_audio(str(out))
                def done() -> None:
                    self.ref_audio_var.set(str(out))
                    self._voice_feedback(ok, f"录音完成：{out.name}（{detail}）")
                    messagebox.showinfo("录音完成", f"参考音频已保存到：\n{out}\n\n{detail}")
                self.root.after(0, done)
            except Exception as exc:  # noqa: BLE001
                reason = str(exc).strip() or exc.__class__.__name__
                self.root.after(0, lambda r=reason: self._voice_feedback(False, f"录音失败：{r[:120]}"))
                self.root.after(0, lambda r=reason: messagebox.showerror("录音失败", r))

        threading.Thread(target=work, daemon=True, name="firefly-ref-record").start()

    def _browse_ref_audio(self) -> None:
        """浏览选择参考音频文件（voiceclone用，官方仅支持 wav/mp3）。"""
        from tkinter import filedialog
        filetypes = [
            ("音频文件（官方支持）", "*.wav *.mp3"),
            ("所有文件", "*.*"),
        ]
        path = filedialog.askopenfilename(
            title="选择参考音频（10~30 秒清晰人声，wav/mp3）",
            filetypes=filetypes,
            initialdir=str(APP_ROOT / "data"),
        )
        if path:
            self.ref_audio_var.set(path)
            self._validate_ref_audio()  # 选完立刻校验并给出反馈

    def _browse_pi_cli(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 pi 可执行文件", initialdir=str(APP_ROOT),
            filetypes=[("可执行文件", "*.cmd *.exe *.bat"), ("所有文件", "*.*")])
        if path:
            self.pi_cli_var.set(path)

    def _browse_pi_cwd(self) -> None:
        path = filedialog.askdirectory(title="选择 Pi 的工作目录", initialdir=str(APP_ROOT))
        if path:
            self.pi_cwd_var.set(path)

    # ============ ⑤ 统计 ============
    def _build_stats_tab(self) -> None:
        f = ttk.Frame(self.nb)
        self.nb.add(f, text=" 统计 ")

        self.stats_box = ScrolledText(f, height=20, font=("Consolas", 10), state="disabled",
                                      wrap="word", relief="flat", background="#f6f6f2")
        self.stats_box.pack(fill="both", expand=True, padx=8, pady=8)

        btn_row = ttk.Frame(f)
        btn_row.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(btn_row, text="🔄 刷新统计", command=self._refresh_stats).pack(side="left")
        ttk.Button(btn_row, text="🧹 清理过期记忆", command=self._cleanup_expired).pack(side="left", padx=8)

        self._refresh_stats()

    def _refresh_stats(self) -> None:
        """刷新使用统计显示。"""
        try:
            stats = UsageStats(str(self.cfg.get("stats", {}).get("path")
                                   or (APP_ROOT / "data" / "stats.json")))
            s = stats.summary()
            lines = [
                "═══════ 使用统计 ═══════",
                "",
                f"  总消息数：{s['总消息数']}",
                f"  总对话轮次：{s['总对话轮次']}",
                f"  总会话数：{s['总会话数']}",
                f"  首次使用：{s['首次使用']}",
                "",
                f"  今日消息：{s['今日消息']}",
                f"  今日操作：{s['今日操作']}",
                "",
                "─── 分类分布 ───",
            ]
            for cat, cnt in s["分类分布"].items():
                if cnt > 0:
                    lines.append(f"  {cat}：{cnt} 条")

            lines.append("")
            lines.append("─── 最常用操作 ───")
            if s["最常用操作"]:
                for name, cnt in s["最常用操作"]:
                    lines.append(f"  {name}：{cnt} 次")
            else:
                lines.append("  （暂无）")

            # 记忆库信息
            lines.append("")
            lines.append("─── 记忆库 ───")
            try:
                mem = Memory(str(self.cfg["memory"]["db_path"]))
                ms = mem.stats()
                lines.append(f"  总记忆条数：{ms['total']}")
                if ms["earliest"]:
                    lines.append(f"  最早记忆：{ms['earliest']}")
                if ms["latest"]:
                    lines.append(f"  最新记忆：{ms['latest']}")
                lines.append(f"  平均重要度：{ms['avg_importance']}")
                mem.close()
            except Exception as exc:
                lines.append(f"  读取失败：{exc}")

            self.stats_box.configure(state="normal")
            self.stats_box.delete("1.0", "end")
            self.stats_box.insert("1.0", "\n".join(lines))
            self.stats_box.configure(state="disabled")
        except Exception as exc:
            self.stats_box.configure(state="normal")
            self.stats_box.delete("1.0", "end")
            self.stats_box.insert("1.0", f"读取统计失败：{exc}")
            self.stats_box.configure(state="disabled")

    def _cleanup_expired(self) -> None:
        """清理过期记忆。**批量永久删除、不可撤销**，所以先报数量再当面确认。"""
        try:
            mem = Memory(str(self.cfg["memory"]["db_path"]))
            try:
                n = mem.count_expired()
                if n <= 0:
                    messagebox.showinfo(
                        "无需清理",
                        "当前没有过期记忆。\n\n"
                        "（只有设了过期时间的记忆才会过期；普通对话是永不过期的，"
                        "想删就用列表里的「🗑 删除选中」。）")
                    return
                if not messagebox.askyesno(
                        "清理过期记忆",
                        f"将永久删除 {n} 条已过期的记忆，删除后无法恢复。\n\n确定清理吗？",
                        icon="warning"):
                    return
                count = mem.cleanup_expired()
            finally:
                mem.close()
            messagebox.showinfo("清理完成", f"已清理 {count} 条过期记忆。")
            self._refresh_stats()
        except Exception as exc:
            messagebox.showerror("清理失败", str(exc))

    # ============ ⑥ 状态 ============
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
        self.tool_box.insert("end", f"运行中：流萤 {flag} …\n\n")
        self.tool_box.configure(state="disabled")

        def work():
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            if is_frozen():
                # 打包后没有 python 和 main.py，直接调用自己：sys.executable 就是 流萤.exe
                cmd = [sys.executable, flag]
            else:
                cmd = [sys.executable, "main.py", flag]
            proc = subprocess.Popen(
                cmd, cwd=str(APP_ROOT), env=env, stdout=subprocess.PIPE,
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
            try:
                kind = msg[0]
                if kind == "chat_user":
                    self._chat_append("user", msg[1])
                elif kind == "chat_firefly":
                    self._chat_append("firefly", msg[1])
                    # 情绪在后台异步演化，稍等一下再拉一次快照，数值条才能跟上
                    self.root.after(1800, self._refresh_emotion)
                elif kind == "emotion":
                    self._render_emotion(msg[1])
                elif kind == "emotion_error":
                    self._render_emotion(None, str(msg[1]))
                elif kind == "context":
                    self._show_context(msg[1])
                elif kind == "chat_sys":
                    self._chat_append("sys", msg[1])
                elif kind == "status":
                    self.status_var.set(msg[1])
                elif kind == "reload_note":
                    # 配置热重载（D1）：状态栏闪一行，聊天区落一条 sys 留痕
                    notes = msg[1] if len(msg) > 1 else []
                    if notes:
                        human = "｜".join(notes)
                        self.status_var.set(f"⚙ 配置已热重载：{human}")
                        self._chat_append("sys", f"⚙ 配置已热重载：{human}")
                elif kind == "busy":
                    self._set_busy(bool(msg[1]))
                elif kind == "confirm":
                    _, prompt, ev, box = msg
                    try:
                        box["ok"] = messagebox.askyesno("⚠️ 危险操作确认", prompt, icon="warning")
                    except Exception:  # noqa: BLE001 —— 弹窗失败时拒绝操作
                        box["ok"] = False
                    ev.set()
                elif kind == "pet_state":
                    if self.pet_q is not None:
                        self.pet_q.put(msg[1])  # 对话状态实时驱动桌宠表情
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
            except Exception as exc:  # noqa: BLE001 —— 单条消息异常不阻断整个轮询
                import traceback
                traceback.print_exc()
                # confirm 消息异常时必须释放等待线程，否则 ChatWorker 会永久挂起
                if msg[0] == "confirm" and len(msg) >= 4:
                    try:
                        msg[3]["ok"] = False
                        msg[2].set()
                    except Exception:  # noqa: BLE001
                        pass
        self.root.after(150, self._poll_ui)

    def _on_close(self) -> None:
        """点击关闭按钮 → 最小化到系统托盘（而非退出）。"""
        if self.busy:
            if not messagebox.askyesno(
                    "最小化", "Firefly 正在回复中，最小化到托盘后对话会在后台继续。确定？"):
                return
        self.root.withdraw()  # 隐藏窗口
        if not hasattr(self, "_tray_icon") or self._tray_icon is None:
            self._start_tray()

    def _quit_app(self) -> None:
        """真正退出应用。"""
        if self.busy and not messagebox.askyesno(
                "退出", "Firefly 正在回复中，退出将中断本轮对话。确定退出？"):
            return
        # 防止"改了配置/人设却没保存"就退出，白改一场
        changed = self._cfg_dirty_changes()
        persona_dirty = self._persona_dirty()
        if changed or persona_dirty:
            parts = []
            if changed:
                shown = "、".join(changed[:8]) + ("…" if len(changed) > 8 else "")
                parts.append(f"配置页有 {len(changed)} 项修改没保存：{shown}")
            if persona_dirty:
                parts.append("人设文本框里还有没点「保存人设」的修改")
            if not messagebox.askyesno(
                    "有未保存的修改",
                    "\n".join(parts) + "\n\n现在退出会丢失这些修改。确定退出吗？",
                    icon="warning"):
                return
        # 桌宠与控制台同生共死：退出控制台时把桌宠一起关掉
        from core.pet import stop_pet

        stop_pet(self.pet_q)
        self.pet_q = None
        self._stop_tray()

        # 清理鼠标滚轮绑定
        if hasattr(self, '_config_canvas') and hasattr(self, '_config_mousewheel_handler'):
            try:
                self._config_canvas.unbind_all("<MouseWheel>")
            except Exception:
                pass

        self.root.destroy()

    # ============ 系统托盘 ============
    def _create_tray_image(self) -> "Image.Image":
        """生成一个小萤火虫图标（64x64）。"""
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        # 萤火虫身体（椭圆）
        draw.ellipse([20, 18, 44, 46], fill=(255, 215, 0, 255))
        # 发光
        draw.ellipse([24, 22, 40, 42], fill=(255, 255, 150, 200))
        # 眼睛
        draw.ellipse([28, 26, 32, 30], fill=(60, 40, 20, 255))
        draw.ellipse([33, 26, 37, 30], fill=(60, 40, 20, 255))
        # 翅膀
        draw.ellipse([10, 12, 28, 30], fill=(200, 230, 255, 120))
        draw.ellipse([36, 12, 54, 30], fill=(200, 230, 255, 120))
        return img

    def _start_tray(self) -> None:
        if not HAS_TRAY:
            return
        try:
            image = self._create_tray_image()
            menu = pystray.Menu(
                pystray.MenuItem("显示控制台", self._tray_restore, default=True),
                pystray.MenuItem("退出", self._tray_quit),
            )
            self._tray_icon = pystray.Icon("firefly", image, "流萤 Firefly", menu)
            threading.Thread(target=self._tray_icon.run, daemon=True).start()
        except Exception:
            pass

    def _stop_tray(self) -> None:
        if hasattr(self, "_tray_icon") and self._tray_icon:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
            self._tray_icon = None

    def _tray_restore(self, icon=None, item=None):
        self.root.after(0, self._restore_from_tray)

    def _restore_from_tray(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _tray_quit(self, icon=None, item=None):
        self.root.after(0, self._quit_app)


def main() -> int:
    if tk is None:
        print("当前 Python 缺少 tkinter，无法打开控制台。请用系统 Python 或完整版 Python 运行。")
        return 1
    try:
        app = ConsoleApp()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        # 黑窗口被隐藏时不能 input（会不可见地卡死），只在控制台可见时才暂停
        from core.winconsole import console_visible

        if console_visible():
            try:
                input("\n出错了，按回车关闭……")
            except Exception:  # noqa: BLE001
                pass
        return 1
    # 启动时即创建系统托盘（这样最小化到托盘功能立刻可用）
    app._start_tray()
    # 必须进入事件循环，否则窗口会一闪而过（关窗/托盘退出时 destroy() 会让它返回）
    try:
        app.root.mainloop()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
