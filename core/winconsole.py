"""Windows 控制台窗口控制（单 exe 化用）。

合并成一个「控制台子系统」的 exe 后，双击会自带一个黑窗口。图形控制台 / 桌宠
这两种模式不需要黑窗口，这里负责把它藏掉；同时要小心**别把用户自己的 cmd 一起藏掉**。

关键 API：
- try_hide_own_console()  只在「这个控制台是我独占的」时才 SW_HIDE；开发环境不动作
- console_visible()        当前控制台是否可见（供错误处理时判断要不要 input 暂停）

判定「独占」的依据：GetConsoleProcessList 返回挂在这个控制台上的进程数。
  · 双击 exe（PyInstaller onefile 会多一个 bootloader 子进程）→ 一般 1~2 个
  · 用户在 cmd 里敲「流萤.exe」→ 多出 cmd.exe 自己，进程数会更多
所以「进程数 ≤ 2 且包含自己」才敢藏。
"""
from __future__ import annotations

import os
import sys

_HIDDEN = False

# 仅在 Windows 上 import ctypes；其它平台这些函数一律 no-op（返回 False）
try:
    import ctypes
    from ctypes import wintypes
    _IS_WIN = sys.platform == "win32"
except Exception:  # pragma: no cover
    ctypes = None  # type: ignore[assignment]
    _IS_WIN = False


def _kernel32():
    if not _IS_WIN or ctypes is None:
        return None
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _user32():
    if not _IS_WIN or ctypes is None:
        return None
    return ctypes.WinDLL("user32", use_last_error=True)


def _console_process_list() -> list[int]:
    """返回挂接在当前控制台上的进程 PID 列表；失败返回空列表。"""
    k = _kernel32()
    if k is None:
        return []
    try:
        k.GetConsoleProcessList.restype = ctypes.c_uint
        k.GetConsoleProcessList.argtypes = [ctypes.POINTER(ctypes.c_uint), ctypes.c_uint]
        n = k.GetConsoleProcessList(None, 0)
        if n <= 0:
            return []
        arr = (ctypes.c_uint * n)()
        got = k.GetConsoleProcessList(arr, n)
        return [int(x) for x in arr[:got]] if got else []
    except Exception:  # noqa: BLE001
        return []


def _console_window() -> int:
    k = _kernel32()
    if k is None:
        return 0
    try:
        return int(k.GetConsoleWindow() or 0)
    except Exception:  # noqa: BLE001
        return 0


def try_hide_own_console() -> bool:
    """图形控制台 / 桌宠模式下调用：藏掉只属于自己的黑窗口。

    返回 True 表示确实隐藏了；开发环境（非 frozen）返回 False、不做任何事。
    """
    global _HIDDEN
    if not _IS_WIN or not getattr(sys, "frozen", False):
        return False
    hwnd = _console_window()
    if not hwnd:
        return False  # 本来就没有控制台（被父进程重定向 stdout 等情况）
    pids = _console_process_list()
    if os.getpid() not in pids:
        return False
    # 进程数 ≤ 2（自己 + 可能的 bootloader）才藏；多了说明挂着用户的 cmd，不能动
    if len(pids) > 2:
        return False
    try:
        u = _user32()
        if u is None:
            return False
        u.ShowWindow.restype = ctypes.c_int
        u.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        _HIDDEN = bool(u.ShowWindow(hwnd, 0) == 0)  # SW_HIDE=0，返回 0 表示"之前可见"
        return _HIDDEN
    except Exception:  # noqa: BLE001
        return False


def console_visible() -> bool:
    """控制台当前是否可见（用于错误处理时决定要不要 input 暂停避免卡死）。"""
    if not _IS_WIN:
        return True
    if _HIDDEN:
        return False
    return bool(_console_window())


def ensure_console_visible() -> bool:
    """需要控制台但可能没有时，尽量保证有一个可交互的控制台。

    合并后的 exe 是「控制台子系统」，双击时 Windows 会自动挂一个控制台，因此本函数
    在绝大多数情况下是 no-op；仅在极端情况（例如被管道启动且 stdin 被关闭）下兜底。
    返回 True 表示现在有可用控制台。
    """
    if not _IS_WIN:
        return True
    k = _kernel32()
    if k is None:
        return True
    hwnd = _console_window()
    if hwnd:
        return True
    # 完全没有控制台窗口：尝试现开一个
    try:
        k.AllocConsole.restype = ctypes.c_int
        if k.AllocConsole():
            k.SetConsoleOutputCP(65001)
            k.SetConsoleCP(65001)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False
