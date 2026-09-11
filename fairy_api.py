"""流萤 · 对外接口（入站扩展接口）。

用途：给将来的 Pi 扩展、或其它外部宿主，通过子进程调用陪伴端的能力。
    python fairy_api.py <子命令> [参数]

协议约定（务必遵守）：
  · stdout 只输出**一行**紧凑 JSON；日志/异常一律走 stderr
  · 退出码：0 成功｜1 业务错误｜2 参数错误
  · **长期记忆只在陪伴端**，本接口只暴露「操作」，不暴露数据库本身

子命令：
  ping                                                       健康检查
  context --query 文本 [--top-k 5] [--session id]            取"人设+可执行操作+相关往事"整块（供注入）
  remember --role user|assistant --text 文本 [--session id] [--category 对话] [--importance 5]
  say --text 文本 [--no-play]                                用流萤的声音念出来
  search --query 文本 [--limit 5]                            检索历史记忆
  persona                                                    查看当前人设
  ask --text 文本 [--session id]                             走完整一轮陪伴对话（人设+记忆+大脑）

注意：`ask` **不会**执行回复里的 ACTION（不产生副作用），只把动作原样返回，由调用方决定怎么做。
"""
from __future__ import annotations

import argparse
import json
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

VERSION = "0.4.0"


