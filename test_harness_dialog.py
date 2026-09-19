#!/usr/bin/env python3
"""测试 Harness 对话流程：验证通过 /pi 命令提交任务。"""
import json
import time
from pathlib import Path

# 加载配置
cfg = json.load(open("config.json", encoding="utf-8"))

# 创建 Firefly 实例
from main import Firefly
ff = Firefly(cfg, speak=False, verbose=True, echo=True)

print("=== Harness 对话流程测试 ===")

# 测试1：通过 /pi 命令提交任务
print("\n1. 测试通过 /pi 命令提交任务...")
# 注意：run_pi_task 会弹出确认框，我们需要模拟确认
ff.confirm_fn = lambda p: True  # 自动确认

reply = ff.run_pi_task("用一句话介绍自己")
print(f"   回复: {reply[:200]}")

# 测试2：通过对话流程提交任务（模拟 LLM 返回 pi_agent 动作）
print("\n2. 测试通过对话流程提交任务...")
# 创建一个假的 LLM，返回 pi_agent 动作
class FakeLLM:
    def __init__(self):
        self.system_prompt = ""
    
    def chat(self, messages):
        return '我来帮你查看时间。\nACTION:{"name":"get_time","args":{}}'

ff.llm = FakeLLM()
reply = ff.respond("现在几点了？", auto_confirm=True)
print(f"   对话回复: {reply[:200]}")

# 测试3：检查任务列表
print("\n3. 检查任务列表...")
tasks = ff.list_tasks()
print(f"   任务数量: {len(tasks)}")
for t in tasks:
    print(f"   - {t['id']} {t['name']}: {t['state']}")

# 清理
ff.memory.close()
ff.close_emotion()
print("\n✅ 测试完成")