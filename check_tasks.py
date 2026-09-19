#!/usr/bin/env python3
"""检查现有任务状态"""
import json
import time

cfg = json.load(open("config.json", encoding="utf-8"))
from main import Firefly

ff = Firefly(cfg, speak=False, verbose=False, echo=False)

tasks = ff.list_tasks()
print(f"当前任务数: {len(tasks)}")
for t in tasks:
    print(f"  {t['id']} {t['name']}: {t['state']} (耗时 {t.get('duration', 0):.1f}s)")
    if t.get('output_count'):
        print(f"    输出行数: {t['output_count']}")

ff.memory.close()
ff.close_emotion()