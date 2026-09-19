"""配置加载：全部配置来自本地 config.json，路径基于项目根目录解析。

「项目根」的判定顺序（见 resolve_root）：
  显式参数 > 环境变量 FIREFLY_ROOT > exe 所在目录（PyInstaller 打包后） > 本包的上级目录
打包成 exe 后，数据/日志/记忆库一律写在 **exe 同级目录**，而不是临时解包目录。
"""
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config.json"

# 项目根可被环境变量 FIREFLY_ROOT 覆盖（便于把核心装到别处、或被其它程序按需调用）。
# 不设该变量时行为与以前完全一致：一律解析到本文件的上上级目录。
ROOT_ENV = "FIREFLY_ROOT"


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包出来的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


def resolve_root(root: str | os.PathLike | None = None) -> Path:
    """决定「项目根」：显式参数 > FIREFLY_ROOT > exe 所在目录（打包后） > 本包所在目录。"""
    if root:
        return Path(root).expanduser().resolve()
    env = os.environ.get(ROOT_ENV)
    if env:
        return Path(env).expanduser().resolve()
    if is_frozen():
        # 打包后 __file__ 指向临时解包目录，必须改用 exe 自己的位置
        return Path(sys.executable).resolve().parent
    return ROOT


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def config_path(root: str | os.PathLike | None = None,
                path: str | os.PathLike | None = None) -> Path:
    """解析「配置文件在哪」——只此一处，别在别处重算（load_config 与热重载共用）。"""
    base = resolve_root(root)
    p = Path(path) if path else base / "config.json"
    return p if p.is_absolute() else base / p


def load_config(path: str | os.PathLike | None = None,
                root: str | os.PathLike | None = None) -> dict:
    base = resolve_root(root)
    cfg_path = config_path(root, path)

    # 首次运行（常见于刚拿到 exe 时）：没有 config.json 就按模板生成一份，别直接崩
    if not Path(cfg_path).exists():
        example = base / "config.example.json"
        if example.exists():
            shutil.copyfile(example, cfg_path)
            sys.stderr.write(f"[流萤] 首次运行：已按模板生成 {cfg_path.name}，"
                             "请填入 API Key 后重新启动。\n")
        else:
            raise FileNotFoundError(f"找不到配置文件：{cfg_path}")

    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # 相对路径一律解析为「项目根目录下」
    def _under_root(value, default: str) -> str:
        p = Path(str(value or default)).expanduser()
        return str(p if p.is_absolute() else (base / p).resolve())

    mem = cfg.setdefault("memory", {})
    mem["db_path"] = _under_root(mem.get("db_path"), "data/memory.db")
    safety = cfg.setdefault("safety", {})
    safety["audit_log"] = _under_root(safety.get("audit_log"), "data/audit.log")

    for folder in (Path(mem["db_path"]).parent, Path(safety["audit_log"]).parent):
        folder.mkdir(parents=True, exist_ok=True)

    persona_rel = cfg.get("persona_path", "persona/default.md")
    cfg["persona_path"] = _under_root(persona_rel, "persona/default.md")

    # 外部 agent（Pi）后端配置：只补默认值，不改用户填的内容
    pi = cfg.setdefault("pi", {})
    pi.setdefault("enabled", True)
    pi.setdefault("cli_path", "")      # 留空 → 自动在 PATH 里找 pi
    pi.setdefault("cwd", "")           # 留空 → 项目根
    pi.setdefault("read_only", False)  # True → 只给读类工具（--tools read,grep,find,ls）
    pi.setdefault("timeout", 600)
    pi.setdefault("mode", "text")      # text（默认）| json | rpc
    if str(pi["mode"]).lower() in ("print", ""):
        pi["mode"] = "text"
    pi.setdefault("extra_args", [])
    pi["cwd"] = str(Path(pi["cwd"]).expanduser().resolve()) if pi["cwd"] else str(base)
    return cfg


def load_persona(cfg: dict) -> str:
    p = Path(cfg.get("persona_path", ""))
    if not p.exists():
        return "你是温柔贴心的中文语音助手 Firefly（流萤），说话简短自然，像朋友聊天。"
    return p.read_text(encoding="utf-8").strip()
