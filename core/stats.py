"""使用统计：追踪对话次数、最常用功能、运行时长等。"""
from __future__ import annotations

import json
import time
from pathlib import Path


class UsageStats:
    """轻量使用统计，持久化到 JSON 文件。"""

    def __init__(self, path: str = "data/stats.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {
            "total_conversations": 0,
            "total_messages": 0,
            "total_voice_seconds": 0.0,
            "action_counts": {},
            "category_counts": {"对话": 0, "笔记": 0, "待办": 0, "备忘": 0},
            "daily": {},
            "first_used": int(time.time()),
            "total_sessions": 0,
        }

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def record_message(self, role: str, category: str = "对话") -> None:
        """记录一条消息。"""
        self.data["total_messages"] += 1
        if role == "user":
            self.data["total_conversations"] += 1
        self.data["category_counts"][category] = self.data["category_counts"].get(category, 0) + 1
        today = time.strftime("%Y-%m-%d")
        daily = self.data.setdefault("daily", {})
        day_data = daily.setdefault(today, {"messages": 0, "actions": 0})
        day_data["messages"] += 1
        self.save()

    def record_action(self, action_name: str) -> None:
        """记录一次操作。"""
        counts = self.data.setdefault("action_counts", {})
        counts[action_name] = counts.get(action_name, 0) + 1
        today = time.strftime("%Y-%m-%d")
        daily = self.data.setdefault("daily", {})
        day_data = daily.setdefault(today, {"messages": 0, "actions": 0})
        day_data["actions"] += 1
        self.save()

    def record_session(self) -> None:
        """记录一次会话启动。"""
        self.data["total_sessions"] += 1
        self.save()

    def summary(self) -> dict:
        """返回摘要统计。"""
        daily = self.data.get("daily", {})
        today = time.strftime("%Y-%m-%d")
        today_data = daily.get(today, {"messages": 0, "actions": 0})
        top_actions = sorted(
            self.data.get("action_counts", {}).items(),
            key=lambda x: x[1], reverse=True,
        )[:5]
        return {
            "总消息数": self.data["total_messages"],
            "总对话轮次": self.data["total_conversations"],
            "总会话数": self.data["total_sessions"],
            "今日消息": today_data["messages"],
            "今日操作": today_data["actions"],
            "分类分布": self.data["category_counts"],
            "最常用操作": top_actions,
            "首次使用": time.strftime("%Y-%m-%d", time.localtime(self.data.get("first_used", time.time()))),
        }
