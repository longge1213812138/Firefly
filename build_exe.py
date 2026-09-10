"""把流萤打包成可运行的 exe（PyInstaller）。

用法：
    .venv\\Scripts\\python.exe build_exe.py            # 打包全部三个
    .venv\\Scripts\\python.exe build_exe.py 控制台      # 只打包名字里含「控制台」的那个

产物在 dist\\ 下：
    流萤助手.exe    语音 / 键盘对话主程序（保留控制台窗口，方便看输出）
    流萤控制台.exe  图形控制台（无控制台窗口）
    流萤接口.exe    给外部程序调用的单行 JSON 接口
另外会把 persona\\ 与 config.example.json 复制到 dist\\；
exe 首次运行若没有 config.json，会自动按模板生成一份。

为什么不是简单的 `pyinstaller main.py`：
  · core/actions.py、gui.py 里的"项目根"必须指向 exe 同级目录，否则数据会写进临时解包目录；
    这一点由 core/config.py 的 resolve_root() 处理（识别 sys.frozen）。
  · pystray 的后端是运行时动态导入的，必须显式声明 hidden-import。
  · sounddevice 自带 portaudio 动态库，需要 --collect-all 一起打包。
  · tests.smoke_test 是 --selftest 用到的，也要一并带上。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
ASSETS = ROOT / "assets"
SEP = ";" if sys.platform == "win32" else ":"

TARGETS = [
    ("流萤助手", "main.py", False),
    ("流萤控制台", "gui.py", True),
    ("流萤接口", "fairy_api.py", False),
]

USAGE = """流萤 Fairy · 使用说明
================================

第一次使用：
  1. 双击「流萤控制台.exe」
  2. 打开「配置」页，把小米 MiMo 的 API Key（tp- 开头）填进去，点「保存配置」
  3. 回到「对话」页就能聊天了（记得勾上「回复后语音播报」）

三个程序分别是什么：
  流萤助手.exe    语音 / 键盘对话主程序。双击后按【空格】说话，按 Q 退出。
                 也支持命令行参数，例如：流萤助手.exe --text（键盘模式）
  流萤控制台.exe  图形控制台：对话 / 记忆 / 情感 / 配置 / 统计 / 状态
  流萤接口.exe    给别的程序调用的接口（输出单行 JSON）。例如：
                   流萤接口.exe ping
                   流萤接口.exe context --query 关键词

让 Pi 帮忙（可选）：
  在对话里输入「/pi 任务」，例如「/pi 帮我看看这个项目的结构」。
  需要本机已安装 pi 命令行；没装或没配好时，只有 /pi 不可用，其余功能一切照常。
  想让 Pi 只读不写：配置页勾「只读模式」。

数据都在这个文件夹里（可随时备份 / 删除）：
  config.json          全部配置（含 API Key，请不要外发）
  data/memory.db       全部对话记忆与情绪状态
  data/audit.log       操作审计日志
  persona/default.md   人设（直接改文字即可，保存即生效）

注意事项：
  · 首次运行若提示找不到 config.json，会自动按 config.example.json 生成一份
  · 语音唤醒「Hi Fairy」需要额外装 pvporcupine；没装就用空格键说话
  · 想换电脑使用：把整个 dist 文件夹拷过去即可（不含任何密钥以外的机器绑定信息）
"""


def make_icon(path: Path) -> bool:
    """用项目自带的 pillow 画一只小萤火虫当图标，避免额外引入二进制文件。"""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    k = size / 64.0
    d = ImageDraw.Draw(img)
    d.ellipse([20 * k, 18 * k, 44 * k, 46 * k], fill=(255, 215, 0, 255))
    d.ellipse([24 * k, 22 * k, 40 * k, 42 * k], fill=(255, 255, 150, 200))
    d.ellipse([28 * k, 26 * k, 32 * k, 30 * k], fill=(60, 40, 20, 255))
    d.ellipse([33 * k, 26 * k, 37 * k, 30 * k], fill=(60, 40, 20, 255))
    d.ellipse([10 * k, 12 * k, 28 * k, 30 * k], fill=(200, 230, 255, 120))
    d.ellipse([36 * k, 12 * k, 54 * k, 30 * k], fill=(200, 230, 255, 120))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48),
                                        (64, 64), (128, 128), (256, 256)])
    return True


def build(only: str | None = None) -> int:
    try:
        import PyInstaller.__main__ as pyi
    except ImportError:
        print("缺少 PyInstaller，请先执行：")
        print(r"  .venv\Scripts\python.exe -m pip install pyinstaller")
        return 1

    icon = ASSETS / "fairy.ico"
    has_icon = make_icon(icon)
    print("图标：" + ("已生成 assets/fairy.ico" if has_icon else "生成失败，跳过"))

    common = [
        # 不用 --clean：它会整目录删除 build/<name>/localpycs，既慢又容易触发删除保护。
        # 改动源码后 PyInstaller 本来就会按时间戳重新分析，够用。
        "--noconfirm", "--onefile",
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        "--specpath", str(BUILD),
        "--paths", str(ROOT),
        "--hidden-import", "tests.smoke_test",
        "--hidden-import", "pystray._win32",
        "--collect-all", "sounddevice",
        "--add-data", f"{ROOT / 'persona'}{SEP}persona",
        "--add-data", f"{ROOT / 'config.example.json'}{SEP}.",
    ]
    if has_icon:
        common += ["--icon", str(icon)]

    failed: list[str] = []
    for name, script, windowed in TARGETS:
        if only and only not in name:
            continue
        print(f"\n=== 正在打包 {name}（{script}）===")
        args = [str(ROOT / script), "--name", name, *common]
        if windowed:
            args.append("--noconsole")
        try:
            pyi.run(args)
            print(f"✅ {name}.exe 打包完成")
        except SystemExit as exc:  # PyInstaller 用 SystemExit 表示失败
            if exc.code:
                failed.append(name)
                print(f"❌ {name} 打包失败（退出码 {exc.code}）")

    # 把「用户可以自己改」的文件放到 exe 旁边
    DIST.mkdir(parents=True, exist_ok=True)
    for item in ("persona", "config.example.json"):
        src = ROOT / item
        if not src.exists():
            continue
        dst = DIST / item
        if src.is_dir():
            # 覆盖式复制，避免整目录删除（既不触发删除保护，也不会误删用户文件）
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copyfile(src, dst)
    (DIST / "使用说明.txt").write_text(USAGE, encoding="utf-8")

    print("\n=== 打包结果 ===")
    for name, _, _ in TARGETS:
        exe = DIST / f"{name}.exe"
        if exe.exists():
            print(f"  ✅ {exe}   {exe.stat().st_size / 1024 / 1024:.1f} MB")
    if failed:
        print(f"  ❌ 失败：{failed}")
        return 1
    print(f"\n全部完成，产物在：{DIST}")
    return 0


if __name__ == "__main__":
    sys.exit(build(sys.argv[1] if len(sys.argv) > 1 else None))
