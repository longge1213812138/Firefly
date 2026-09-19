#!/usr/bin/env python3
"""测试 Harness 连续多轮任务：验证第二轮任务是否能正常执行。"""
import json
import time
from pathlib import Path

# 加载配置
cfg = json.load(open("config.json", encoding="utf-8"))

# 创建 Firefly 实例
from main import Firefly
ff = Firefly(cfg, speak=False, verbose=True)

print("=== Harness 连续多轮任务测试 ===")

# 模拟用户连续下达两个任务
print("\n1. 提交第一个任务...")
ok1, msg1, task_id1 = ff.submit_task("pi_agent", {"task": "创建一个简单的贪吃蛇游戏", "read_only": False})
print(f"   任务1: ok={ok1}, msg={msg1}, id={task_id1}")

print("\n2. 立即提交第二个任务...")
ok2, msg2, task_id2 = ff.submit_task("pi_agent", {"task": "修改贪吃蛇游戏，添加计分功能", "read_only": False})
print(f"   任务2: ok={ok2}, msg={msg2}, id={task_id2}")

# 等待两个任务都完成
print("\n3. 等待任务完成...")
for i in range(30):  # 最多等待30秒
    time.sleep(1)
    tasks = ff.list_tasks()
    completed = [t for t in tasks if t['state'] == 'completed']
    failed = [t for t in tasks if t['state'] == 'failed']
    running = [t for t in tasks if t['state'] == 'running']
    pending = [t for t in tasks if t['state'] == 'pending']
    
    print(f"   {i+1}s: 完成={len(completed)}, 失败={len(failed)}, 运行中={len(running)}, 排队中={len(pending)}")
    
    if len(completed) + len(failed) >= 2:
        break

# 检查结果
print("\n4. 检查任务结果...")
for task_id in [task_id1, task_id2]:
    exists, result, error = ff.get_task_result(task_id)
    status = ff.get_task_status(task_id)
    print(f"   任务 {task_id}:")
    print(f"     状态: {status}")
    print(f"     结果长度: {len(result) if result else 0}")
    print(f"     错误: {error[:50] if error else '无'}")

# 测试任务状态查询
print("\n5. 测试任务状态查询...")
reply = ff._check_task_query("任务做得怎么样了？")
print(f"   查询结果: {reply[:200] if reply else '无'}")

# 清理
ff.memory.close()
ff.close_emotion()
print("\n✅ 测试完成")