def _emit(obj: dict, code: int = 0) -> int:
    """stdout 输出单行 JSON 并返回退出码。"""
    print(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    return code


def _fail(msg: str, code: int = 1, **extra) -> int:
    return _emit({"ok": False, "error": msg, **extra}, code)


# --------------------------------------------------------------------- 子命令
def cmd_ping(cfg: dict, args) -> int:
    from core import agent_backend
    from core.config import load_persona
    from core.memory import Memory

    persona = load_persona(cfg)
    try:
        mem = Memory(cfg["memory"]["db_path"])
        count = mem.count()
        mem.close()
        mem_ok = True
    except Exception as exc:  # noqa: BLE001
        count, mem_ok = 0, False
        sys.stderr.write(f"memory unavailable: {exc}\n")

    llm = cfg.get("llm", {}) or {}
    mimo_key = bool((cfg.get("mimo", {}) or {}).get("api_key"))
    # 小米系会自动复用 mimo 的 Key，所以"地址是小米"还得配上 Key 才算真的可用
    llm_ok = bool(llm.get("api_key")) or (
        "xiaomimimo" in str(llm.get("base_url", "")) and mimo_key)
    tts_model = (cfg.get("tts", {}) or {}).get("model") or (cfg.get("mimo", {}) or {}).get("tts_model", "")
    agents = {name: b.available() for name, b in agent_backend.list_backends(cfg).items()}
    return _emit({
        "ok": True,
        "version": VERSION,
        "persona": bool(persona),
        "persona_path": cfg.get("persona_path", ""),
        "memory": mem_ok,
        "memory_count": count,
        "llm_configured": llm_ok,
        "tts": tts_model,
        "agents": agents,
    })


def _system_block(cfg: dict, query: str, top_k: int) -> tuple[str, str, int]:
    """组装注入用的整块文本，逻辑与 Fairy._system_prompt 保持一致，避免人格漂移。

    注意：这里和 main.py 的 `_system_prompt` 是**同一份拼装规则的两处实现**，
    改了其中一处就要同步另一处（区块顺序：人设 → 动作说明 → 往事召回 → 此刻心情）。
    """
    from core import actions
    from core.config import load_persona
    from core.emotion import EmotionModel
    from core.memory import Memory

    persona = load_persona(cfg)
    recall, count, mood = "", 0, ""
    mem_cfg = cfg.get("memory", {}) or {}
    if (query or "").strip():
        mem = Memory(cfg["memory"]["db_path"])
        recall = mem.build_recall_block(
            query, top_k=top_k,
            pin=bool(mem_cfg.get("pin_important", True)),
            pin_min=int(mem_cfg.get("pin_min_importance", 8)),
            pin_limit=int(mem_cfg.get("pin_limit", 5)),
        )
        count = len(mem.search(query, limit=top_k))
        mem.close()

    # 心情段与 Fairy._mood_context 对齐（共用同一个 emotion.inject_to_context 开关）
    emo_cfg = cfg.get("emotion", {}) or {}
    if bool(emo_cfg.get("inject_to_context", True)):
        try:
            emo = EmotionModel(cfg, db_path=cfg["memory"]["db_path"])
            if emo.enabled and emo.inject_to_context:
                mood = emo.context_line()
            emo.close()
        except Exception:  # noqa: BLE001
            mood = ""

    parts = [persona, "", actions.DESCRIPTIONS]
    if recall:
        parts += ["", "【你记得的与当前话题相关的往事】", recall,
                  "（自然地运用这些记忆，不要生硬地复述，也不要说你查了数据库）"]
    if mood:
        parts += ["", mood]
    return "\n".join(parts), recall, count


def cmd_context(cfg: dict, args) -> int:
    top_k = int(args.top_k or cfg.get("memory", {}).get("recall_top_k", 5))
    block, recall, count = _system_block(cfg, args.query or "", top_k)
    return _emit({
        "ok": True,
        "system_block": block,
        "recall": recall,
        "recall_count": count,
        "session": args.session or "",
    })


def cmd_remember(cfg: dict, args) -> int:
    from core.memory import Memory

    text = (args.text or "").strip()
    if not text:
        return _fail("缺少 --text", 2)
    if args.role not in ("user", "assistant", "system"):
        return _fail("--role 只能是 user / assistant / system", 2)
    session = args.session or ("api-" + time.strftime("%Y%m%d"))
    mem = Memory(cfg["memory"]["db_path"])
    mid = mem.add(session, args.role, text, category=args.category,
                  importance=int(args.importance))
    mem.close()
    return _emit({"ok": True, "id": mid, "session": session})


def cmd_say(cfg: dict, args) -> int:
    text = (args.text or "").strip()
    if not text:
        return _fail("缺少 --text", 2)
    from core import audio_io
    from core.tts import make_tts

    tts_cfg = cfg.get("tts", {}) or {}
    t0 = time.time()
    wav = make_tts(cfg).synth(text, tts_cfg.get("voice_instruction", ""),
                              tts_cfg.get("reference_audio_path", ""))
    played = False
    if not args.no_play:
        audio = cfg.get("audio", {}) or {}
        audio_io.play_wav_bytes(
            wav,
            device=audio.get("output_device"),
            tail_silence=float(audio.get("output_tail_silence", 0.8)),
        )
        played = True
    return _emit({"ok": True, "played": played, "chars": len(text),
                  "audio_bytes": len(wav), "elapsed": round(time.time() - t0, 2)})


def cmd_search(cfg: dict, args) -> int:
    from core.memory import Memory

    mem = Memory(cfg["memory"]["db_path"])
    hits = mem.search(args.query or "", limit=int(args.limit or 10))
    mem.close()
    return _emit({
        "ok": True,
        "count": len(hits),
        "hits": [{"id": h["id"], "role": h["role"], "ts": h["ts"],
                  "content": str(h["content"])[:300]} for h in hits],
    })


def cmd_persona(cfg: dict, args) -> int:
    from core.config import load_persona

    return _emit({"ok": True, "persona": load_persona(cfg),
                  "path": cfg.get("persona_path", "")})


def cmd_ask(cfg: dict, args) -> int:
    """完整一轮陪伴对话。不执行 ACTION，只把动作返回给调用方。"""
    from core import llm as llm_mod
    from core.memory import Memory

    text = (args.text or "").strip()
    if not text:
        return _fail("缺少 --text", 2)
    session = args.session or ("api-" + time.strftime("%Y%m%d"))
    top_k = int(cfg.get("memory", {}).get("recall_top_k", 5))

    mem = Memory(cfg["memory"]["db_path"])
    mem.add(session, "user", text)
    block, _recall, _n = _system_block(cfg, text, top_k)
    brain = llm_mod.make_llm(cfg, system_prompt=block)
    history = mem.recent(session, limit=int(cfg.get("llm", {}).get("max_history_turns", 20)))
    reply = brain.chat([{"role": h["role"], "content": h["content"]} for h in history])
    out_text, actions = llm_mod.extract_actions(reply)
    mem.add(session, "assistant", out_text)
    mem.close()
    return _emit({"ok": True, "text": out_text, "actions": actions, "session": session})


# --------------------------------------------------------------------- 入口
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="fairy_api", description="流萤 · 对外接口（单行 JSON）")
    ap.add_argument("--config", default=None, help="config.json 路径")
    ap.add_argument("--root", default=None, help="项目根目录（覆盖 FAIRY_ROOT）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ping", help="健康检查")

    p = sub.add_parser("context", help="取注入用的整块文本")
    p.add_argument("--query", default="")
    p.add_argument("--top-k", dest="top_k", type=int, default=None)
    p.add_argument("--session", default="")

    p = sub.add_parser("remember", help="写入记忆")
    p.add_argument("--role", required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--session", default="")
    p.add_argument("--category", default="对话")
    p.add_argument("--importance", type=int, default=5)

    p = sub.add_parser("say", help="TTS 播报")
    p.add_argument("--text", required=True)
    p.add_argument("--no-play", dest="no_play", action="store_true")

    p = sub.add_parser("search", help="检索记忆")
    p.add_argument("--query", required=True)
    p.add_argument("--limit", type=int, default=10)

    sub.add_parser("persona", help="查看人设")

    p = sub.add_parser("ask", help="完整一轮对话")
    p.add_argument("--text", required=True)
    p.add_argument("--session", default="")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        from core.config import load_config

        cfg = load_config(args.config, args.root)
    except Exception as exc:  # noqa: BLE001
        return _fail(f"加载配置失败：{exc}")

    handler = {
        "ping": cmd_ping,
        "context": cmd_context,
        "remember": cmd_remember,
        "say": cmd_say,
        "search": cmd_search,
        "persona": cmd_persona,
        "ask": cmd_ask,
    }.get(args.cmd)
    if handler is None:
        return _fail(f"未知子命令：{args.cmd}", 2)
    try:
        return handler(cfg, args)
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc(file=sys.stderr)
        return _fail(f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
