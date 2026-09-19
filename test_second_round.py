#!/usr/bin/env python3
"""复现已知问题：第二轮 Pi 任务是否正常执行。

用户报告：第二轮任务未执行
接续动作：验证 /pi 帮我做个小游戏 → /pi 帮我修改这个游戏 的流程
"""
import json
import time

cfg = json.load(open("config.json", encoding="utf-8"))
from main import Firefly

ff = Firefly(cfg, speak=False, verbose=True, echo=True)
# 自动确认（模拟用户点确认）
ff.confirm_fn = lambda p: True

print("=== 复现已知问题：第二轮任务 ===\n")

# 第一轮
print("【第一轮】/pi 用 Python 写一个简单的 hello.py 文件")
r1 = ff.run_pi_task("用 Python 写一个简单的 hello.py 文件，只打印 Hello World")
print(f"  回复: {r1[:200]}\n")

# 等第一轮完成
print("等待第一轮完成……")
task_id_1 = None
for t in ff.list_tasks():
    if t['state'] == 'running':
        task_id_1 = t['id']
        break

if task_id_1:
    for i in range(60):
        time.sleep(1)
        s = ff.get_task_status(task_id_1)
        if s and ("已完成" in s or "失败" in s):
            print(f"  第一轮 {s}")
            break
else:
    print("  ⚠️ 第一轮任务 ID 未找到（可能是同步模式）")

# 第二轮
print("\n【第二轮】/pi 修改 hello.py 加上当前日期")
r2 = ff.run_pi_task("修改 hello.py 文件，在 Hello World 后面加上当前日期")
print(f"  回复: {r2[:200]}\n")

# 等第二轮完成
task_id_2 = None
all_tasks = ff.list_tasks()
for t in all_tasks:
    if t['id'] != task_id_1 and t['state'] in ('running', 'pending'):
        task_id_2 = t['id']
        break

if task_id_2:
    print(f"等待第二轮完成（ID: {task_id_2}）……")
    for i in range(60):
        time.sleep(1)
        s = ff.get_task_status(task_id_2)
        if s and ("已完成" in s or "失败" in s):
            print(f"  第二轮 {s}")
            break
else:
    print("  ⚠️ 第二轮任务 ID 未找到")

# 最终报告
print("\n=== 最终状态 ===")
all_tasks = ff.list_tasks()
for t in all_tasks:
    print(f"  {t['id']} {t['name']}: {t['state']} (耗时 {t.get('duration', 0):.1f}s)")

# 判断
running_tasks = [t for t in all_tasks if t['state'] == 'running']
pending_tasks = [t for t in all_tasks if t['state'] == 'pending']
completed_tasks = [t for t in all_tasks if t['state'] == 'completed']
failed_tasks = [t for t in all_tasks if t['state'] == 'failed']

ok = len(running_tasks) == 0 and len(pending_tasks) == 0
print(f"\n{'✅ 两轮任务都已完成' if ok else '❌ 存在未完成的任务'}")
print(f"  完成: {len(completed_tasks)}, 失败: {len(failed_tasks)}, 运行中: {len(running_tasks)}, 排队中: {len(pending_tasks)}")

ff.memory.close()
ff.close_emotion()