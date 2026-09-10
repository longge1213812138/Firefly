"""HTTP 连接复用：全局 requests.Session 单例 + 连接池。

ASR / TTS / LLM 三处云端调用都走这里，复用 TLS 握手与 keep-alive 连接，
避免每轮对话重建三次连接（每次约省 100~300ms）。

线程安全：Session 只在创建时 mount 适配器，之后仅做 post()，可被多线程并发使用
（后台情绪推断线程也会调用）。用双检锁保证只创建一次。
"""
from __future__ import annotations

import threading

import requests
from requests.adapters import HTTPAdapter

_SESSION: requests.Session | None = None
_LOCK = threading.Lock()


def session() -> requests.Session:
    """返回全局共享的 requests.Session（懒加载 + 双检锁）。"""
    global _SESSION
    if _SESSION is None:
        with _LOCK:
            if _SESSION is None:
                s = requests.Session()
                adapter = HTTPAdapter(pool_connections=4, pool_maxsize=12, max_retries=0)
                s.mount("https://", adapter)
                s.mount("http://", adapter)
                _SESSION = s
    return _SESSION


def shutdown() -> None:
    """关闭共享连接池（进程退出前调用，避免偶发挂起）。"""
    global _SESSION
    with _LOCK:
        if _SESSION is not None:
            try:
                _SESSION.close()
            finally:
                _SESSION = None
