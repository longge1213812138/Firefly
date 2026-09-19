"""Harness：任务编排与执行框架。

设计目标：
- 陪伴对话与自动化任务执行无缝交织
- 支持连续下达多个命令，任务排队执行
- 任务执行不阻塞对话，对话期间可随时查询进度
- 所有安全闸口（硬闸口）完整保留

核心组件：
- TaskState: 任务状态枚举（pending/running/completed/failed/cancelled）
- Task: 任务数据类（id/状态/参数/结果/时间戳）
- TaskQueue: 线程安全的任务队列（入队/出队/状态查询/取消）
- AsyncExecutor: 异步执行器（后台线程池，任务调度，超时控制）
"""
from __future__ import annotations

import enum
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import safety


# 任务状态枚举
class TaskState(enum.Enum):
    """任务生命周期状态。"""
    PENDING = "pending"          # 排队等待执行
    RUNNING = "running"          # 正在执行
    COMPLETED = "completed"      # 执行完成
    FAILED = "failed"            # 执行失败
    CANCELLED = "cancelled"      # 被用户取消
    CONFIRMING = "confirming"    # 等待用户确认（硬闸口）

    @property
    def is_terminal(self) -> bool:
        """是否为终态（不会再变化）。"""
        return self in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED)

    @property
    def display(self) -> str:
        """中文显示名。"""
        return {
            TaskState.PENDING: "排队中",
            TaskState.RUNNING: "执行中",
            TaskState.COMPLETED: "已完成",
            TaskState.FAILED: "失败",
            TaskState.CANCELLED: "已取消",
            TaskState.CONFIRMING: "等待确认",
        }.get(self, self.value)


# 任务回调类型
OnTaskUpdate = Optional[Callable[["Task"], None]]


@dataclass
class Task:
    """一个任务的完整生命周期数据。"""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str = ""                          # 任务名称（如 "pi_agent"、"run_command"）
    args: dict = field(default_factory=dict) # 任务参数
    state: TaskState = TaskState.PENDING
    result: Any = None                      # 执行结果（成功时）
    error: str = ""                         # 错误信息（失败时）
    backend: str = ""                       # 执行后端（如 "pi"）
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    completed_at: float = 0.0
    progress: str = ""                      # 进度描述（如 "Step 2/5"）
    output_lines: list[str] = field(default_factory=list)  # 实时输出行
    _cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def duration(self) -> float:
        """已执行时长（秒）。"""
        if self.started_at <= 0:
            return 0.0
        end = self.completed_at if self.completed_at > 0 else time.time()
        return end - self.started_at

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def cancel(self) -> bool:
        """请求取消任务。返回是否成功（终态任务无法取消）。"""
        if self.state.is_terminal:
            return False
        self._cancel_event.set()
        return True

    def to_dict(self) -> dict:
        """导出为可序列化字典（不含锁和事件）。"""
        return {
            "id": self.id,
            "name": self.name,
            "args": self.args,
            "state": self.state.value,
            "result": self.result if self.state == TaskState.COMPLETED else None,
            "error": self.error,
            "backend": self.backend,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration": round(self.duration, 2),
            "progress": self.progress,
            "output_count": len(self.output_lines),
        }

    def summary(self) -> str:
        """人话摘要（用于对话播报和 GUI 显示）。"""
        if self.state == TaskState.PENDING:
            return f"任务 {self.id}（{self.name}）排队中"
        if self.state == TaskState.RUNNING:
            dur = f"{self.duration:.0f}秒" if self.duration > 0 else "刚开始"
            prog = f"，{self.progress}" if self.progress else ""
            return f"任务 {self.id}（{self.name}）执行中{prog}，已耗时{dur}"
        if self.state == TaskState.COMPLETED:
            return f"任务 {self.id}（{self.name}）已完成，耗时{self.duration:.1f}秒"
        if self.state == TaskState.FAILED:
            return f"任务 {self.id}（{self.name}）失败：{self.error[:100]}"
        if self.state == TaskState.CANCELLED:
            return f"任务 {self.id}（{self.name}）已取消"
        if self.state == TaskState.CONFIRMING:
            return f"任务 {self.id}（{self.name}）等待确认"
        return f"任务 {self.id}（{self.name}）状态={self.state.value}"


