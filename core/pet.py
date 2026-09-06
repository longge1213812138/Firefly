"""桌宠：小萤火虫「流萤」（tkinter 画布，零第三方依赖）。

四种状态（由语音主流程通过线程安全队列驱动）：
  idle      待机   —— 缓慢呼吸发光，偶尔眨眼
  listening 聆听中 —— 翅膀快速扇动，头部两侧出现声波
  thinking  思考中 —— 头顶转星星，眼睛向上看
  speaking  说话中 —— 口型开合，灯球快闪

窗口无边框、置顶、背景透明（Windows transparentcolor）；
支持拖拽移动、贴边隐藏（拖到屏幕左右边缘）、双击切换演示模式、右键菜单。
纯逻辑部分在 PetBrain（不依赖窗口，可离线自检）。
"""
from __future__ import annotations

import math
import queue
import threading
import time

try:
    import tkinter as tk
except Exception:  # pragma: no cover - 无 tkinter 环境下降级
    tk = None

STATES = ("idle", "listening", "thinking", "speaking")
STATE_TEXT = {"idle": "待机中", "listening": "聆听中", "thinking": "思考中", "speaking": "说话中"}
GLOW = ["#2e3318", "#4c551f", "#6f7d2a", "#93a838", "#bcd451", "#e2f76f", "#fbffbe"]
BG = "#0a0a0e"  # 作为透明色使用，绘制时避开这个确切颜色


