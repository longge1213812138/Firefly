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
from .config import resolve_root

# 打包成 exe 后要指向 exe 所在目录（受保护目录判断要用）
PROJECT_ROOT = resolve_root()

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
    "11. search_files {\"path\":\"搜索范围目录\", \"pattern\":\"文件名模式\", \"content\":\"内容关键词\"} —— 搜索文件（按名称和/或内容）\n"
    "12. batch_rename {\"path\":\"目录\", \"pattern\":\"原模式\", \"replacement\":\"替换模式\", \"regex\":false} —— 批量重命名文件（需确认）\n"
    "13. organize_files {\"src\":\"源目录\", \"dst\":\"目标目录\", \"strategy\":\"type|date\"} —— 按类型或日期整理文件（需确认）\n"
    "14. compress_files {\"src\":\"源目录或文件\", \"dst\":\"压缩包路径\"} —— 压缩文件/目录为zip（需确认）\n"
    "15. extract_archive {\"src\":\"压缩包路径\", \"dst\":\"解压目标目录\"} —— 解压zip文件（需确认）\n"
    "16. get_system_info 无参数 —— 获取系统信息（CPU/内存/磁盘）\n"
    "17. pi_agent {\"task\":\"要交给 Pi 的完整任务描述\", \"backend\":\"pi\", \"read_only\":false} —— "
    "调用 Pi 编程智能体（外部 CLI）完成编程/查资料/改文件类任务（需确认，且属硬闸口）\n"
    "规则：删除、覆盖、外发、付款类操作必须先当面征求用户同意，绝不自作主张；"
    "批量整理类任务把多个 ACTION 各占一行一起提交，等用户在清单上一次性确认；"
    "**pi_agent 只在用户明确点名要求（例如「用 Pi 帮我…」「让 Pi 来做」）时才可发起，绝不能自作主张调用。**"
)


def _protected(p: Path) -> bool:
    """根目录 / 主目录 / 项目目录本身，禁止删除。"""
    try:
        s = str(p.resolve()).lower().rstrip("\\/")
        home = str(Path.home()).lower().rstrip("\\/")
        prot = tuple(x.rstrip("\\/").lower() for x in safety.PROTECTED_PATHS)
        return s in prot or s == home or s == str(PROJECT_ROOT).lower()
    except OSError:
        return True  # 解析失败一律当保护对象处理


def _run_pi_agent(args: dict, cfg: dict | None) -> tuple[bool, str]:
    """把任务转发给外部 agent（默认 Pi）。

    注意：陪伴端的长期记忆不会被共享出去；
    这里只做「转发任务 → 取回结果」，调用前后的确认与审计由 execute() 负责。
    """
    from . import agent_backend

    task = str(args.get("task", "") or "").strip()
    if not task:
        return False, "pi_agent 缺少 task（要交给 Pi 的任务描述）"

    if cfg is None:
        from .config import load_config

        cfg = load_config()
    if not cfg.get("pi", {}).get("enabled", True):
        return False, "config.json 里 pi.enabled=false，已停用外部 agent 调用"

    backend_name = str(args.get("backend") or cfg.get("pi", {}).get("backend") or "pi").strip() or "pi"
    backend = agent_backend.get_backend(backend_name, cfg)
    if backend is None:
        known = ", ".join(agent_backend.list_backends() or []) or "无"
        return False, f"未知的 agent 后端：{backend_name}（已注册：{known}）"
    if not backend.available():
        return False, f"{backend.describe()} 不可用——请先安装，或在 config.json 的 pi.cli_path 填绝对路径"

    res = backend.run(
        task,
        read_only=bool(args.get("read_only", False)),
        cwd=args.get("cwd") or None,
    )
    if not res.ok:
        return False, f"{backend_name} 未能完成：{res.error or '无输出'}"
    return True, res.text


