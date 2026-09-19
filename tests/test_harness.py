"""Harness 框架单元测试。

覆盖核心场景：
- Task 状态机流转
- TaskQueue 入队/出队/取消
- AsyncExecutor 异步执行
- harness_middleware 查询检测
"""
from __future__ import annotations

import threading
import time
import unittest

# 添加项目根目录到 path
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.harness import Task, TaskQueue, TaskState, AsyncExecutor


class TestTaskState(unittest.TestCase):
    """测试 Task 状态机。"""

    def test_initial_state(self):
        task = Task()
        self.assertEqual(task.state, TaskState.PENDING)

    def test_is_terminal(self):
        self.assertFalse(TaskState.PENDING.is_terminal)
        self.assertFalse(TaskState.RUNNING.is_terminal)
        self.assertFalse(TaskState.CONFIRMING.is_terminal)
        self.assertTrue(TaskState.COMPLETED.is_terminal)
        self.assertTrue(TaskState.FAILED.is_terminal)
        self.assertTrue(TaskState.CANCELLED.is_terminal)

    def test_display(self):
        self.assertEqual(TaskState.PENDING.display, "排队中")
        self.assertEqual(TaskState.RUNNING.display, "执行中")
        self.assertEqual(TaskState.COMPLETED.display, "已完成")
        self.assertEqual(TaskState.FAILED.display, "失败")
        self.assertEqual(TaskState.CANCELLED.display, "已取消")
        self.assertEqual(TaskState.CONFIRMING.display, "等待确认")

    def test_cancel(self):
        task = Task()
        self.assertTrue(task.cancel())
        self.assertTrue(task.is_cancelled)

    def test_cancel_terminal(self):
        task = Task()
        task.state = TaskState.COMPLETED
        self.assertFalse(task.cancel())

    def test_duration(self):
        task = Task()
        self.assertEqual(task.duration, 0.0)
        task.started_at = time.time() - 5
        self.assertAlmostEqual(task.duration, 5.0, delta=0.1)

    def test_summary(self):
        task = Task(id="abc12345", name="pi_agent")
        self.assertIn("abc12345", task.summary())
        self.assertIn("pi_agent", task.summary())

    def test_to_dict(self):
        task = Task(id="abc12345", name="pi_agent", args={"task": "test"})
        d = task.to_dict()
        self.assertEqual(d["id"], "abc12345")
        self.assertEqual(d["name"], "pi_agent")
        self.assertEqual(d["state"], "pending")


class TestTaskQueue(unittest.TestCase):
    """测试 TaskQueue。"""

    def test_enqueue_dequeue(self):
        queue = TaskQueue(max_size=10)
        task = Task(name="test")
        ok, msg = queue.enqueue(task)
        self.assertTrue(ok)
        self.assertEqual(queue.pending_count(), 1)

        dequeued = queue.dequeue(timeout=0.1)
        self.assertIsNotNone(dequeued)
        self.assertEqual(dequeued.id, task.id)
        self.assertEqual(dequeued.state, TaskState.RUNNING)

    def test_queue_full(self):
        queue = TaskQueue(max_size=2)
        queue.enqueue(Task(name="a"))
        queue.enqueue(Task(name="b"))
        ok, msg = queue.enqueue(Task(name="c"))
        self.assertFalse(ok)
        self.assertIn("队列已满", msg)

    def test_cancel_pending(self):
        queue = TaskQueue()
        task = Task(name="test")
        queue.enqueue(task)
        ok, msg = queue.cancel(task.id)
        self.assertTrue(ok)
        self.assertEqual(queue.pending_count(), 0)

    def test_cancel_not_found(self):
        queue = TaskQueue()
        ok, msg = queue.cancel("nonexistent")
        self.assertFalse(ok)

    def test_complete(self):
        queue = TaskQueue()
        task = Task(name="test")
        queue.enqueue(task)
        dequeued = queue.dequeue(timeout=0.1)
        queue.complete(dequeued, success=True, result="done")
        self.assertEqual(dequeued.state, TaskState.COMPLETED)
        self.assertEqual(dequeued.result, "done")

    def test_complete_failed(self):
        queue = TaskQueue()
        task = Task(name="test")
        queue.enqueue(task)
        dequeued = queue.dequeue(timeout=0.1)
        queue.complete(dequeued, success=False, error="failed")
        self.assertEqual(dequeued.state, TaskState.FAILED)
        self.assertEqual(dequeued.error, "failed")

    def test_get(self):
        queue = TaskQueue()
        task = Task(name="test")
        queue.enqueue(task)
        found = queue.get(task.id)
        self.assertIsNotNone(found)
        self.assertEqual(found.id, task.id)

    def test_all_tasks(self):
        queue = TaskQueue()
        queue.enqueue(Task(name="a"))
        queue.enqueue(Task(name="b"))
        tasks = queue.all_tasks()
        self.assertEqual(len(tasks), 2)

    def test_on_update_callback(self):
        queue = TaskQueue()
        updates = []
        queue.set_on_update(lambda t: updates.append(t.state.value))
        task = Task(name="test")
        queue.enqueue(task)
        self.assertIn("pending", updates)