class PetBrain:
    """状态机 + 动画相位（无窗口依赖，可离线自检）。"""

    _speed = {"idle": 0.05, "listening": 0.13, "thinking": 0.09, "speaking": 0.18}
    _wing_speed = {"idle": 0.09, "listening": 0.35, "thinking": 0.07, "speaking": 0.22}

    def __init__(self, initial: str = "idle"):
        self.state = initial if initial in STATES else "idle"
        self.frame = 0

    def set_state(self, s: str) -> None:
        if s in STATES:
            self.state = s

    def tick(self) -> str:
        self.frame += 1
        return self.state

    @property
    def glow(self) -> float:
        """呼吸亮度 0..1，状态越活跃呼吸越快。"""
        return 0.5 + 0.5 * math.sin(self.frame * self._speed[self.state])

    @property
    def wing(self) -> float:
        """翅膀开合 0.35..1。"""
        return 0.35 + 0.65 * (0.5 + 0.5 * math.sin(self.frame * self._wing_speed[self.state]))

    @property
    def mouth_open(self) -> bool:
        return self.state == "speaking" and (self.frame // 3) % 2 == 0

    @property
    def blinking(self) -> bool:
        return self.state != "speaking" and (self.frame % 46) < 4

    @property
    def glow_color(self) -> str:
        idx = int(self.glow * (len(GLOW) - 1) + 0.5)
        return GLOW[max(0, min(len(GLOW) - 1, idx))]


class FairyPet:
    """桌宠窗口（tkinter 主循环阻塞运行）。"""

    W, H = 150, 176

    def __init__(self, cfg: dict | None = None, q: "queue.Queue[str] | None" = None, demo: bool = False):
        if tk is None:
            raise RuntimeError("当前 Python 没有自带 tkinter，无法显示桌宠")
        self.brain = PetBrain()
        self.q = q or queue.Queue()
        self.demo = demo
        self._demo_ts = time.time()
        self.hidden = False
        self.edge = None  # 'left' / 'right'（贴边隐藏时记录哪一边）
        self.drag_dx = 0
        self.drag_dy = 0

        self.root = tk.Tk()
        self.root.title("流萤")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BG)
        try:
            self.root.attributes("-transparentcolor", BG)
        except Exception:
            pass

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        pet_cfg = (cfg or {}).get("pet", {}) or {}
        x = int(pet_cfg.get("start_x") or (sw - self.W - 40))
        y = int(pet_cfg.get("start_y") or max(60, sh - self.H - 180))
        self.root.geometry(f"{self.W}x{self.H}+{x}+{y}")

        self.canvas = tk.Canvas(self.root, width=self.W, height=self.H, bg=BG, highlightthickness=0)
        self.canvas.pack()
        self._bind()
        self._tick()
        self.root.mainloop()

    # ---------- 交互 ----------
    def _bind(self) -> None:
        c = self.canvas
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._drag)
        c.bind("<ButtonRelease-1>", self._release)
        c.bind("<Double-Button-1>", lambda e: self._toggle_demo())
        c.bind("<Button-3>", self._menu)
        c.bind("<Enter>", self._restore)

    def _press(self, e) -> None:
        self.drag_dx = e.x - self.root.winfo_x()
        self.drag_dy = e.y - self.root.winfo_y()

    def _drag(self, e) -> None:
        self.hidden = False
        self.edge = None
        self.root.geometry(f"+{e.x - self.drag_dx}+{e.y - self.drag_dy}")

    def _release(self, _e) -> None:
        """松手时贴边检测：靠近屏幕左右边缘 → 滑入边缘只露一条，鼠标移上来恢复。"""
        x = self.root.winfo_x()
        sw = self.root.winfo_screenwidth()
        if x <= 40:
            self.edge = "left"
            self.root.geometry(f"+{-self.W + 18}+{self.root.winfo_y()}")
            self.hidden = True
        elif x >= sw - self.W - 40:
            self.edge = "right"
            self.root.geometry(f"+{sw - 18}+{self.root.winfo_y()}")
            self.hidden = True

    def _restore(self, _e=None) -> None:
        if not self.hidden:
            return
        sw = self.root.winfo_screenwidth()
        x = 20 if self.edge == "left" else sw - self.W - 20
        self.root.geometry(f"+{x}+{self.root.winfo_y()}")
        self.hidden = False

    def _toggle_demo(self) -> None:
        self.demo = not self.demo
        self._demo_ts = time.time()

    def _menu(self, e) -> None:
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label=("停止演示" if self.demo else "演示四种状态"), command=self._toggle_demo)
        m.add_command(label="贴边隐藏", command=lambda: self._release(None))
        m.add_separator()
        m.add_command(label="退出桌宠", command=self.root.destroy)
        m.tk_popup(e.x_root, e.y_root)

    # ---------- 动画 ----------
    def _tick(self) -> None:
        while True:
            try:
                self.brain.set_state(self.q.get_nowait())
            except queue.Empty:
                break
        if self.demo and time.time() - self._demo_ts > 2.2:
            order = list(STATES)
            self.brain.set_state(order[(order.index(self.brain.state) + 1) % len(order)])
            self._demo_ts = time.time()
        self.brain.tick()
        self._draw()
        self.root.after(80, self._tick)

    def _draw(self) -> None:
        c = self.canvas
        b = self.brain
        c.delete("all")

        # 呼吸光晕（两层，颜色随呼吸变亮变暗）
        g = b.glow
        r1 = 46 + 10 * g
        r2 = 30 + 6 * g
        c.create_oval(75 - r1, 92 - r1, 75 + r1, 92 + r1, fill=GLOW[1], outline="")
        c.create_oval(75 - r2, 92 - r2, 75 + r2, 92 + r2, fill=GLOW[2], outline="")

        # 翅膀（聆听时扇得最快）
        wf = b.wing
        c.create_oval(75 - 14 - 38 * wf, 50, 75 - 10, 74, fill="#cfe8ff", outline="#9fb8d8", stipple="gray50")
        c.create_oval(75 + 14 + 38 * wf, 50, 75 + 10, 74, fill="#cfe8ff", outline="#9fb8d8", stipple="gray50")

        # 身体
        c.create_oval(45, 66, 105, 124, fill="#3d4450", outline="#1c2027", width=2)
        # 灯球（萤火虫的发光腹部）
        c.create_oval(63, 108, 87, 132, fill=b.glow_color, outline="#2a2e18")

        # 触角（思考时晃动）
        wig = math.sin(b.frame * 0.3) * 3 if b.state == "thinking" else 0
        c.create_line(63, 67, 52 + wig, 47, smooth=True, width=2, fill="#1c2027")
        c.create_line(87, 67, 98 + wig, 47, smooth=True, width=2, fill="#1c2027")
        c.create_oval(49 + wig, 42, 56 + wig, 49, fill=GLOW[5], outline="")
        c.create_oval(95 + wig, 42, 102 + wig, 49, fill=GLOW[5], outline="")

        # 眼睛（眨眼 / 思考时向上看）
        dy = -2 if b.state == "thinking" else 0
        if b.blinking:
            c.create_line(55, 86, 64, 86, width=2, fill="#e8ecf4")
            c.create_line(86, 86, 95, 86, width=2, fill="#e8ecf4")
        else:
            c.create_oval(54, 79, 66, 93, fill="#e8ecf4", outline="")
            c.create_oval(84, 79, 96, 93, fill="#e8ecf4", outline="")
            c.create_oval(58, 83 + dy, 64, 89 + dy, fill="#232833", outline="")
            c.create_oval(86, 83 + dy, 92, 89 + dy, fill="#232833", outline="")

        # 腮红
        c.create_oval(48, 96, 55, 101, fill="#c97b8b", outline="")
        c.create_oval(95, 96, 102, 101, fill="#c97b8b", outline="")

        # 嘴：说话时口型开合，其余时候微笑
        if b.state == "speaking":
            h = 9 if b.mouth_open else 3
            c.create_oval(68, 96, 82, 96 + h + 2, fill="#22262e", outline="")
        else:
            c.create_arc(66, 92, 84, 106, start=200, extent=140, style=tk.ARC,
                         outline="#22262e", width=2)

        # 聆听声波（左右各两道，虚线闪动）
        if b.state == "listening":
            dash = (4, 3) if b.frame % 12 < 6 else (2, 5)
            for i, pad in enumerate((0, 8)):
                c.create_arc(14 - pad, 70 - pad, 46 + pad, 102 + pad, start=100, extent=140,
                             style=tk.ARC, outline=GLOW[4], width=2, dash=dash)
                c.create_arc(104 - pad, 70 - pad, 136 + pad, 102 + pad, start=-60, extent=140,
                             style=tk.ARC, outline=GLOW[4], width=2, dash=dash)

        # 思考星星（头顶旋转）
        if b.state == "thinking":
            ang = b.frame * 0.12
            pts = []
            for i in range(8):
                rr = 11 if i % 2 == 0 else 4.2
                a = ang + i * math.pi / 4
                pts += [75 + rr * math.cos(a), 36 + rr * math.sin(a)]
            c.create_polygon(pts, fill="#ffe66d", outline="")

        # 状态文字
        c.create_text(75, 158, text=STATE_TEXT[b.state], fill="#9aa4b5",
                      font=("Microsoft YaHei UI", 9))