def _run(name: str, args: dict, cfg: dict | None = None) -> tuple[bool, str]:
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
        if name == "search_files":
            base = Path(args.get("path", ".")).expanduser()
            pattern = args.get("pattern", "*")
            content = args.get("content", "")
            if not base.is_dir():
                return False, f"搜索目录不存在：{base}"
            matches = []
            for p in base.rglob(pattern):
                if len(matches) >= 50:
                    break
                if content and p.is_file():
                    try:
                        text = p.read_text(encoding="utf-8", errors="ignore")[:50000]
                        if content.lower() not in text.lower():
                            continue
                    except Exception:
                        continue
                matches.append(str(p.relative_to(base)))
            if not matches:
                return True, f"未找到匹配的文件（模式：{pattern}，内容：{content or '无'}）"
            return True, f"找到 {len(matches)} 个文件：\n" + "\n".join(matches)
        if name == "batch_rename":
            base = Path(args.get("path", "")).expanduser()
            pat = args.get("pattern", "")
            repl = args.get("replacement", "")
            use_regex = args.get("regex", False)
            if not base.is_dir():
                return False, f"目录不存在：{base}"
            if not pat:
                return False, "请提供原模式（pattern）"
            renamed = []
            for p in sorted(base.iterdir()):
                if use_regex:
                    import re
                    new_name = re.sub(pat, repl, p.name)
                else:
                    new_name = p.name.replace(pat, repl)
                if new_name != p.name:
                    new_path = p.parent / new_name
                    p.rename(new_path)
                    renamed.append(f"{p.name} → {new_name}")
            if not renamed:
                return True, "没有文件匹配该模式"
            return True, f"已重命名 {len(renamed)} 个文件：\n" + "\n".join(renamed[:20])
        if name == "organize_files":
            src = Path(args.get("src", "")).expanduser()
            dst = Path(args.get("dst", "")).expanduser()
            strategy = args.get("strategy", "type")
            if not src.is_dir():
                return False, f"源目录不存在：{src}"
            dst.mkdir(parents=True, exist_ok=True)
            moved = []
            for p in sorted(src.iterdir()):
                if not p.is_file():
                    continue
                if strategy == "type":
                    ext = p.suffix.lower().lstrip(".")
                    target_dir = dst / (ext or "无扩展名")
                elif strategy == "date":
                    ts = p.stat().st_mtime
                    target_dir = dst / datetime.fromtimestamp(ts).strftime("%Y-%m")
                else:
                    target_dir = dst
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / p.name
                shutil.move(str(p), str(target))
                moved.append(f"{p.name} → {target_dir.name}/")
            if not moved:
                return True, "没有文件需要整理"
            return True, f"已整理 {len(moved)} 个文件到 {dst}"
        if name == "compress_files":
            src = Path(args.get("src", "")).expanduser()
            dst = Path(args.get("dst", "")).expanduser()
            if not src.exists():
                return False, f"源不存在：{src}"
            import zipfile
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_file():
                with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
                    zf.write(src, src.name)
                return True, f"已压缩 {src.name} → {dst}"
            # 目录
            with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in sorted(src.rglob("*")):
                    if p.is_file():
                        zf.write(p, p.relative_to(src.parent))
            return True, f"已压缩 {src.name}/ → {dst}"
        if name == "extract_archive":
            src = Path(args.get("src", "")).expanduser()
            dst = Path(args.get("dst", "")).expanduser()
            if not src.is_file():
                return False, f"压缩包不存在：{src}"
            import zipfile
            dst.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(src, "r") as zf:
                zf.extractall(dst)
            return True, f"已解压 {src.name} → {dst}"
        if name == "get_system_info":
            import platform
            try:
                import psutil
                has_psutil = True
            except ImportError:
                has_psutil = False
            info = {
                "系统": platform.system(),
                "版本": platform.version(),
                "架构": platform.machine(),
                "处理器": platform.processor(),
            }
            if has_psutil:
                info["CPU核心"] = psutil.cpu_count()
                info["内存总量"] = f"{psutil.virtual_memory().total / (1024**3):.1f} GB"
                info["内存使用"] = f"{psutil.virtual_memory().percent}%"
                disk = psutil.disk_usage("/")
                info["磁盘总量"] = f"{disk.total / (1024**3):.1f} GB"
                info["磁盘使用"] = f"{disk.percent}%"
            else:
                info["提示"] = "安装 psutil 可获取更多系统信息：pip install psutil"
            return True, "\n".join(f"{k}：{v}" for k, v in info.items())
        if name == "pi_agent":
            return _run_pi_agent(args, cfg)
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

    ok, out = _run(name, args, cfg)
    safety.audit(cfg, {"session_id": session_id, "action": name, "args": args,
                       "result": ("成功" if ok else "失败") + "｜" + str(out)[:200],
                       "risk": "high" if require else "low", "hard": hard})
    return ok, out
