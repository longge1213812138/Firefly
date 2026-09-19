#!/usr/bin/env python3
"""测试 Harness 集成：验证任务提交、执行、查询流程。"""
import json
import time
from pathlib import Path

# 加载配置
cfg = json.load(open("config.json", encoding="utf-8"))

# 创建 Firefly 实例
from main import Firefly
ff = Firefly(cfg, speak=False, verbose=True)

print("=== Harness 集成测试 ===")
print(f"Harness 启用: {ff._harness_queue is not None}")

if ff._harness_queue is None:
    print("❌ Harness 未启用，请检查 config.json 中的 harness.enabled")
    exit(1)

# 测试1：提交任务
print("\n1. 测试提交任务...")
ok, msg, task_id = ff.submit_task("pi_agent", {"task": "用一句话介绍自己", "read_only": True})
print(f"   提交结果: ok={ok}, msg={msg}, task_id={task_id}")

if ok:
    # 测试2：查询任务状态
    print("\n2. 查询任务状态...")
    status = ff.get_task_status(task_id)
    print(f"   状态: {status}")
    
    # 测试3：等待任务完成
    print("\n3. 等待任务完成（最多10秒）...")
    for i in range(10):
        time.sleep(1)
        status = ff.get_task_status(task_id)
        print(f"   {i+1}s: {status}")
        if "已完成" in (status or "") or "失败" in (status or ""):
            break
    
    # 测试4：获取任务结果
    print("\n4. 获取任务结果...")
    exists, result, error = ff.get_task_result(task_id)
    print(f"   存在={exists}, 结果长度={len(result) if result else 0}, 错误={error[:50] if error else ''}")
    
    # 测试5：列出所有任务
    print("\n5. 列出所有任务...")
    tasks = ff.list_tasks()
    print(f"   任务数量: {len(tasks)}")
    for t in tasks:
        print(f"   - {t['id']} {t['name']}: {t['state']}")

# 测试6：任务状态查询
print("\n6. 测试任务状态查询...")
reply = ff._check_task_query("任务做得怎么样了？")
print(f"   查询结果: {reply[:100] if reply else '无'}")

# 清理
ff.memory.close()
ff.close_emotion()
print("\n✅ 测试完成")