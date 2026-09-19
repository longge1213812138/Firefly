"""安全闸口：危险操作强制二次确认 + 全量操作审计日志（不可关闭）。

两级闸口（v0.2 起）：
- 普通闸口（require）：写入文件、执行命令、复制/移动等操作需要二次确认；
  在可信自动化流程里可用 auto_confirm 放行（仅供离线自检等内部场景）。
- 硬闸口（hard）：删除、覆盖已有文件、外发类操作——无论配置与流程如何，
  都必须当面获得用户确认，代码层面不可配置关闭（对应需求 F-06 AC1）。

确认入口支持回调注入（confirm_fn），命令行用 input()，GUI 用弹窗——
由调用方决定交互形式，闸口规则本身不变。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

# 硬闸口动作：本质不可逆/外发，永远需要当面确认
# pi_agent 会驱动外部 agent 读写文件、执行命令，效果等价于不可逆操作，故列为硬闸口
# 注意：harness 异步执行模式同样受此约束——任务提交前必须通过确认，无法绕过
HARD_ACTIONS = {"delete_file", "delete_dir", "send_file", "send_message", "pi_agent"}
# 命令行中出现这些词 → 硬闸口
HARD_CMD_KEYWORDS = (
    "rm ", "rm -", "del ", "del/", "rd ", "rmdir", "erase",
    "format", "格式化", "remove-item", "remove_item", "shred",
)
# 参数里出现这些词（如文件内容/路径/命令包含）→ 硬闸口
HARD_ARG_KEYWORDS = (
    "删除", "删掉", "清除", "清空", "格式化",
    "覆盖", "外发", "发送", "上传", "邮件", "短信", "发布", "付款", "转账",
)
# 保护目录：永不执行删除类操作
PROTECTED_PATHS = ("c:\\", "d:\\", "c:/", "d:/", "/")


def is_hard(cfg: dict, action_name: str, args: dict) -> bool:
    """是否命中硬闸口（必须当面确认，不可被 auto_confirm 跳过）。"""
    name = str(action_name or "").strip().lower()
    a = args or {}
    if name in HARD_ACTIONS:
        return True
    if name == "write_file":
        # 覆盖已有文件 = 硬闸口；写新文件走普通闸口
        try:
            return Path(str(a.get("path", ""))).expanduser().exists()
        except OSError:
            return False
    if name in ("copy_file", "move_file"):
        try:
            return Path(str(a.get("dst", ""))).expanduser().exists()
        except OSError:
            return False
    if name == "run_command":
        cmd = str(a.get("cmd", "")).lower()
        if any(k in cmd for k in HARD_CMD_KEYWORDS):
            return True
    blob = f"{name} {json.dumps(a, ensure_ascii=False)}".lower()
    return any(k in blob for k in HARD_ARG_KEYWORDS)


def needs_confirm(cfg: dict, action_name: str, args: dict) -> bool:
    """是否需要二次确认（普通闸口或硬闸口都算）。"""
    name = str(action_name or "").strip().lower()
    dangerous_actions = {
        "write_file", "run_command", "delete", "send",
        "delete_file", "delete_dir", "move_file", "copy_file", "send_file", "send_message",
        "batch_rename", "organize_files", "compress_files", "extract_archive",
        "pi_agent",
    }
    if name in dangerous_actions:
        return True
    if is_hard(cfg, name, args):
        return True
    blob = f"{name} {json.dumps(args or {}, ensure_ascii=False)}".lower()
    for kw in cfg.get("safety", {}).get("require_confirm_keywords", []):
        if str(kw).lower() in blob:
            return True
    return False


def format_confirm(action_name: str, args: dict) -> str:
    return f"即将执行操作：{action_name}  参数：{json.dumps(args or {}, ensure_ascii=False)}"


def format_confirm_list(actions: list[dict]) -> str:
    """撤销清单（F-06 AC3）：执行前把将要执行的全部操作列给用户过目。"""
    lines = ["本次将要执行以下操作："]
    for i, act in enumerate(actions, 1):
        name = str(act.get("name", "?"))
        args = act.get("args", {}) or {}
        if name == "run_command":
            lines.append(f"  {i}. 执行命令：{args.get('cmd', '')}")
        elif name == "write_file":
            n = len(str(args.get("content", "")))
            lines.append(f"  {i}. 写入文件：{args.get('path', '')}（{n} 字）")
        elif name in ("copy_file", "move_file"):
            verb = "复制" if name == "copy_file" else "移动"
            lines.append(f"  {i}. {verb}：{args.get('src', '')} → {args.get('dst', '')}")
        elif name in ("delete_file", "delete_dir"):
            lines.append(f"  {i}. 删除{'目录' if name == 'delete_dir' else '文件'}：{args.get('path', '')}")
        elif name == "pi_agent":
            ro = "（只读）" if args.get("read_only") else "（可读写，会动文件/命令）"
            lines.append(f"  {i}. 调用 Pi 完成：{str(args.get('task', ''))[:160]}{ro}")
        else:
            lines.append(f"  {i}. {name}：{json.dumps(args, ensure_ascii=False)[:120]}")
    lines.append("确认执行吗？（删除/覆盖/外发类操作必须当面确认，无法跳过）")
    return "\n".join(lines)


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