class TaskQueue:
    """线程安全的任务队列，支持按 ID 查询、取消、历史追溯。

    内部用 OrderedDict 保持插入顺序，便于实现 FIFO 出队。
    已完成的任务会移入 _history（保留最近 N 条）。
    """

    def __init__(self, max_size: int = 50, history_size: int = 20):
        self._pending: OrderedDict[str, Task] = OrderedDict()
        self._active: dict[str, Task] = {}  # 运行中的任务
        self._history: OrderedDict[str, Task] = OrderedDict()  # 已完成的任务
        self._max_size = max_size
        self._history_size = history_size
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self._on_update: OnTaskUpdate = None

    def set_on_update(self, callback: OnTaskUpdate) -> None:
        """设置任务状态变更回调（用于 GUI/桌宠更新）。"""
        self._on_update = callback

    def enqueue(self, task: Task) -> tuple[bool, str]:
        """入队。返回 (成功, 消息)。"""
        with self._lock:
            if len(self._pending) + len(self._active) >= self._max_size:
                return False, f"队列已满（最多 {self._max_size} 个任务）"
            self._pending[task.id] = task
            self._not_empty.notify()
            self._notify_update(task)
            return True, f"任务 {task.id} 已入队"

    def dequeue(self, timeout: float = 1.0) -> Optional[Task]:
        """出队（阻塞直到有任务或超时）。返回 None 表示超时。"""
        with self._not_empty:
            if not self._pending:
                self._not_empty.wait(timeout=timeout)
            if not self._pending:
                return None
            task_id, task = self._pending.popitem(last=False)
            task.state = TaskState.RUNNING
            task.started_at = time.time()
            self._active[task_id] = task
            self._notify_update(task)
            return task

    def complete(self, task: Task, success: bool, result: Any = None, error: str = "") -> None:
        """标记任务完成（由执行器调用）。"""
        with self._lock:
            task.completed_at = time.time()
            if task.is_cancelled:
                task.state = TaskState.CANCELLED
            elif success:
                task.state = TaskState.COMPLETED
                task.result = result
            else:
                task.state = TaskState.FAILED
                task.error = error
            # 从 active 移到 history
            self._active.pop(task.id, None)
            self._history[task.id] = task
            # 修剪 history
            while len(self._history) > self._history_size:
                self._history.popitem(last=False)
            self._notify_update(task)

    def get(self, task_id: str) -> Optional[Task]:
        """按 ID 查找任务（含队列、执行中、历史）。"""
        with self._lock:
            if task_id in self._pending:
                return self._pending[task_id]
            if task_id in self._active:
                return self._active[task_id]
            if task_id in self._history:
                return self._history[task_id]
            return None

    def cancel(self, task_id: str) -> tuple[bool, str]:
        """取消任务。返回 (成功, 消息)。"""
        with self._lock:
            # 先查排队中的
            if task_id in self._pending:
                task = self._pending.pop(task_id)
                task.state = TaskState.CANCELLED
                task.completed_at = time.time()
                self._history[task_id] = task
                self._notify_update(task)
                return True, f"任务 {task_id} 已取消（尚未执行）"
            # 再查执行中的
            if task_id in self._active:
                task = self._active[task_id]
                if task.cancel():
                    return True, f"任务 {task_id} 取消请求已发送（执行器将尽快终止）"
                return False, f"任务 {task_id} 已在终态，无法取消"
            return False, f"未找到任务 {task_id}"

    def pending_ids(self) -> list[str]:
        """返回排队中的任务 ID 列表。"""
        with self._lock:
            return list(self._pending.keys())

    def active_ids(self) -> list[str]:
        """返回执行中的任务 ID 列表。"""
        with self._lock:
            return list(self._active.keys())

    def all_tasks(self) -> list[Task]:
        """返回所有任务（按状态分组：执行中 → 排队中 → 历史）。"""
        with self._lock:
            result = list(self._active.values())
            result.extend(self._pending.values())
            result.extend(reversed(list(self._history.values())))
            return result

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def _notify_update(self, task: Task) -> None:
        """触发状态更新回调（在锁内调用，注意不要死锁）。"""
        if self._on_update:
            try:
                self._on_update(task)
            except Exception:
                pass


