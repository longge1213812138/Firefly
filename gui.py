"""流萤 · 控制台（GUI 操作台，对应需求 F-03）。

六个标签页：
  ① 对话  —— 文字聊天（与语音模式同一套大脑/记忆/安全闸口），可点「🎙 说话」
             用麦克风说一句；危险操作会弹窗确认（撤销清单）；支持输入 /pi 任务。
  ② 记忆  —— 浏览与检索全部历史对话（本地 SQLite）。
  ③ 情感  —— 程序化情绪模型（愉悦度/唤醒度/亲密度）实时状态、情绪曲线、
             发给 MiMo-TTS 的风格指令预览（对齐小米官方情绪方案）、手动微调。
  ④ 配置  —— API Key（掩码显示）、模型、TTS 音色/模型、唤醒词、录音阈值、桌宠开关、
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

from core.config import is_frozen, load_config, load_persona, resolve_root  # noqa: E402

# 打包成 exe 后，配置 / 数据 / 日志都要落在 exe 同级目录，而不是临时解包目录 _MEIPASS
APP_ROOT = resolve_root()
from core.memory import Memory  # noqa: E402
from core.stats import UsageStats  # noqa: E402
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
    """开机自启：往「启动」文件夹放/删一个 bat（GBK+CRLF，双击系统可识别）。

    打包成 exe 后没有「启动助手.bat」，改为直接拉起「流萤助手.exe」。
    """
    p = autostart_path()
    if enable:
        if is_frozen():
            launch = f'start "" "{APP_ROOT / "流萤助手.exe"}"\r\n'
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
                    t0 = time.time()
                    if text.startswith("/pi"):
                        self.ui.put(("status", "正在调用 Pi…（长任务可能几分钟，请稍候）"))
                        reply = f.run_pi_task(text[3:])
                    else:
                        self.ui.put(("status", "思考中…"))
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
        self._build_emotion_tab()
        self._build_config_tab()
        self._build_stats_tab()
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
                                 "涉及删除/覆盖/外发的操作会先弹窗让你确认。\n"
                                 "想让 Pi 帮忙：输入「/pi 任务」，例如「/pi 帮我看看这个项目的结构」"
                                 "（只在明确要求时调用，且每次都会弹窗确认）。\n")

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
                who = "你" if r["role"] == "user" else "Fairy"
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
        who = "你" if row["role"] == "user" else "Fairy"
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
            initialfile=f"fairy_memory.{fmt}",
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
        from core.emotion import EmotionModel

        f = ttk.Frame(self.nb)
        self.nb.add(f, text=" 情感 ")
        self._emo = EmotionModel(self.cfg, db_path=self.cfg["memory"]["db_path"])

        head = ttk.Frame(f)
        head.pack(fill="x", padx=10, pady=(10, 2))
        self.emo_title_var = tk.StringVar(value="")
        ttk.Label(head, textvariable=self.emo_title_var, font=FONT_BOLD).pack(side="left")
        self.emo_reason_var = tk.StringVar(value="")
        ttk.Label(head, textvariable=self.emo_reason_var, font=FONT_SMALL,
                  foreground="#8a8f98").pack(side="left", padx=10)

        self.emo_canvas = tk.Canvas(f, height=112, highlightthickness=0, background="#fbfbf7")
        self.emo_canvas.pack(fill="x", padx=10, pady=(4, 4))

        row = ttk.Frame(f)
        row.pack(fill="x", padx=10, pady=(0, 4))
        ttk.Button(row, text="🔄 刷新", command=self._refresh_emotion).pack(side="left")
        ttk.Button(row, text="🔁 重置到基准", command=self._reset_emotion).pack(side="left", padx=6)
        ttk.Button(row, text="😊 开心一点", command=lambda: self._nudge_emotion(0.15, 0.10, 0)).pack(side="left")
        ttk.Button(row, text="🌙 安静一点", command=lambda: self._nudge_emotion(-0.05, -0.18, 0)).pack(side="left", padx=6)
        ttk.Button(row, text="💗 更亲近", command=lambda: self._nudge_emotion(0, 0, 0.05)).pack(side="left")

        ttk.Label(f, text="将发给 MiMo-TTS 的风格指令（按官方规范放 role=user，可编辑后复制）",
                  font=FONT_BOLD).pack(anchor="w", padx=10, pady=(6, 2))
        self.emo_style_box = ScrolledText(f, height=9, font=FONT, wrap="word")
        self.emo_style_box.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        ttk.Label(f, text="情绪变化（蓝＝愉悦度，绿＝唤醒度）", font=FONT_SMALL,
                  foreground="#8a8f98").pack(anchor="w", padx=10)
        self.emo_hist_canvas = tk.Canvas(f, height=88, highlightthickness=0, background="#fbfbf7")
        self.emo_hist_canvas.pack(fill="x", padx=10, pady=(2, 8))
        self._refresh_emotion()

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

    def _draw_emotion_history(self) -> None:
        c = self.emo_hist_canvas
        c.delete("all")
        rows = self._emo.history(40)
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
        if getattr(self, "_emo", None) is None:
            return
        try:
            s = self._emo.snapshot()
        except Exception as exc:  # noqa: BLE001
            self.emo_title_var.set(f"读取情绪失败：{exc}")
            return
        self.emo_title_var.set(f"当前情绪：{s['label']}（{s['compound']}）｜累计 {s['turns']} 轮")
        self.emo_reason_var.set(s["reason"] or "")
        if not s["enabled"]:
            self.emo_title_var.set("情绪模型已在 config.json 里关闭（emotion.enabled=false）")
        c = self.emo_canvas
        c.delete("all")
        self._draw_bar(c, 4, "愉悦度", s["valence"], -1.0, 1.0, "#378ADD", "{:+.2f}")
        self._draw_bar(c, 42, "唤醒度", s["arousal"], 0.0, 1.0, "#1D9E75", "{:.2f}")
        self._draw_bar(c, 80, "亲密度", s["intimacy"], 0.0, 1.0, "#BA7517", "{:.2f}")
        self.emo_style_box.delete("1.0", "end")
        self.emo_style_box.insert("1.0", s["style_preview"])
        self._draw_emotion_history()

    def _reset_emotion(self) -> None:
        if getattr(self, "_emo", None) is None:
            return
        self._emo.reset()
        self._refresh_emotion()

    def _nudge_emotion(self, dv: float, da: float, di: float) -> None:
        if getattr(self, "_emo", None) is None:
            return
        self._emo.nudge(dv, da, di)
        self._refresh_emotion()

    # ============ ④ 配置 ============
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
        self.voice_instruction_frame.grid(row=next_row(), column=0, columnspan=3, sticky="we", pady=3)
        ttk.Label(self.voice_instruction_frame, text="音色描述（voicedesign用）", font=FONT).pack(side="left")
        ttk.Entry(self.voice_instruction_frame, textvariable=self.voice_instruction_var, width=40).pack(side="left", padx=5)
        ttk.Label(self.voice_instruction_frame, text="例：温柔甜美的年轻女性，语速适中", font=FONT_SMALL,
                  foreground="#8a8f98").pack(side="left", padx=5)

        # voiceclone参考音频
        self.ref_audio_var = tk.StringVar(value=self.cfg.get("tts", {}).get("reference_audio_path", ""))
        self.ref_audio_frame = ttk.Frame(wrap)
        self.ref_audio_frame.grid(row=next_row(), column=0, columnspan=3, sticky="we", pady=3)
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
        try:
            thresh = float(self.thresh_var.get())
            tail = float(self.tail_var.get())
            temp = float(self.temp_var.get())
            emo_v = float(self.emo_base_v_var.get())
            emo_a = float(self.emo_base_a_var.get())
            emo_i = float(self.emo_base_i_var.get())
            emo_decay = float(self.emo_decay_var.get())
            pi_timeout = float(self.pi_timeout_var.get())
        except ValueError:
            messagebox.showerror("格式不对",
                                 "阈值 / 温度 / 情绪基准 / 平复比例 / Pi 超时 都要填数字"
                                 "（例如 0.008、0.9、0.25、0.12、600）")
            return
        cfg_path = APP_ROOT / "config.json"
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
        # 情绪模型
        raw.setdefault("emotion", {})
        raw["emotion"]["enabled"] = bool(self.emo_enabled_var.get())
        raw["emotion"]["infer_with_llm"] = bool(self.emo_llm_var.get())
        raw["emotion"]["style_mode"] = self.emo_mode_var.get().strip() or "director"
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
        self.worker.submit("reload")
        messagebox.showinfo("已保存", "配置已保存，大脑正在重载。\n"
                                      "（人设即时生效；换 Key/模型/音色/情绪设置后下一句对话用新配置）")

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
            initialdir=str(APP_ROOT / "data"),
        )
        if path:
            self.ref_audio_var.set(path)

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
        """清理过期记忆。"""
        try:
            mem = Memory(str(self.cfg["memory"]["db_path"]))
            count = mem.cleanup_expired()
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
        self.tool_box.insert("end", f"运行中：main.py {flag} …\n\n")
        self.tool_box.configure(state="disabled")

        def work():
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            if is_frozen():
                # 打包后没有 python 和 main.py，改为调用同目录的「流萤助手.exe」
                helper = APP_ROOT / "流萤助手.exe"
                if not helper.exists():
                    self.ui.put(("log_line", f"找不到 {helper}，无法运行该项检查。"))
                    self.ui.put(("log_line", "请把「流萤助手.exe」和本程序放在同一个目录。"))
                    self.ui.put(("tool_done", 1))
                    return
                cmd = [str(helper), flag]
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
            kind = msg[0]
            if kind == "chat_user":
                self._chat_append("user", msg[1])
            elif kind == "chat_fairy":
                self._chat_append("fairy", msg[1])
                if getattr(self, "_emo", None) is not None:
                    self._refresh_emotion()
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
        """点击关闭按钮 → 最小化到系统托盘（而非退出）。"""
        if self.busy:
            if not messagebox.askyesno(
                    "最小化", "Fairy 正在回复中，最小化到托盘后对话会在后台继续。确定？"):
                return
        self.root.withdraw()  # 隐藏窗口
        if not hasattr(self, "_tray_icon") or self._tray_icon is None:
            self._start_tray()

    def _quit_app(self) -> None:
        """真正退出应用。"""
        if self.busy and not messagebox.askyesno(
                "退出", "Fairy 正在回复中，退出将中断本轮对话。确定退出？"):
            return
        self._stop_tray()
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
            self._tray_icon = pystray.Icon("fairy", image, "流萤 Fairy", menu)
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
        try:
            input("\n出错了，按回车关闭……")
        except Exception:  # noqa: BLE001
            pass
        return 1
    # 必须进入事件循环，否则窗口会一闪而过（关窗/托盘退出时 destroy() 会让它返回）
    try:
        app.root.mainloop()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