class TestAsyncExecutor(unittest.TestCase):
    """测试 AsyncExecutor。"""

    def test_basic_execution(self):
        queue = TaskQueue()
        executor = AsyncExecutor(queue, max_concurrent=1)
        executor.start()

        def my_executor(task, cfg):
            return True, "result", ""

        executor.register_executor("test", my_executor)

        task = Task(name="test", args={})
        queue.enqueue(task)
        time.sleep(0.5)

        executor.stop()
        self.assertEqual(task.state, TaskState.COMPLETED)
        self.assertEqual(task.result, "result")

    def test_cancel(self):
        queue = TaskQueue()
        executor = AsyncExecutor(queue, max_concurrent=1)
        executor.start()

        def slow_executor(task, cfg):
            time.sleep(5)
            return True, "done", ""

        executor.register_executor("slow", slow_executor)

        task = Task(name="slow", args={})
        queue.enqueue(task)
        time.sleep(0.1)
        task.cancel()
        time.sleep(0.5)

        executor.stop()
        self.assertEqual(task.state, TaskState.CANCELLED)

    def test_unknown_executor(self):
        queue = TaskQueue()
        executor = AsyncExecutor(queue, max_concurrent=1)
        executor.start()

        task = Task(name="unknown", args={})
        queue.enqueue(task)
        time.sleep(0.5)

        executor.stop()
        self.assertEqual(task.state, TaskState.FAILED)
        self.assertIn("未知任务类型", task.error)


class TestHarnessMiddleware(unittest.TestCase):
    """测试 harness_middleware。"""

    def test_is_task_query(self):
        from core.harness_middleware import is_task_query
        self.assertTrue(is_task_query("任务怎么样了"))
        self.assertTrue(is_task_query("Pi 做完了没"))
        self.assertTrue(is_task_query("查看任务进度"))
        self.assertFalse(is_task_query("你好"))

    def test_is_cancel_request(self):
        from core.harness_middleware import is_cancel_request
        is_cancel, _ = is_cancel_request("取消任务 abc12345")
        self.assertTrue(is_cancel)
        is_cancel, task_id = is_cancel_request("取消任务 abc12345")
        self.assertEqual(task_id, "abc12345")

    def test_parse_sequential_commands(self):
        from core.harness_middleware import parse_sequential_commands
        cmds = parse_sequential_commands("帮我看看项目结构，然后列出所有文件")
        self.assertEqual(len(cmds), 2)
        self.assertIn("项目结构", cmds[0])
        self.assertIn("列出所有文件", cmds[1])

    def test_is_sequential_command(self):
        from core.harness_middleware import is_sequential_command
        self.assertTrue(is_sequential_command("先做 A，再做 B"))
        self.assertFalse(is_sequential_command("做一件事"))


if __name__ == "__main__":
    unittest.main()
