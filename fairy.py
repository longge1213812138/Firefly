"""流萤 · 唯一入口（打包时只指向这一个文件）。

把原先三个入口收编到一个 exe，按命令行参数分发：

  双击（无参数）       → 图形控制台（GUI；隐藏黑窗口）
  流萤.exe --pet       → 只启动桌宠（隐藏黑窗口）
  流萤.exe --text ...  → 语音/键盘对话主程序（保留控制台，可交互）
  流萤.exe --selftest / --diag / --emotion / --stats / --search / --devices /
           --mic-test / --pi-check / --no-speak
                       → 对应体检/状态/工具（保留控制台输出）
  流萤.exe api <子命令> → 对外 JSON 接口（保留 stdout，可被外部程序用管道调用）

开发期也可以直接 `python fairy.py` 跑起来，行为与打包后一致。
"""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    head = (argv[0] if argv else "").lower()

    if head == "api":
        # 接口模式：绝不隐藏控制台，保证 stdout 单行 JSON 能被父进程捕获
        from fairy_api import main as api_main

        return api_main(argv[1:])

    if head == "--pet":
        # 桌宠：纯 tkinter 窗口，隐藏黑窗口
        from core.winconsole import try_hide_own_console

        try_hide_own_console()
        from main import run_app

        return run_app(argv)

    if head.startswith("--"):
        # 助手 / 体检 / 状态类：保留控制台
        from main import run_app

        return run_app(argv)

    # 默认（双击）：图形控制台，隐藏黑窗口
    from core.winconsole import try_hide_own_console

    try_hide_own_console()
    from gui import main as gui_main

    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
