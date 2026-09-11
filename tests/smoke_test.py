"""离线自检：不联网、不需要麦克风，验证核心模块是否可用。

运行：python main.py --selftest
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import wave
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

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
    cfg["stats"] = {"path": str(tmp_dir / "selftest_stats.json")}
    # 自检一律离线：情绪推断关掉大模型，只走关键词兜底
    cfg["emotion"] = {"enabled": True, "infer_with_llm": False}

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
        #    注意查询要用能命中的词（FTS trigram 是子串匹配，整句提问命中率低——
        #    长句召回的短板单独记录在审查报告 D9，不在本次 P0 范围内）
        reply3 = f.respond("豆豆", auto_confirm=True)
        recall_used = "豆豆" in (f.llm.system_prompt_used or "")
        n = f.memory.count()
        hits = f.memory.search("豆豆", limit=3)
        f.memory.close()

        # ④ 同一句话再说一遍：召回块里最多只应出现一条（历史那条），
        #    绝不能把"刚入库的当前这句"也召回进来（自召回回归）
        f2 = main_mod.Fairy(cfg, speak=False, verbose=False)
        f2.llm = FakeLLM()
        f2.respond("我想吃城南那家云南米线", auto_confirm=True)
        f2.respond("我想吃城南那家云南米线", auto_confirm=True)
        prompt2 = f2.llm.system_prompt_used or ""
        n_self = prompt2.count("云南米线")
        f2.memory.close()
        ok = (len(reply1) > 0 and len(reply3) > 0 and recall_used
              and not recall_unrelated and n >= 7 and len(hits) >= 1 and n_self == 1)
        return ok, (f"相关召回={recall_used} 无关话题误召回={recall_unrelated} 总条数={n} "
                    f"自召回行数={n_self}(应为1) 回复1={reply1[:20]}")

    # 10. 唤醒兜底（键盘模式可实例化）
    def t_wake():
        from core.wake import WakeListener

        w = WakeListener(cfg["wake"], sample_rate=16000)
        mode = w.start()
        w.close()
        return mode in ("porcupine", "keyboard"), f"当前唤醒方式={mode}"

    # 11. 硬闸口：删除/覆盖/外发必须当面确认，自动流程绕不过
    def t_safety_hard():
        from core import actions, safety

        h1 = safety.is_hard(cfg, "delete_file", {"path": "x.txt"})
        h2 = safety.is_hard(cfg, "write_file", {"path": "config.json", "content": "覆盖"})
        h3 = safety.is_hard(cfg, "run_command", {"cmd": "del x.txt"})
        h4 = safety.is_hard(cfg, "write_file", {"path": "新文件-自检.txt", "content": "新建"})
        # 硬闸口：即使 auto_confirm=True，确认回调拒绝（或没有）就执行不了
        ok, out = actions.execute(
            cfg, {"name": "delete_file", "args": {"path": "自检-不存在.txt"}},
            auto_confirm=True, confirm_fn=lambda p: False,
        )
        # 保护目录：删除根目录必须被拒绝
        ok2, out2 = actions._run("delete_file", {"path": "C:/"})
        return (h1 and h2 and h3 and (not h4) and (not ok) and (not ok2),
                f"删={h1} 覆盖={h2} 危险命令={h3} 新建={h4} 拦截={str(out)[:12]} 根目录保护={not ok2}")

    # 12. 批量 ACTION 解析（撤销清单的数据来源）
    def t_extract_actions():
        from core.llm import extract_actions

        reply = ("好的，分两步来。\n"
                 "ACTION:{\"name\":\"get_time\",\"args\":{}}\n"
                 "ACTION:{\"name\":\"list_dir\",\"args\":{\"path\":\".\"}}")
        text, acts = extract_actions(reply)
        single_text, single = extract_actions("只是随便聊聊")
        ok = (len(acts) == 2 and acts[0]["name"] == "get_time"
              and "两步" in text and "ACTION" not in text and single == [])
        return ok, f"批量解析={len(acts)} 个｜纯文本无动作列表={single == []}"

    # 13. 桌宠状态机（不弹窗口）
    def t_pet_brain():
        from core.pet import STATES, PetBrain

        b = PetBrain()
        b.set_state("listening")
        keep = b.state == "listening"
        b.set_state("不存在状态")
        keep = keep and b.state == "listening"
        b.tick()
        g = b.glow
        sp = PetBrain("speaking")
        sp.frame = 2  # frame//3=0 → 口型张开帧
        idl = PetBrain("idle")
        idl.frame = 3
        return (keep and 0.0 <= g <= 1.0 and sp.mouth_open and not idl.mouth_open
                and len(STATES) == 4), f"非法状态保持={keep} 呼吸={g:.2f} 口型={sp.mouth_open}"

    # 14. GUI 模块可导入（不弹窗口）
    def t_gui_module():
        import gui

        need = ("ConsoleApp", "set_autostart", "autostart_path", "main", "ChatWorker")
        missing = [n for n in need if not hasattr(gui, n)]
        tk_ok = gui.tk is not None
        return not missing and tk_ok, f"缺失={missing}｜tkinter 可用={tk_ok}"

    # 14b. 桌宠配置解析：默认值 / 非法值钳制 / 退出命令（纯函数，不弹窗口）
    def t_pet_options():
        from core.pet import CMD_QUIT, STATES, resolve_pet_options

        d = resolve_pet_options(None)
        ok_default = (d["enabled"] is True and d["demo"] is False and d["scale"] == 1.0
                      and d["opacity"] == 1.0 and d["start_x"] is None and d["start_y"] is None)
        bad = resolve_pet_options({"pet": {"scale": "很大", "opacity": 99,
                                           "start_x": "abc", "start_y": "120.7"}})
        ok_clamp = (bad["scale"] == 1.0 and bad["opacity"] == 1.0
                    and bad["start_x"] is None and bad["start_y"] == 120)
        hi = resolve_pet_options({"pet": {"scale": 99, "opacity": 0.01}})
        ok_range = hi["scale"] == 2.5 and hi["opacity"] == 0.3
        ok_quit = CMD_QUIT not in STATES
        return (ok_default and ok_clamp and ok_range and ok_quit,
                f"默认={ok_default} 非法钳制={ok_clamp} 边界={ok_range} 退出命令独立={ok_quit}")

    # 15. 外部 agent 后端注册表与参数拼装（离线，不真的调 Pi）
    def t_agent_backend():
        from core import agent_backend

        b = agent_backend.get_backend("pi", cfg)
        ok1 = b is not None and b.name == "pi"
        argv_ro = b.build_argv("改一下 README", read_only=True) if b else []
        ok2 = ("-p" in argv_ro and "--tools" in argv_ro and "read,grep,find,ls" in argv_ro
               and argv_ro[-1] == "改一下 README" and "--" in argv_ro
               and "--print-turn" not in argv_ro)
        argv_rw = b.build_argv("x", read_only=False) if b else []
        ok3 = "--tools" not in argv_rw
        names = list(agent_backend.list_backends(cfg))
        ok4 = "pi" in names
        avail = b.available() if b else None
        ok5 = isinstance(avail, bool)
        return (ok1 and ok2 and ok3 and ok4 and ok5,
                f"注册={names}｜只读带--tools={('--tools' in argv_ro)}｜"
                f"可写不加--tools={('--tools' not in argv_rw)}｜可用={avail}")

    # 16. pi_agent 是硬闸口：每次当面确认，自动流程绕不过
    def t_pi_safety():
        from core import actions, safety

        args = {"task": "帮我重构 src/", "backend": "pi"}
        h = safety.is_hard(cfg, "pi_agent", args)
        c = safety.needs_confirm(cfg, "pi_agent", args)
        ok, out = actions.execute(cfg, {"name": "pi_agent", "args": args},
                                  auto_confirm=True, confirm_fn=lambda p: False)
        blocked = (not ok) and ("取消" in str(out))
        prompt = safety.format_confirm_list([{"name": "pi_agent", "args": args}])
        return (h and c and blocked and "调用 Pi" in prompt,
                f"硬闸口={h} 需确认={c} 拒绝后拦截={blocked}")

    # 17. pi_agent 参数校验：未知后端 / 空任务都要优雅失败
    def t_pi_args():
        from core import actions

        ok1, msg1 = actions._run("pi_agent", {"task": "x", "backend": "不存在的后端"}, cfg)
        ok2, msg2 = actions._run("pi_agent", {"backend": "pi"}, cfg)
        return ((not ok1) and ("未知" in msg1) and (not ok2) and ("task" in msg2),
                f"未知后端→{msg1[:26]}｜空任务→{msg2[:22]}")

    # 18. 删除文件真的能删（回归此前 _protected 引用未导入常量的 bug）
    def t_delete_file():
        from core import actions

        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "待删.txt"
            f.write_text("x", encoding="utf-8")
            ok, out = actions._run("delete_file", {"path": str(f)})
            gone = not f.exists()
            denied, msg = actions._run("delete_file", {"path": "C:/"})
        return (ok and gone and (not denied) and ("保护" in str(msg)),
                f"临时文件已删除={gone}｜受保护目录拒删={not denied}")

    # 19. 对外接口（fairy_api）单行 JSON 协议
    def t_api_protocol():
        import json as _json
        import subprocess as _sp

        # 打包后没有 python 和 fairy_api.py，直接调用自己：`流萤.exe api ...`
        if getattr(sys, "frozen", False):
            base, workdir = [sys.executable, "api"], str(Path(sys.executable).resolve().parent)
        else:
            base, workdir = [sys.executable, "fairy_api.py"], str(_ROOT)

        tmp_cfg = tmp_dir / "selftest_config.json"
        tmp_cfg.write_text(_json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        results = []
        for cmd in (["ping"], ["persona"], ["context", "--query", ""]):
            p = _sp.run([*base, "--config", str(tmp_cfg), *cmd],
                        cwd=workdir, env=env, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=90)
            try:
                obj = _json.loads((p.stdout or "").strip().splitlines()[-1])
                ok = (p.returncode == 0 and obj.get("ok") is True
                      and len((p.stdout or "").strip().splitlines()) == 1)
            except Exception:  # noqa: BLE001
                ok = False
            results.append(ok)
        return all(results), f"ping/persona/context 单行JSON合法={results}"

    # 20. 流式分句器：按标点切句 + 过滤 ACTION 行（离线，纯函数）
    def t_sentence_buffer():
        from core.sentence_buffer import SentenceBuffer

        buf = SentenceBuffer(min_len=4, max_buf=40)
        out: list[str] = []
        # 逐段喂入，模拟流式 delta
        for delta in ["今天天气", "不错，", "适合出去走走。\nACTION:{\"name\":\"get_time\"}", " 好。"]:
            out += buf.feed(delta)
        out += buf.flush()
        joined = "".join(out)
        ok = ("今天天气不错，适合出去走走。" in out or "不错，适合出去走走。" in out) \
            and ("ACTION" not in joined) and buf.dropped_action
        return ok, f"分句={out} 过滤ACTION={buf.dropped_action}"

    # 21. LLM 流式 SSE 解析（离线桩：mock 掉网络，验证 delta 拼接）
    def t_llm_stream():
        import json as _json
        from unittest import mock

        from core import http as http_mod
        from core import llm as llm_mod

        chunks = [
            {"choices": [{"delta": {"content": "你好"}}]},
            {"choices": [{"delta": {"content": "，我是流萤。"}}]},
            {"choices": [{"delta": {"content": ""}}]},
        ]
        lines = ["data: " + _json.dumps(c, ensure_ascii=False) for c in chunks] + ["data: [DONE]"]

        class FakeResp:
            status_code = 200
            text = ""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def iter_lines(self, decode_unicode=True):  # noqa: ARG002
                return iter(lines)

        brain = llm_mod.LLM({"api_key": "test", "base_url": "https://x.test", "model": "m"},
                            system_prompt="")
        with mock.patch.object(http_mod.session(), "post", return_value=FakeResp()):
            out = "".join(brain.chat_stream([{"role": "user", "content": "hi"}]))
        return out == "你好，我是流萤。", f"流式拼接={out!r}"

    # 22. 情绪模型：词典推断 + 演化 + 边界钳制（离线，不调大模型）
    def t_emotion_model():
        import copy as _copy

        from core.emotion import EmotionModel, describe

        ecfg = _copy.deepcopy(cfg)
        ecfg["emotion"] = {"enabled": True, "infer_with_llm": False,
                           "baseline": {"valence": 0.2, "arousal": 0.4, "intimacy": 0.3}}
        with tempfile.TemporaryDirectory() as td:
            emo = EmotionModel(ecfg, db_path=str(Path(td) / "e.db"))
            v0 = emo.state.valence
            emo.update_from_turn("今天好累啊，加班到现在", "辛苦啦，先歇会儿", use_llm=False)
            sad = emo.state.valence < v0 and emo.state.arousal < 0.5
            emo.update_from_turn("谢谢你陪我，今天很开心！", "那就好呀", use_llm=False)
            warm = emo.state.valence > v0 and emo.state.intimacy > 0.3
            rng = (-1.0 <= emo.state.valence <= 1.0 and 0.0 <= emo.state.arousal <= 1.0
                   and 0.0 <= emo.state.intimacy <= 1.0)
            lab, comp = describe(0.5, 0.2)
            lab_ok = lab in ("温柔", "恬静") and bool(comp)
            emo.close()
        return (sad and warm and rng and lab_ok,
                f"低落演化={sad} 回暖+亲密={warm} 范围合法={rng} 标签={lab}/{comp}")

    # 23. 情绪持久化（重启后仍在）
    def t_emotion_persist():
        import copy as _copy

        from core.emotion import EmotionModel

        ecfg = _copy.deepcopy(cfg)
        ecfg["emotion"] = {"enabled": True, "infer_with_llm": False}
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "e.db")
            a = EmotionModel(ecfg, db_path=db)
            a.update_from_turn("我今天超级开心！！！", "太好了", use_llm=False)
            turns, val = a.state.turns, a.state.valence
            a.close()
            b = EmotionModel(ecfg, db_path=db)
            same = b.state.turns == turns and abs(b.state.valence - val) < 0.08
            hist = len(b.history(10))
            b.close()
        return same and hist >= 1, f"重启后轮次={turns} 情绪保持={same} 历史条数={hist}"

    # 24. MiMo 官方风格指令：导演模式三段齐 + brief 更短
    def t_emotion_style():
        import copy as _copy

        from core.emotion import EmotionModel

        with tempfile.TemporaryDirectory() as td:
            d = _copy.deepcopy(cfg)
            d["emotion"] = {"enabled": True, "infer_with_llm": False, "style_mode": "director"}
            e1 = EmotionModel(d, db_path=str(Path(td) / "a.db"))
            e1.update_from_turn("好累好困，今天没力气", "", use_llm=False)
            director = e1.style_instruction(scene="用户说自己很累")
            e1.close()

            b = _copy.deepcopy(cfg)
            b["emotion"] = {"enabled": True, "infer_with_llm": False, "style_mode": "brief"}
            e2 = EmotionModel(b, db_path=str(Path(td) / "b.db"))
            e2.update_from_turn("好累好困，今天没力气", "", use_llm=False)
            brief = e2.style_instruction()
            e2.close()

        three = all(k in director for k in ("【角色】", "【场景】", "【指导】"))
        ok = three and "用户说自己很累" in director and len(brief) < len(director)
        return ok, f"导演三段齐全={three}｜brief 更短={len(brief) < len(director)}｜director {len(director)} 字"

    # 25. TTS messages 符合官方规范（指令在 user、正文在 assistant）
    def t_tts_messages():
        from core.tts import make_tts

        tts = make_tts(cfg)
        with_instr = tts.build_messages("你好呀", "用温柔的语气说，语速慢一点")
        no_instr = tts.build_messages("你好呀", "")
        ok = (with_instr[0]["role"] == "user" and "温柔" in with_instr[0]["content"]
              and with_instr[-1]["role"] == "assistant" and with_instr[-1]["content"] == "你好呀"
              and len(no_instr) == 1 and no_instr[0]["role"] == "assistant")
        return ok, f"有指令={[m['role'] for m in with_instr]}｜无指令={[m['role'] for m in no_instr]}"

    # 25b. 声音设置：音色映射 / 参考音频校验 / 必填项检查（离线，不发请求）
    def t_voice_settings():
        from core import audio_io
        from core.tts import (tts_settings_issues, validate_reference_audio,
                              voice_choices, voice_display_for_id, voice_id_for_display)

        # 音色下拉映射：展示名 ↔ ID 能互相还原
        choices = voice_choices()
        ids = [vid for _, vid in choices]
        ok_map = ("mimo_default" in ids and len(choices) >= 8
                  and voice_id_for_display(choices[0][0]) == choices[0][1]
                  and voice_display_for_id("mimo_default") == choices[0][0]
                  and voice_id_for_display("自定义xyz") == "自定义xyz")

        with tempfile.TemporaryDirectory() as td:
            import numpy as np

            wav_path = Path(td) / "ref.wav"
            tone = (np.sin(np.linspace(0, 6.28 * 12, 16000 * 12)) * 8000).astype(np.int16)
            wav_path.write_bytes(audio_io.to_wav_bytes(tone, 16000))
            ok_wav, msg_wav = validate_reference_audio(str(wav_path))     # 12 秒 wav → 可用
            ok_missing, _ = validate_reference_audio(str(Path(td) / "nope.wav"))
            bad = Path(td) / "ref.flac"
            bad.write_bytes(b"x" * 100)
            ok_flac, msg_flac = validate_reference_audio(str(bad))        # 官方只收 wav/mp3
            empty = Path(td) / "ref.mp3"
            empty.write_bytes(b"")
            ok_empty, _ = validate_reference_audio(str(empty))
            ref_ok = str(wav_path)
            clone_ok = tts_settings_issues({"model": "mimo-v2.5-tts-voiceclone", "voice": "",
                                            "voice_instruction": "", "reference_audio_path": ref_ok})

        clone_missing = tts_settings_issues({"model": "mimo-v2.5-tts-voiceclone", "voice": "",
                                             "voice_instruction": "", "reference_audio_path": ""})
        design_missing = tts_settings_issues({"model": "mimo-v2.5-tts-voicedesign", "voice": "",
                                              "voice_instruction": "", "reference_audio_path": ""})
        preset_ok = tts_settings_issues({"model": "mimo-v2.5-tts", "voice": "mimo_default",
                                         "voice_instruction": "", "reference_audio_path": ""})
        bad_model = tts_settings_issues({"model": "不存在的模型", "voice": "x",
                                         "voice_instruction": "", "reference_audio_path": ""})
        ok_issues = (clone_ok == [] and any("参考音频" in i for i in clone_missing)
                     and any("音色描述" in i for i in design_missing)
                     and preset_ok == [] and any("模型" in i for i in bad_model))
        ok = (ok_map and ok_wav and (not ok_missing) and (not ok_flac)
              and (not ok_empty) and ok_issues)
        return ok, (f"映射={ok_map}｜12s wav={ok_wav}({msg_wav})｜flac拦截={not ok_flac}"
                    f"｜空文件拦截={not ok_empty}｜必填校验={ok_issues}")

    # 25c. 情绪实例注入：控制台情感页与对话共用同一实例，微调立刻影响播报语气
    def t_emotion_injection():
        import main as main_mod
        from core.emotion import EmotionModel

        with tempfile.TemporaryDirectory() as td:
            ecfg = {**cfg, "emotion": {"enabled": True, "infer_with_llm": False}}
            shared = EmotionModel(ecfg, db_path=str(Path(td) / "e.db"))
            f = main_mod.Fairy(ecfg, speak=False, verbose=False, emotion=shared)
            same = f.emotion is shared
            owns = f._owns_emotion is False
            before = f._tts_instruction()
            shared.nudge(0.5, 0.3, 0.2)  # 模拟情感页点「😊 开心一点 / 更亲近」
            after = f._tts_instruction()
            changed = after != before
            f.memory.close()
            f.close_emotion()  # 不应把外部注入的实例关掉
            kept_open = shared._conn is not None
            shared.reset()
            shared.close()
        return (same and owns and changed and kept_open,
                f"与对话同一实例={same} 不接管所有权={owns} 微调后语气变化={changed} 未被误关={kept_open}")

    # 25d. 保存配置的解耦：非法数字只回退该项，不连带挡下整份配置（含音色）
    def t_coerce_numbers():
        import gui

        ok1, v1 = gui.coerce_number("0.008", 1.0)
        ok2, v2 = gui.coerce_number("abc", 0.5)
        ok3, v3 = gui.coerce_number("", 0.25)
        ok4, v4 = gui.coerce_optional_int("", 7)
        ok5, v5 = gui.coerce_optional_int("880", 7)
        ok6, v6 = gui.coerce_optional_int("八百", 7)
        ok = (ok1 and abs(v1 - 0.008) < 1e-9 and (not ok2) and v2 == 0.5
              and (not ok3) and v3 == 0.25 and ok4 and v4 is None
              and ok5 and v5 == 880 and (not ok6) and v6 == 7)
        return ok, f"合法={ok1} 非法回退={v2} 空值回退={v3} 空坐标={v4} 整数={v5} 非法坐标回退={v6}"

    # 26. 记忆管理：筛选查询 / 计数 / 分类 / 翻页 / 删除
    def t_memory_admin():
        from core.memory import Memory

        with tempfile.TemporaryDirectory() as td:
            mem = Memory(str(Path(td) / "a.db"))
            mem.add("s1", "user", "我明天要去北京出差", category="待办", importance=8)
            mem.add("s1", "assistant", "好的，我记下了", category="对话", importance=3)
            mem.add("s1", "user", "北京烤鸭真好吃", category="对话", importance=5)
            cats = mem.categories()
            n_all = mem.count_query()
            n_hi = mem.count_query(min_importance=8)
            n_cat = mem.count_query(category="待办")
            n_kw = mem.count_query(keyword="北京")
            n_cat_hi = len(mem.query(category="对话", min_importance=4))
            page1 = mem.query(offset=0, limit=2)
            page2 = mem.query(offset=2, limit=2)
            mid = page1[0]["id"]
            deleted = mem.delete(mid)
            n_after = mem.count_query()
            miss = mem.delete(999999)
            mem.close()
        ok = (set(cats) == {"待办", "对话"} and n_all == 3 and n_hi == 1 and n_cat == 1
              and n_kw == 2 and n_cat_hi == 1 and len(page1) == 2 and len(page2) == 1
              and deleted and n_after == 2 and (not miss))
        return ok, (f"分类={sorted(cats)} 总数={n_all} 重要度≥8={n_hi} 待办={n_cat} "
                    f"含北京={n_kw} 分页={len(page1)}+{len(page2)} 删后={n_after}")

    # 27. 打包（冻结模式）下项目根指向 exe 同级目录
    def t_frozen_root():
        from core import config as cfgmod

        saved_exe = sys.executable
        had_frozen = hasattr(sys, "frozen")
        saved_frozen = getattr(sys, "frozen", None)
        try:
            sys.frozen = True
            sys.executable = str(tmp_dir / "流萤.exe")
            frozen_root = cfgmod.resolve_root()
            frozen_flag = cfgmod.is_frozen()
        finally:
            if had_frozen:
                sys.frozen = saved_frozen
            else:
                try:
                    del sys.frozen
                except AttributeError:
                    pass
            sys.executable = saved_exe
        # 显式参数优先级最高（打包与非打包环境都成立）
        explicit = cfgmod.resolve_root(tmp_dir)
        want = str(Path(tmp_dir).resolve())
        ok = (str(frozen_root) == want and frozen_flag and str(explicit) == want)
        return ok, (f"冻结时根={Path(frozen_root).name}｜is_frozen={frozen_flag}"
                    f"｜显式参数生效={str(explicit) == want}")

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
        ("硬闸口（删除/覆盖/外发强制确认）", t_safety_hard),
        ("批量 ACTION 解析（撤销清单）", t_extract_actions),
        ("桌宠状态机", t_pet_brain),
        ("桌宠配置解析（缩放/透明度/位置钳制）", t_pet_options),
        ("GUI 模块（tkinter）", t_gui_module),
        ("外部agent后端注册与参数拼装", t_agent_backend),
        ("pi_agent 硬闸口", t_pi_safety),
        ("pi_agent 参数校验", t_pi_args),
        ("删除文件真正可用（回归修复）", t_delete_file),
        ("对外接口单行JSON协议", t_api_protocol),
        ("流式分句器（切句+过滤ACTION）", t_sentence_buffer),
        ("LLM流式SSE解析（离线桩）", t_llm_stream),
        ("情绪模型演化与边界", t_emotion_model),
        ("情绪持久化（重启可读）", t_emotion_persist),
        ("MiMo风格指令（导演模式）", t_emotion_style),
        ("TTS消息结构符合官方规范", t_tts_messages),
        ("声音设置校验（音色/参考音频/必填项）", t_voice_settings),
        ("情绪单实例注入（情感页↔对话联动）", t_emotion_injection),
        ("保存配置字段解耦（非法数字回退）", t_coerce_numbers),
        ("记忆管理（筛选/翻页/删除）", t_memory_admin),
        ("打包后项目根指向exe目录", t_frozen_root),
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
    # Windows 下打包（onefile）时，api 协议子进程的 sqlite 句柄可能延迟释放，
    # 清理临时目录偶发 PermissionError——这不应影响自检结果，忽略即可（系统会兜底清理）。
    try:
        _tmp.cleanup()
    except OSError:
        pass
    return 1 if failed else 0
