"""Harness 中间件：任务状态与对话上下文整合。

职责：
- 检测用户是否在询问任务状态
- 生成任务状态上下文块（注入 system prompt）
- 将任务结果写入记忆
- 提供任务相关的人话回复
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .harness import Task, TaskQueue

# 任务状态查询关键词
_TASK_QUERY_PATTERNS = (
    r"任务.*怎么样",
    r"任务.*进度",
    r"任务.*状态",
    r"任务.*做完",
    r"任务.*结果",
    r"(Pi|pi).*做完",
    r"(Pi|pi).*结果",
    r"(Pi|pi).*怎么样",
    r"(Pi|pi).*进度",
    r"后台.*任务",
    r"执行.*任务",
    r"排队.*任务",
    r"有哪些.*任务",
    r"查看.*任务",
    r"取消.*任务",
    r"任务.*取消",
)

_CANCEL_PATTERNS = (
    r"取消.*任务",
    r"停止.*任务",
    r"终止.*任务",
    r"不要.*任务",
    r"算了.*任务",
)


def is_task_query(text: str) -> bool:
    """用户是否在询问任务状态。"""
    if not text:
        return False
    t = text.lower()
    return any(re.search(p, t) for p in _TASK_QUERY_PATTERNS)


def is_cancel_request(text: str) -> tuple[bool, str]:
    """用户是否在请求取消任务。返回 (是否取消请求, 提取的任务ID)。"""
    if not text:
        return False, ""
    t = text.lower()
    if not any(re.search(p, t) for p in _CANCEL_PATTERNS):
        return False, ""
    # 尝试提取任务 ID（8位十六进制）
    m = re.search(r"[0-9a-f]{8}", text.lower())
    return True, m.group(0) if m else ""


def build_task_context(queue: "TaskQueue | None") -> str:
    """生成任务状态上下文块（注入 system prompt 让 LLM 知道当前任务状况）。"""
    if queue is None:
        return ""
    tasks = queue.all_tasks()
    if not tasks:
        return ""

    # 分组统计
    pending = [t for t in tasks if t.state.value == "pending"]
    running = [t for t in tasks if t.state.value == "running"]
    completed = [t for t in tasks if t.state.value == "completed"]
    failed = [t for t in tasks if t.state.value == "failed"]

    lines = ["【当前任务状况】"]
    if running:
        lines.append(f"执行中（{len(running)} 个）：")
        for t in running[:3]:
            lines.append(f"  - {t.id} {t.name}：{t.progress or '执行中'}，已耗时{t.duration:.0f}秒")
    if pending:
        lines.append(f"排队中（{len(pending)} 个）：")
        for t in pending[:3]:
            lines.append(f"  - {t.id} {t.name}")
    if completed:
        lines.append(f"最近完成（{len(completed)} 个）：")
        for t in completed[-3:]:
            lines.append(f"  - {t.id} {t.name}：耗时{t.duration:.1f}秒")
    if failed:
        lines.append(f"最近失败（{len(failed)} 个）：")
        for t in failed[-2:]:
            lines.append(f"  - {t.id} {t.name}：{t.error[:60]}")

    lines.append("（用户询问任务状态时，请根据以上信息回答）")
    return "\n".join(lines)


def _get_task_field(task, field, default=""):
    """从 Task 对象或 dict 中安全取值。"""
    if isinstance(task, dict):
        return task.get(field, default)
    return getattr(task, field, default)


def _get_task_state_value(task) -> str:
    """获取任务状态值（兼容 Task 对象和 dict）。"""
    if isinstance(task, dict):
        return task.get('state', 'pending')
    state = task.state
    return state.value if hasattr(state, 'value') else str(state)


def format_task_list(tasks: list) -> str:
    """格式化任务列表为人话。支持 Task 对象或 dict 列表。"""
    if not tasks:
        return "当前没有任何任务。"

    lines = []
    pending = [t for t in tasks if _get_task_state_value(t) == "pending"]
    running = [t for t in tasks if _get_task_state_value(t) == "running"]
    completed = [t for t in tasks if _get_task_state_value(t) == "completed"]
    failed = [t for t in tasks if _get_task_state_value(t) == "failed"]

    if running:
        lines.append("🔄 执行中：")
        for t in running:
            dur = _get_task_field(t, 'duration', 0)
            dur_str = f"{dur:.0f}秒" if dur > 0 else "刚开始"
            prog = _get_task_field(t, 'progress', '')
            prog_str = f"（{prog}）" if prog else ""
            lines.append(f"  • {_get_task_field(t, 'id')} {_get_task_field(t, 'name')}{prog_str}，已耗时{dur_str}")
    if pending:
        lines.append("⏳ 排队中：")
        for t in pending:
            lines.append(f"  • {_get_task_field(t, 'id')} {_get_task_field(t, 'name')}")
    if completed:
        lines.append("✅ 已完成：")
        for t in completed[-5:]:
            dur = _get_task_field(t, 'duration', 0)
            lines.append(f"  • {_get_task_field(t, 'id')} {_get_task_field(t, 'name')}，耗时{dur:.1f}秒")
    if failed:
        lines.append("❌ 失败：")
        for t in failed[-3:]:
            error = _get_task_field(t, 'error', '')
            lines.append(f"  • {_get_task_field(t, 'id')} {_get_task_field(t, 'name')}：{error[:60]}")

    if not lines:
        return "当前没有任何任务。"
    return "\n".join(lines)


def format_task_detail(task: "Task") -> str:
    """格式化单个任务详情。"""
    lines = [f"任务 {task.id}（{task.name}）"]
    lines.append(f"状态：{task.state.display}")
    if task.progress:
        lines.append(f"进度：{task.progress}")
    if task.started_at > 0:
        lines.append(f"已耗时：{task.duration:.1f}秒")
    if task.output_lines:
        lines.append(f"输出行数：{len(task.output_lines)}")
        # 显示最后几行输出
        last_lines = task.output_lines[-5:]
        lines.append("最近输出：")
        for line in last_lines:
            lines.append(f"  > {line[:100]}")
    if task.state.value == "completed" and task.result:
        result_str = str(task.result)
        if len(result_str) > 500:
            result_str = result_str[:500] + "..."
        lines.append(f"结果：{result_str}")
    if task.state.value == "failed" and task.error:
        lines.append(f"错误：{task.error[:200]}")
    return "\n".join(lines)


# ---------- 连续命令解析 ----------

# 连续命令分隔符
_SEQ_SEPARATORS = (
    r"然后",
    r"接着",
    r"再",
    r"之后",
    r"接下来",
    r"同时",
    r"并且",
    r"，然后",
    r"，接着",
    r"，再",
    r"；然后",
    r"；接着",
    r"；再",
)


def parse_sequential_commands(text: str) -> list[str]:
    """解析连续命令。返回命令列表。

    示例：
        "帮我看看项目结构，然后列出所有文件" -> ["帮我看看项目结构", "列出所有文件"]
        "先读 README，再检查 config.json" -> ["读 README", "检查 config.json"]
    """
    if not text:
        return []

    # 移除开头的 "先"
    t = text.strip()
    if t.startswith("先"):
        t = t[1:].strip()

    # 按分隔符分割
    parts = [t]
    for sep in _SEQ_SEPARATORS:
        new_parts = []
        for part in parts:
            splits = re.split(sep, part, maxsplit=1)
            new_parts.extend([s.strip() for s in splits if s.strip()])
        parts = new_parts

    # 过滤空串和太短的片段
    return [p for p in parts if len(p) >= 2]


def is_sequential_command(text: str) -> bool:
    """判断是否包含连续命令。"""
    if not text:
        return False
    t = text.lower()
    return any(re.search(sep, t) for sep in _SEQ_SEPARATORS)


def format_sequential_plan(commands: list[str]) -> str:
    """格式化连续命令计划为人话。"""
    if not commands:
        return ""
    if len(commands) == 1:
        return f"执行：{commands[0]}"
    lines = ["将按顺序执行以下操作："]
    for i, cmd in enumerate(commands, 1):
        lines.append(f"  {i}. {cmd}")
    return "\n".join(lines)
