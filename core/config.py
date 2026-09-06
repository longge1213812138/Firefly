"""配置加载：全部配置来自本地 config.json，路径基于项目根目录解析。"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config.json"


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | os.PathLike | None = None) -> dict:
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # 相对路径一律解析为「项目根目录下」
    mem = cfg.setdefault("memory", {})
    mem["db_path"] = str((ROOT / mem.get("db_path", "data/memory.db")).resolve())
    safety = cfg.setdefault("safety", {})
    safety["audit_log"] = str((ROOT / safety.get("audit_log", "data/audit.log")).resolve())

    for folder in (Path(mem["db_path"]).parent, Path(safety["audit_log"]).parent):
        folder.mkdir(parents=True, exist_ok=True)

    persona_rel = cfg.get("persona_path", "persona/default.md")
    cfg["persona_path"] = str((ROOT / persona_rel).resolve())
    return cfg


def load_persona(cfg: dict) -> str:
    p = Path(cfg.get("persona_path", ""))
    if not p.exists():
        return "你是温柔贴心的中文语音助手 Fairy（流萤），说话简短自然，像朋友聊天。"
    return p.read_text(encoding="utf-8").strip()
