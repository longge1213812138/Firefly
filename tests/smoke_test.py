"""离线自检：不联网、不需要麦克风，验证核心模块是否可用。

运行：python main.py --selftest
"""
from __future__ import annotations

import io
import sys
import tempfile
import wave
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def run_selftest(cfg: dict) -> int:
    """自检一律在临时目录中进行，绝不污染真实记忆库与审计日志。"""
    import copy
    import tempfile as _tf

    _tmp = _tf.TemporaryDirectory()
    tmp_dir = Path(_tmp.name)
    cfg = copy.deepcopy(cfg)
    cfg["memory"]["db_path"] = str(tmp_dir / "selftest_memory.db")
    cfg["safety"]["audit_log"] = str(tmp_dir / "selftest_audit.log")

    results: list[tuple[str, bool, str]] = []

    def check(name: str, fn):
        try:
            ok, detail = fn()
            results.append((name, bool(ok), str(detail)))
        except Exception as exc:  # noqa: BLE001
            results.append((name, False, f"异常：{exc}"))

    # 1. 配置
    def t_config():
        from core.config import load_persona

        need = ["mimo", "llm", "wake", "audio", "memory", "safety"]
        missing = [k for k in need if k not in cfg]
        persona = load_persona(cfg)
        return not missing and len(persona) > 20, f"缺失字段={missing}｜人设长度={len(persona)}"

    # 2. 记忆：写入 + 中文检索 + 近期上下文
    def t_memory():
        from core.memory import Memory

        with tempfile.TemporaryDirectory() as td:
            mem = Memory(str(Path(td) / "t.db"))
            mem.add("s1", "user", "我昨天在城南吃了那家云南米线")
            mem.add("s1", "assistant", "好吃吗？我记得你说过你喜欢酸汤口味")
            mem.add("s1", "user", "周末想去爬山，查一下天气")
            hits = mem.search("云南米线", limit=5)
            short_hits = mem.search("爬山", limit=5)
            recent = mem.recent("s1", limit=3)
            info = f"分词器={mem.tokenizer}｜命中={len(hits)}｜短词命中={len(short_hits)}"
            ok = len(hits) >= 1 and len(short_hits) >= 1 and len(recent) == 3 and mem.count() == 3
            mem.close()
            return ok, info

    # 3. 记忆持久化（重启后仍在）
    def t_persist():
        from core.memory import Memory

        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "p.db")
            m1 = Memory(db)
            m1.add("s1", "user", "我的猫叫豆豆")
            m1.close()
            m2 = Memory(db)
            hits = m2.search("豆豆", limit=3)
            n = m2.count()
            m2.close()
            return n == 1 and len(hits) == 1, f"重启后条数={n} 命中={len(hits)}"

    # 4. 安全闸口
    def t_safety():
        from core import safety

        d1 = safety.needs_confirm(cfg, "run_command", {"cmd": "rm -rf test"})
        d2 = safety.needs_confirm(cfg, "write_file", {"path": "a.txt", "content": "覆盖内容"})
        d3 = safety.needs_confirm(cfg, "list_dir", {"path": "."})
        d4 = safety.needs_confirm(cfg, "open_url", {"url": "https://example.com"})
        return d1 and d2 and (not d3) and (not d4), f"命令={d1} 写入={d2} 列目录={d3} 开网页={d4}"

    # 5. 审计日志
    def t_audit():
        from core import safety

        log = cfg["safety"]["audit_log"]
        before = Path(log).stat().st_size if Path(log).exists() else 0
        safety.audit(cfg, {"action": "selftest", "result": "ok", "risk": "low"})
        after = Path(log).stat().st_size
        return after > before, f"日志 {before}→{after} 字节"

    # 6. 只读操作
    def t_actions():
        from core import actions

        ok, out = actions._run("get_time", {})
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "a.txt").write_text("hello", encoding="utf-8")
            ok2, out2 = actions._run("list_dir", {"path": td})
            ok3, out3 = actions._run("read_file", {"path": str(Path(td) / "a.txt")})
        return ok and ok2 and ok3 and "a.txt" in out2 and out3 == "hello", f"时间={out[:19]} 目录={out2[:20]}"

    # 7. 动作指令解析
    def t_parse():
        from core.llm import extract_action

        text, act = extract_action("好的，我来查时间。\nACTION:{\"name\":\"get_time\",\"args\":{}}")
        text2, act2 = extract_action("只是随便聊聊")
        return (act is not None and act["name"] == "get_time" and "查时间" in text
                and act2 is None and text2 == "只是随便聊聊"), f"解析={act} 纯文本={act2}"

    # 8. 音频：wav 编解码（不需要设备）
    def t_audio():
        import numpy as np

        from core import audio_io

        data = (np.sin(np.linspace(0, 6.28, 16000)) * 10000).astype(np.int16)
        wav = audio_io.to_wav_bytes(data, 16000)
        with wave.open(io.BytesIO(wav), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
        return frames == 16000 and rate == 16000 and len(wav) > 30000, f"帧数={frames} 采样率={rate}"

    # 9. 全链路（假的 ASR/LLM/TTS，不联网不录音）
    def t_pipeline():
        import main as main_mod

        f = main_mod.Fairy(cfg, speak=False, verbose=False)

        class FakeLLM:
            def __init__(self):
                self.system_prompt = ""

            def chat(self, messages):
                last = messages[-1]["content"] if messages else ""
                self.system_prompt_used = self.system_prompt
                if "几点" in last or "时间" in last:
                    return "我来看看。\nACTION:{\"name\":\"get_time\",\"args\":{}}"
                return "我记住啦，先记在本子上。"

        f.memory.add(f.session_id, "user", "我的猫叫豆豆，很怕打雷")
        f.llm = FakeLLM()

        # ① 触发一次带操作的对话（get_time 属低风险，auto_confirm 直接放行）
        reply1 = f.respond("现在几点了？", auto_confirm=True)
        # ② 换个话题：此时不应把「豆豆」的记忆塞进来（只注入相关记忆才对）
        f.respond("今天天气怎么样？", auto_confirm=True)
        recall_unrelated = "豆豆" in (f.llm.system_prompt_used or "")
        # ③ 提到豆豆：应当自动召回那段往事
        reply3 = f.respond("豆豆怕什么来着？", auto_confirm=True)
        recall_used = "豆豆" in (f.llm.system_prompt_used or "")
        n = f.memory.count()
        hits = f.memory.search("豆豆", limit=3)
        f.memory.close()
        ok = (len(reply1) > 0 and len(reply3) > 0 and recall_used
              and not recall_unrelated and n >= 7 and len(hits) >= 1)
        return ok, f"相关召回={recall_used} 无关话题误召回={recall_unrelated} 总条数={n} 回复1={reply1[:20]}"

    # 10. 唤醒兜底（键盘模式可实例化）
    def t_wake():
        from core.wake import WakeListener

        w = WakeListener(cfg["wake"], sample_rate=16000)
        mode = w.start()
        w.close()
        return mode in ("porcupine", "keyboard"), f"当前唤醒方式={mode}"

    for name, fn in [
        ("配置与人设加载", t_config),
        ("记忆写入与中文检索", t_memory),
        ("记忆持久化（重启可读）", t_persist),
        ("安全闸口判定", t_safety),
        ("审计日志写入", t_audit),
        ("只读操作执行", t_actions),
        ("动作指令解析", t_parse),
        ("音频编解码", t_audio),
        ("全链路对话（离线桩）", t_pipeline),
        ("唤醒模块", t_wake),
    ]:
        check(name, fn)

    print("\n=== Fairy MVP 自检结果 ===")
    failed = 0
    for name, ok, detail in results:
        flag = "✅ 通过" if ok else "❌ 失败"
        if not ok:
            failed += 1
        print(f"{flag}  {name}  ｜ {detail}")
    print(f"\n共 {len(results)} 项，通过 {len(results)-failed} 项，失败 {failed} 项。")
    print("（自检数据写在临时目录，不会污染真实记忆库）")
    _tmp.cleanup()
    return 1 if failed else 0
