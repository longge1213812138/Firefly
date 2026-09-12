"""外部 Agent 后端：可插拔的「按需调用」接口（出站扩展点）。

设计原则
- 陪伴端默认完全独立：没装 Pi、没配置，也能照常跑（桌宠/语音/记忆一样不落）。
- 只有当用户在对话里**明确要求**时，才会通过这里调用外部 agent 的命令行：
  · 显式命令 `/pi <任务>`（确定性最高）
  · 模型按人设判断后发出的 `pi_agent` 动作
- **长期记忆始终只在陪伴端**，本模块只负责把「任务交给谁、拿到什么结果」讲清楚。

再接一个新后端（比如别的 agent CLI）只需三步：
    class MyBackend:
        name = "my"
        def available(self) -> bool: ...
        def describe(self) -> str: ...
        def run(self, task, **kw) -> AgentResult: ...
    register_backend(MyBackend)          # 注册
之后 `pi_agent` 动作里传 {"backend": "my"} 即可路由过去。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

OnEvent = Optional[Callable[[str], None]]


@dataclass
class AgentResult:
    """一次外部 agent 调用的结果（与具体后端无关的统一形状）。"""

    ok: bool
    text: str = ""
    backend: str = ""
    argv: list[str] = field(default_factory=list)
    code: int = 0
    duration: float = 0.0
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "text": self.text,
            "backend": self.backend,
            "code": self.code,
            "duration": round(self.duration, 2),
            "error": self.error,
        }


# ------------------------------------------------------------------ 后端注册表
_BACKENDS: dict[str, type] = {}


def register_backend(cls: type) -> type:
    """把一个后端类登记进注册表（用类属性 name 作键）。"""
    name = str(getattr(cls, "name", "") or "").strip().lower()
    if not name:
        raise ValueError("后端必须有非空的 name")
    _BACKENDS[name] = cls
    return cls


def get_backend(name: str, cfg: dict | None = None):
    """按名字取一个后端实例；不存在返回 None。"""
    cls = _BACKENDS.get(str(name or "").strip().lower())
    return cls(cfg or {}) if cls else None


def list_backends(cfg: dict | None = None) -> dict:
    """列出全部后端实例（name -> backend）。"""
    return {n: cls(cfg or {}) for n, cls in _BACKENDS.items()}


# ------------------------------------------------------------------ Pi 后端
@register_backend
class PiCliBackend:
    """调用 Pi 编程智能体（pi.dev）的命令行。

    关键参数（都能在 config.json 的 pi 段里改）：
      cli_path   留空 → 自动在 PATH 里找 pi
      cwd        Pi 的工作目录（默认项目根）
      read_only  True → 只给读类工具（--tools read,grep,find,ls）
      mode       text（默认，纯文本）| json（事件按 JSON 行输出）| rpc
      timeout    秒；超时会被强制终止
      extra_args 追加给 pi 的额外参数
    """

    name = "pi"

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or {}
        self.pcfg = dict(self.cfg.get("pi", {}) or {})
        self._resolved: Optional[str] = None

    # -------------------------------------------------- 探测
    def resolve_cli(self) -> str:
        """找到 pi 可执行文件；找不到返回空串。结果会缓存。"""
        if self._resolved is not None:
            return self._resolved
        raw = str(self.pcfg.get("cli_path", "") or "").strip()
        if raw:
            p = os.path.expanduser(raw)
            self._resolved = p if (os.path.exists(p) or shutil.which(p)) else ""
        else:
            self._resolved = shutil.which("pi") or ""
        return self._resolved

    def available(self) -> bool:
        return bool(self.resolve_cli())

    def describe(self) -> str:
        cli = self.resolve_cli()
        return f"Pi 编程智能体 CLI（{cli}）" if cli else "Pi 编程智能体 CLI（未找到 pi 命令）"

    # -------------------------------------------------- 参数拼装
    def build_argv(self, task: str, *, read_only: bool = False,
                   extra_args: list[str] | None = None) -> list[str]:
        """把任务拼成 pi 的命令行。单独抽出来便于离线测试。"""
        cli = self.resolve_cli() or "pi"
        mode = str(self.pcfg.get("mode", "text") or "text").lower()
        argv = [cli, "-p"]  # -p：非交互，跑完（含多轮工具调用）就退出
        if mode in ("json", "rpc"):
            argv += ["--mode", mode]  # 事件按 JSON 行 / RPC 输出，供程序解析
        # 默认输出模式就是 text，无需额外参数（旧版的 --no-markdown/--print-turn 已不存在）
        if read_only:
            argv += ["--tools", "read,grep,find,ls"]
        extra = extra_args if extra_args is not None else (self.pcfg.get("extra_args") or [])
        if isinstance(extra, str):
            extra = [extra]
        argv += [str(a) for a in extra]
        argv += ["--", task]  # -- 兜住以短横线开头的任务文本
        return argv

    def _wrap_windows(self, argv: list[str]) -> list[str]:
        """Windows 上 npm 装的 pi 是 pi.cmd，需要用 cmd /c 起。"""
        if os.name == "nt" and argv and argv[0].lower().endswith((".cmd", ".bat")):
            return ["cmd", "/c", *argv]
        return argv

    def _kill_tree(self, proc: "subprocess.Popen") -> None:
        """终止进程及其**整棵子进程树**，并回收僵尸进程。

        为什么不能只 proc.kill()：Windows 上 pi 是 pi.cmd，被 `cmd /c` 包了一层；
        proc.kill() 只杀最外层 cmd.exe，真正的 node 子进程会变成孤儿继续跑、
        继续占 CPU/内存/管道句柄。多次超时后孤儿进程越攒越多，会拖垮后续的
        大脑/语音请求（表现为"突然不能正常聊天了"）。
        这里用系统自带 taskkill /T /F 把整棵树一起端掉；非 Windows 退回 kill。
        """
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                               capture_output=True, timeout=10)
            else:
                proc.kill()
        except Exception:  # noqa: BLE001 —— 兜底再试一次 kill
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            proc.wait(timeout=5)  # 等它真正退出，避免留下僵尸进程
        except subprocess.TimeoutExpired:
            pass

    # -------------------------------------------------- 执行
    def run(self, task: str, *, read_only: bool = False,
            timeout: float | None = None, cwd: str | None = None,
            on_event: OnEvent = None, extra_args: list[str] | None = None) -> AgentResult:
        task = (task or "").strip()
        if not task:
            return AgentResult(ok=False, backend=self.name, error="空任务")

        cli = self.resolve_cli()
        if not cli:
            return AgentResult(
                ok=False, backend=self.name,
                error="没找到 pi 命令（请先安装 Pi，或在 config.json 的 pi.cli_path 填绝对路径）",
            )

        cwd = cwd or self.pcfg.get("cwd") or os.getcwd()
        timeout = float(timeout or self.pcfg.get("timeout", 600) or 600)
        if read_only is None:
            read_only = bool(self.pcfg.get("read_only", False))
        else:
            read_only = bool(read_only)
        argv = self._wrap_windows(self.build_argv(task, read_only=read_only, extra_args=extra_args))

        env = dict(os.environ)
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")

        t0 = time.time()
        try:
            proc = subprocess.Popen(
                argv, cwd=cwd, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        except OSError as exc:
            return AgentResult(ok=False, backend=self.name, argv=argv,
                               error=f"启动失败：{exc}", duration=time.time() - t0)

        out: list[str] = []
        err: deque[str] = deque(maxlen=20)

        def _pump(stream, sink, notify: bool) -> None:
            try:
                for line in stream or []:
                    line = line.rstrip("\r\n")
                    sink.append(line)
                    if notify and on_event:
                        try:
                            on_event(line)
                        except Exception:  # noqa: BLE001
                            pass
            except Exception:  # noqa: BLE001
                pass

        t_out = threading.Thread(target=_pump, args=(proc.stdout, out, True), daemon=True)
        t_err = threading.Thread(target=_pump, args=(proc.stderr, err, False), daemon=True)
        t_out.start()
        t_err.start()

        timed_out = False
        try:
            proc.wait(timeout=max(1.0, timeout))
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill_tree(proc)   # 杀整棵树（Windows 下 kill 只杀得到 cmd.exe 外壳）
        t_out.join(timeout=5)
        t_err.join(timeout=5)

        dur = time.time() - t0
        text = "\n".join(out).strip()

        if timed_out:
            return AgentResult(ok=False, text=text, backend=self.name, argv=argv, code=-1,
                               duration=dur, error=f"超时（>{timeout:.0f}s）已被终止")
        ok = proc.returncode == 0 and bool(text)
        errmsg = "" if ok else ("\n".join(list(err))[:300] or f"退出码 {proc.returncode}，无输出")
        return AgentResult(ok=ok, text=text, backend=self.name, argv=argv,
                           code=proc.returncode or 0, duration=dur, error=errmsg)


# ------------------------------------------------------------------ 手动自测
if __name__ == "__main__":  # python -m core.agent_backend "任务"
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from core.config import load_config

    _cfg = load_config()
    _task = " ".join(sys.argv[1:]) or "用一句话介绍你自己"
    _b = get_backend("pi", _cfg)
    print(_b.describe())
    _r = _b.run(_task, on_event=lambda line: print(f"  | {line}"))
    print("ok=", _r.ok, "code=", _r.code, "耗时=%.1fs" % _r.duration)
    print(_r.text[:2000] or _r.error)