class AsyncExecutor:
    """异步任务执行器：后台线程从队列取任务并执行。

    支持：
    - 并发控制（最大同时执行数）
    - 超时控制
    - 取消响应
    - 实时输出回调
    """

    def __init__(self, queue: TaskQueue, max_concurrent: int = 2, cfg: dict | None = None):
        self.queue = queue
        self.max_concurrent = max_concurrent
        self.cfg = cfg or {}
        self._workers: list[threading.Thread] = []
        self._semaphore = threading.Semaphore(max_concurrent)
        self._running = False
        self._executor_fn: dict[str, Callable] = {}  # name -> executor function
        self._on_output: Optional[Callable[[Task, str], None]] = None

    def register_executor(self, name: str, fn: Callable[[Task, dict], tuple[bool, Any, str]]) -> None:
        """注册任务执行函数。

        fn 签名：fn(task, cfg) -> (success, result, error)
        """
        self._executor_fn[name] = fn

    def set_on_output(self, callback: Optional[Callable[[Task, str], None]]) -> None:
        """设置实时输出回调（每行输出触发一次）。"""
        self._on_output = callback

    def start(self) -> None:
        """启动工作线程。"""
        if self._running:
            return
        self._running = True
        for i in range(self.max_concurrent):
            t = threading.Thread(target=self._worker_loop, daemon=True,
                                 name=f"harness-worker-{i}")
            t.start()
            self._workers.append(t)

    def stop(self) -> None:
        """停止工作线程（等待当前任务完成）。"""
        self._running = False
        # 唤醒所有等待的 worker
        with self.queue._not_empty:
            self.queue._not_empty.notify_all()
        for t in self._workers:
            t.join(timeout=5)
        self._workers.clear()

    def _worker_loop(self) -> None:
        """工作线程主循环：取任务 → 执行 → 报告结果。"""
        while self._running:
            task = self.queue.dequeue(timeout=1.0)
            if task is None:
                continue
            if task.is_cancelled:
                self.queue.complete(task, success=False, error="任务已被取消")
                self._semaphore.release()
                continue
            try:
                self._execute_task(task)
            except Exception as exc:
                self.queue.complete(task, success=False, error=f"执行异常：{exc}")
            finally:
                self._semaphore.release()

    def _execute_task(self, task: Task) -> None:
        """执行单个任务。"""
        executor = self._executor_fn.get(task.name)
        if executor is None:
            self.queue.complete(task, success=False, error=f"未知任务类型：{task.name}")
            return

        # 检查取消
        if task.is_cancelled:
            self.queue.complete(task, success=False, error="任务已被取消")
            return

        try:
            success, result, error = executor(task, self.cfg)
            self.queue.complete(task, success=success, result=result, error=error)
        except Exception as exc:
            self.queue.complete(task, success=False, error=f"执行器异常：{exc}")


def make_harness(cfg: dict) -> tuple[Optional[TaskQueue], Optional[AsyncExecutor]]:
    """根据配置创建 harness 实例。未启用时返回 (None, None)。"""
    hcfg = cfg.get("harness", {})
    if not hcfg.get("enabled", False):
        return None, None

    max_size = int(hcfg.get("queue_size", 50))
    history_size = int(hcfg.get("history_size", 20))
    max_concurrent = int(hcfg.get("max_concurrent", 2))

    queue = TaskQueue(max_size=max_size, history_size=history_size)
    executor = AsyncExecutor(queue, max_concurrent=max_concurrent, cfg=cfg)
    executor.start()
    return queue, executor
