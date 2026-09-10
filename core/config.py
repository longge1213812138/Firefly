"""配置加载：全部配置来自本地 config.json，路径基于项目根目录解析。"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config.json"

# 项目根可被环境变量 FAIRY_ROOT 覆盖（便于把核心装到别处、或被其它程序按需调用）。
# 不设该变量时行为与以前完全一致：一律解析到本文件的上上级目录。
ROOT_ENV = "FAIRY_ROOT"


def resolve_root(root: str | os.PathLike | None = None) -> Path:
    """决定「项目根」：显式参数 > 环境变量 FAIRY_ROOT > 本包所在目录。"""
    if root:
        return Path(root).expanduser().resolve()
    env = os.environ.get(ROOT_ENV)
    if env:
        return Path(env).expanduser().resolve()
    return ROOT


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | os.PathLike | None = None,
                root: str | os.PathLike | None = None) -> dict:
    base = resolve_root(root)
    cfg_path = Path(path) if path else base / "config.json"
    if not Path(cfg_path).is_absolute():
        cfg_path = base / cfg_path
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
        return "你是温柔贴心的中文语音助手 Fairy（流萤），说话简短自然，像朋友聊天。"
    return p.read_text(encoding="utf-8").strip()
