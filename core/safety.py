"""安全闸口：危险操作强制二次确认 + 全量操作审计日志（不可关闭）。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def needs_confirm(cfg: dict, action_name: str, args: dict) -> bool:
    """命中高风险关键词或高风险动作时，必须二次确认。"""
    dangerous_actions = {"write_file", "run_command", "delete", "send"}
    if action_name in dangerous_actions:
        return True
    blob = f"{action_name} {json.dumps(args or {}, ensure_ascii=False)}".lower()
    for kw in cfg.get("safety", {}).get("require_confirm_keywords", []):
        if str(kw).lower() in blob:
            return True
    return False


def format_confirm(action_name: str, args: dict) -> str:
    return f"即将执行操作：{action_name}  参数：{json.dumps(args or {}, ensure_ascii=False)}"


def confirm_interactive(prompt: str, default_no: bool = True) -> bool:
    """命令行二次确认。返回 True 表示允许执行。"""
    try:
        ans = input(f"{prompt}\n  确认执行？(y/N)：").strip().lower()
    except EOFError:
        return False
    if ans in ("y", "yes", "是"):
        return True
    if default_no is False and ans == "":
        return True
    return False


def audit(cfg: dict, entry: dict) -> None:
    """写入本地审计日志（JSONL，追加）。"""
    log_path = Path(cfg.get("safety", {}).get("audit_log", "audit.log"))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = dict(entry)
    entry.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
