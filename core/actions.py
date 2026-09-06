"""电脑操作工具（只读/低风险直接执行；写入/命令需确认；删除/覆盖/外发为硬闸口）。

大模型通过在回复中输出一行或多行 ACTION:{"name":..., "args":{...}} 来请求执行。
安全规则见 core/safety.py：硬闸口（删除/覆盖已有文件/外发）永远当面确认。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from . import safety

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DESCRIPTIONS = (
    "可用操作（在回复最后另起一行输出 ACTION:{\"name\":\"操作名\",\"args\":{...}} 来调用，可多行一次提交批量操作）：\n"
    "1. get_time 无参数 —— 查询当前时间\n"
    "2. list_dir {\"path\":\"目录\"} —— 列出目录内容\n"
    "3. read_file {\"path\":\"文件\", \"max_chars\":2000} —— 读取文本文件内容\n"
    "4. open_path {\"path\":\"文件或目录\"} —— 打开文件/文件夹/程序\n"
    "5. open_url {\"url\":\"https://...\"} —— 用浏览器打开网页\n"
    "6. write_file {\"path\":\"文件\", \"content\":\"内容\"} —— 写入文件（需确认；覆盖已有文件必须当面确认）\n"
    "7. run_command {\"cmd\":\"命令\"} —— 执行命令行（需确认；含删除/格式化的命令必须当面确认）\n"
    "8. copy_file {\"src\":\"源\", \"dst\":\"目标\"} —— 复制文件（需确认）\n"
    "9. move_file {\"src\":\"源\", \"dst\":\"目标\"} —— 移动文件（需确认）\n"
    "10. delete_file {\"path\":\"文件\"} —— 删除单个文件（硬闸口：必须当面确认）\n"
    "规则：删除、覆盖、外发、付款类操作必须先当面征求用户同意，绝不自作主张；"
    "批量整理类任务把多个 ACTION 各占一行一起提交，等用户在清单上一次性确认。"
)


def _protected(p: Path) -> bool:
    """根目录 / 主目录 / 项目目录本身，禁止删除。"""
    try:
        s = str(p.resolve()).lower().rstrip("\\/")
        home = str(Path.home()).lower().rstrip("\\/")
        prot = tuple(x.rstrip("\\/").lower() for x in PROTECTED_PATHS)
        return s in prot or s == home or s == str(PROJECT_ROOT).lower()
    except OSError:
        return True  # 解析失败一律当保护对象处理


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
        if name == "copy_file":
            src = Path(args.get("src", "")).expanduser()
            dst = Path(args.get("dst", "")).expanduser()
            if not src.is_file():
                return False, f"源文件不存在：{src}"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            return True, f"已复制 {src} → {dst}"
        if name == "move_file":
            src = Path(args.get("src", "")).expanduser()
            dst = Path(args.get("dst", "")).expanduser()
            if not src.exists():
                return False, f"源不存在：{src}"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            return True, f"已移动 {src} → {dst}"
        if name == "delete_file":
            p = Path(args.get("path", "")).expanduser()
            if _protected(p):
                return False, f"拒绝删除：{p} 是受保护的目录（根目录/主目录/项目目录）"
            if not p.is_file():
                return False, f"文件不存在：{p}"
            p.unlink()
            return True, f"已删除文件 {p}"
        if name == "delete_dir":
            p = Path(args.get("path", "")).expanduser()
            if _protected(p):
                return False, f"拒绝删除：{p} 是受保护的目录（根目录/主目录/项目目录）"
            if not p.is_dir():
                return False, f"目录不存在：{p}"
            shutil.rmtree(p)
            return True, f"已删除目录 {p}"
        return False, f"未知操作：{name}"
    except Exception as exc:  # noqa: BLE001
        return False, f"执行失败：{exc}"


def execute(
    cfg: dict,
    action: dict,
    auto_confirm: bool = False,
    session_id: str = "",
    confirm_fn=None,
) -> tuple[bool, str]:
    """执行单个动作；命中闸口时确认。

    confirm_fn(prompt)->bool：确认回调，默认命令行 input()；GUI 传弹窗回调。
    硬闸口（删除/覆盖已有文件/外发）即使 auto_confirm=True 也必须经 confirm_fn 当面确认。
    """
    name = str(action.get("name", "")).strip()
    args = action.get("args", {}) or {}
    require = safety.needs_confirm(cfg, name, args)
    hard = safety.is_hard(cfg, name, args)
    confirm_fn = confirm_fn or safety.confirm_interactive

    allowed = True
    if require:
        prompt = safety.format_confirm(name, args)
        if hard:
            allowed = bool(confirm_fn(prompt))  # 硬闸口：任何自动流程都绕不过
        else:
            allowed = True if auto_confirm else bool(confirm_fn(prompt))

    if not allowed:
        safety.audit(cfg, {"session_id": session_id, "action": name, "args": args,
                           "result": "用户拒绝", "risk": "high", "hard": hard})
        return False, "（已取消该操作）"

    ok, out = _run(name, args)
    safety.audit(cfg, {"session_id": session_id, "action": name, "args": args,
                       "result": ("成功" if ok else "失败") + "｜" + str(out)[:200],
                       "risk": "high" if require else "low", "hard": hard})
    return ok, out
