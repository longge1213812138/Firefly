"""电脑操作工具（MVP 只开放只读/低风险 + 带确认的高风险动作）。

大模型通过在回复中输出一行 ACTION:{"name":..., "args":{...}} 来请求执行。
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

from . import safety

DESCRIPTIONS = (
    "可用操作（在回复最后另起一行输出 ACTION:{\"name\":\"操作名\",\"args\":{...}} 来调用，最多一个）：\n"
    "1. get_time 无参数 —— 查询当前时间\n"
    "2. list_dir {\"path\":\"目录\"} —— 列出目录内容\n"
    "3. read_file {\"path\":\"文件\", \"max_chars\":2000} —— 读取文本文件内容\n"
    "4. open_path {\"path\":\"文件或目录\"} —— 打开文件/文件夹/程序\n"
    "5. open_url {\"url\":\"https://...\"} —— 用浏览器打开网页\n"
    "6. write_file {\"path\":\"文件\", \"content\":\"内容\"} —— 写入文件（需确认）\n"
    "7. run_command {\"cmd\":\"命令\"} —— 执行命令行（需确认）\n"
    "规则：删除、覆盖、外发、付款类操作必须先征求用户同意；拿不准就只用只读操作。"
)


def _run(name: str, args: dict) -> tuple[bool, str]:
    try:
        if name == "get_time":
            return True, datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if name == "list_dir":
            p = Path(args.get("path", ".")).expanduser()
            items = [f"{'[目录]' if i.is_dir() else ''}{i.name}" for i in list(p.iterdir())[:100]]
            return True, "\n".join(items) if items else "(空目录)"
        if name == "read_file":
            p = Path(args.get("path", "")).expanduser()
            n = int(args.get("max_chars", 2000))
            return True, p.read_text(encoding="utf-8", errors="ignore")[:n]
        if name == "open_path":
            p = str(Path(args.get("path", "")).expanduser())
            if os.name == "nt":
                os.startfile(p)  # noqa: S606
            else:
                subprocess.Popen(["open" if os.name == "posix" else "xdg-open", p])
            return True, f"已打开 {p}"
        if name == "open_url":
            import webbrowser

            webbrowser.open(args.get("url", ""))
            return True, f"已打开网页 {args.get('url','')}"
        if name == "write_file":
            p = Path(args.get("path", "")).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args.get("content", ""), encoding="utf-8")
            return True, f"已写入 {p}"
        if name == "run_command":
            out = subprocess.run(
                args.get("cmd", ""),
                shell=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            return True, (out.stdout or "")[:2000] + (("\nERR:" + out.stderr[:500]) if out.stderr else "")
        return False, f"未知操作：{name}"
    except Exception as exc:  # noqa: BLE001
        return False, f"执行失败：{exc}"


def execute(cfg: dict, action: dict, auto_confirm: bool = False, session_id: str = "") -> tuple[bool, str]:
    """执行动作；命中安全闸口时强制确认。全部动作写审计日志。"""
    name = str(action.get("name", "")).strip()
    args = action.get("args", {}) or {}
    require = safety.needs_confirm(cfg, name, args)

    allowed = True
    if require:
        prompt = safety.format_confirm(name, args)
        allowed = True if auto_confirm else safety.confirm_interactive(prompt)

    if not allowed:
        safety.audit(cfg, {"session_id": session_id, "action": name, "args": args,
                           "result": "用户拒绝", "risk": "high"})
        return False, "（已取消该操作）"

    ok, out = _run(name, args)
    safety.audit(cfg, {"session_id": session_id, "action": name, "args": args,
                       "result": ("成功" if ok else "失败") + "｜" + str(out)[:200],
                       "risk": "high" if require else "low"})
    return ok, out