def run_pet(cfg: dict | None = None, q: "queue.Queue[str] | None" = None, demo: bool = False) -> None:
    """阻塞运行桌宠（供独立进程 / 桌宠线程调用）。"""
    FairyPet(cfg, q=q, demo=demo)


def start_pet_thread(cfg: dict | None = None, demo: bool | None = None) -> "queue.Queue[str]":
    """在后台线程启动桌宠，返回状态队列：put('listening') 等即可切换状态。"""
    q: "queue.Queue[str]" = queue.Queue()
    pet_cfg = (cfg or {}).get("pet", {}) or {}
    use_demo = bool(pet_cfg.get("demo", False)) if demo is None else demo

    def _run() -> None:
        try:
            run_pet({"pet": pet_cfg}, q, demo=use_demo)
        except Exception:  # noqa: BLE001 - 桌宠失败不应拖垮主流程
            pass

    threading.Thread(target=_run, daemon=True, name="fairy-pet").start()
    return q


def main() -> int:
    import argparse

    if hasattr(__import__("sys").stdout, "reconfigure"):
        __import__("sys").stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="流萤桌宠（小萤火虫）")
    ap.add_argument("--demo", action="store_true", help="循环演示四种状态（看看它长什么样）")
    args = ap.parse_args()
    run_pet(demo=args.demo)
    return 0


if __name__ == "__main__":
    main()
