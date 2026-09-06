"""Fairy（流萤）本地语音助手 · MVP 入口

用法（在「启动助手.bat」里已封装，也可命令行）：
  python main.py              语音模式（默认）
  python main.py --text       键盘输入模式（不用麦克风，方便调试/没配 Key 时试跑）
  python main.py --devices    查看电脑的麦克风/喇叭设备
  python main.py --search 关键词   检索历史对话
  python main.py --selftest   离线自检（不联网）
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import traceback
import uuid

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from core import actions, audio_io, llm as llm_mod, safety  # noqa: E402
from core.asr import make_asr  # noqa: E402
from core.config import load_config, load_persona  # noqa: E402
from core.memory import Memory  # noqa: E402
from core.tts import MiMoTTS, make_tts  # noqa: E402
from core.wake import WakeListener  # noqa: E402


class Fairy:
    def __init__(self, cfg: dict, speak: bool = True, verbose: bool = True):
        self.cfg = cfg
        self.speak = speak
        self.verbose = verbose
        self.persona = load_persona(cfg)
        self.memory = Memory(cfg["memory"]["db_path"])
        self.llm = llm_mod.make_llm(cfg, system_prompt=self.persona)
        self.asr = make_asr(cfg)
        self.tts = make_tts(cfg)
        self.session_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]

    # ---------- 核心对话 ----------
    def _system_prompt(self, recall: str) -> str:
        parts = [self.persona, "", actions.DESCRIPTIONS]
        if recall:
            parts += ["", "【你记得的与当前话题相关的往事】", recall,
                      "（自然地运用这些记忆，不要生硬地复述，也不要说你查了数据库）"]
        return "\n".join(parts)

    def respond(self, user_text: str, auto_confirm: bool = False) -> str:
        self.memory.add(self.session_id, "user", user_text)
        recall = self.memory.build_recall_block(
            user_text, top_k=int(self.cfg["memory"].get("recall_top_k", 5))
        )
        self.llm.system_prompt = self._system_prompt(recall)

        history = self.memory.recent(
            self.session_id, limit=int(self.cfg["llm"].get("max_history_turns", 20))
        )
        messages = [{"role": h["role"], "content": h["content"]} for h in history]

        reply = self.llm.chat(messages)
        text, action = llm_mod.extract_action(reply)

        if action:
            ok, out = actions.execute(self.cfg, action, auto_confirm=auto_confirm,
                                      session_id=self.session_id)
            note = f"（操作 {action.get('name')} {'成功' if ok else '失败'}：{str(out)[:400]}）"
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": f"系统提示：{note} 请用自然语言简短告诉用户结果。"})
            try:
                text = self.llm.chat(messages).strip()
            except Exception:
                text = (text + " " + note).strip()

        self.memory.add(self.session_id, "assistant", text)
        return text

    # ---------- 语音链路 ----------
    def listen(self) -> str:
        a = self.cfg["audio"]
        if self.verbose:
            print("  🎤 聆听中……（说完停一下即可）", flush=True)
        data = audio_io.record_until_silence(
            sample_rate=int(a.get("sample_rate", 16000)),
            silence_threshold=float(a.get("silence_threshold", 0.012)),
            max_seconds=float(a.get("max_record_seconds", 20)),
            min_seconds=float(a.get("min_record_seconds", 0.4)),
            tail_silence_seconds=float(a.get("tail_silence_seconds", 1.0)),
            device=a.get("input_device"),
        )
        if data.size < 1600:  # 小于 0.1 秒视为无效
            print("  ⚠️ 没录到声音：麦克风可能被静音，或选错了设备（跑 python main.py --devices 查看）", flush=True)
            return ""
        rms, peak = audio_io.level_stats(data)
        if peak < 0.02:
            print(
                f"  ⚠️ 声音很小（峰值 {peak:.4f}）。建议在 config.json 把 silence_threshold 调小到 "
                f"{max(0.003, rms * 0.6):.3f}，或把系统麦克风音量调大", flush=True,
            )
        wav = audio_io.to_wav_bytes(data, int(a.get("sample_rate", 16000)))
        text = self.asr.transcribe(wav)
        if not text:
            print("  ⚠️ 没听清（识别结果为空），请再说一次", flush=True)
        return text

    def say(self, text: str) -> None:
        print(f"\n🧚 Fairy：{text}\n", flush=True)
        if not self.speak:
            return
        try:
            wav_bytes = self.tts.synth(text)
            tail = float(self.cfg["audio"].get("output_tail_silence", 0.8))
            audio_io.play_wav_bytes(wav_bytes, device=self.cfg["audio"].get("output_device"), tail_silence=tail)
        except Exception as exc:  # noqa: BLE001
            print(f"  （语音播报失败：{exc}）", flush=True)

    # ---------- 主循环 ----------
    def run_voice(self) -> None:
        waker = WakeListener(self.cfg["wake"], sample_rate=int(self.cfg["audio"]["sample_rate"]),
                             device=self.cfg["audio"].get("input_device"))
        mode = waker.start()
        if mode == "porcupine":
            print(f"\n✅ 语音唤醒已就绪：说「{self.cfg['wake'].get('keyword','Hi Fairy')}」即可叫我", flush=True)
        else:
            print("\n⚠️ 未配置 Picovoice 语音唤醒，已启用【空格键说话】兜底模式", flush=True)
            print("   → 按【空格】后直接说你要说的话（不用喊 Hi Fairy），说完停顿约 1 秒自动结束", flush=True)
            print("   → 按【Q】退出", flush=True)
            print("   → 注意：黑窗口被鼠标点过后会进入「标记模式」吞掉按键，按一下 Esc 可解除", flush=True)
            print("   → 想用真·语音唤醒「Hi Fairy」，请看 README 里的 3 步配置指引", flush=True)

        print(f"   会话 ID：{self.session_id}｜历史记忆：{self.memory.count()} 条\n", flush=True)

        try:
            while True:
                sig = waker.wait()
                if sig == "quit":
                    break
                if sig != "wake":
                    continue
                try:
                    audio_io.play_beep(device=self.cfg["audio"].get("output_device"))
                except Exception:
                    pass
                try:
                    user_text = self.listen()
                except Exception as exc:  # noqa: BLE001
                    print(f"  （录音/识别失败：{exc}）", flush=True)
                    try:
                        user_text = input("  ⌨️ 识别服务不可用，改用键盘输入这句话（直接回车跳过）：").strip()
                    except (EOFError, KeyboardInterrupt):
                        user_text = ""
                if not user_text:
                    continue
                # 云端兜底：即使没有本地唤醒引擎，喊「Hi Fairy」也会立刻回应
                if is_wake_phrase(user_text):
                    print(f"\n🗣 你：{user_text}", flush=True)
                    self.say("我在呢，说吧。")
                    user_text = self.listen()
                    if not user_text or is_wake_phrase(user_text):
                        continue
                print(f"\n🗣 你：{user_text}", flush=True)
                if _is_exit(user_text):
                    self.say("好，我先去休息啦，随时叫我。")
                    break
                try:
                    reply = self.respond(user_text)
                except Exception as exc:  # noqa: BLE001
                    print(f"  （对话失败：{exc}）", flush=True)
                    continue
                self.say(reply)
        except KeyboardInterrupt:
            print("\n已退出。", flush=True)
        finally:
            waker.close()
            self.memory.close()

    def run_text(self) -> None:
        print("\n【键盘模式】直接打字回车即可对话；输入 q 退出。", flush=True)
        print(f"   会话 ID：{self.session_id}｜历史记忆：{self.memory.count()} 条\n", flush=True)
        while True:
            try:
                user_text = input("你：").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not user_text:
                continue
            if user_text.lower() in ("q", "quit", "exit"):
                break
            t0 = time.time()
            try:
                reply = self.respond(user_text)
            except Exception as exc:  # noqa: BLE001
                print(f"（对话失败：{exc}）", flush=True)
                continue
            print(f"\n🧚 Fairy（{time.time()-t0:.1f}s）：{reply}\n", flush=True)
            if self.speak:
                self.say(reply)
        self.memory.close()


def _is_exit(text: str) -> bool:
    return any(k in text for k in ("退出", "再见", "结束吧", "拜拜", "退下吧", "关闭助手"))


def is_wake_phrase(text: str) -> bool:
    """识别结果是否只是在叫名字（Hi Fairy），用于云端兜底唤醒。"""
    if not text:
        return False
    t = re.sub(r"[^\w\u4e00-\u9fff]", "", text.lower())
    if len(t) > 14:
        return False
    return any(k in t for k in ("fairy", "菲儿", "菲瑞", "飞儿", "费尔瑞"))


def diag(cfg: dict) -> None:
    """云端服务体检：分别测 ASR / TTS / LLM，给出人话结论。"""
    import base64
    import json

    import numpy as np
    import requests

    mimo, llm = cfg["mimo"], cfg["llm"]
    wav = audio_io.to_wav_bytes(np.zeros(3200, dtype=np.int16), 16000)

    print("\n=== 云端服务体检 ===")

    # 1. ASR
    print("\n1) 语音识别 ASR", end="：", flush=True)
    try:
        text = make_asr(cfg).transcribe(wav)
        print(f"✅ 正常（测试返回：{text[:30] or '空（静音正常）'}）")
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 失败\n   {exc}")

    # 2. TTS
    print("2) 语音合成 TTS", end="：", flush=True)
    try:
        b = self_tts(cfg).synth("你好，我是流萤。")
        print(f"✅ 正常（收到 {len(b)} 字节音频）")
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 失败\n   {exc}")

    # 3. LLM
    print("3) 大脑 LLM", end="：", flush=True)
    try:
        reply = llm_mod.make_llm(cfg).chat([{"role": "user", "content": "只回复两个字：收到"}])
        print(f"✅ 正常（模型回复：{reply[:30]}）")
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 失败\n   {exc}")

    print("\n（哪一项 ❌ 就处理哪一项，三项全绿就可以正常对话了）")


def self_tts(cfg: dict):
    return make_tts(cfg)


def mic_test(cfg: dict) -> None:
    """麦克风音量体检：录 3 秒，报告电平并给出阈值建议。"""
    a = cfg["audio"]
    print("\n=== 麦克风体检 ===")
    print("请马上对着麦克风，用正常音量说 3 秒钟话……", flush=True)
    data = audio_io.record_seconds(3.0, int(a.get("sample_rate", 16000)), device=a.get("input_device"))
    rms, peak = audio_io.level_stats(data)
    print(f"\n平均音量 RMS = {rms:.4f}   峰值 = {peak:.4f}")
    if peak < 0.01:
        print("❌ 几乎没收到声音：检查麦克风是否被静音 / 是否为系统默认设备 / 权限是否开启。")
    elif peak < 0.05:
        print("⚠️ 声音偏小：建议提高系统麦克风音量（录音设备 → 级别）。")
    else:
        print("✅ 麦克风正常。")
    suggest = round(max(0.003, min(0.05, rms * 0.6)), 3)
    print(f"建议 config.json 的 silence_threshold 设为：{suggest}（当前 {a.get('silence_threshold')}）")


def main() -> int:
    ap = argparse.ArgumentParser(description="Fairy 本地语音助手")
    ap.add_argument("--text", action="store_true", help="键盘输入模式")
    ap.add_argument("--devices", action="store_true", help="列出音频设备")
    ap.add_argument("--search", metavar="关键词", help="检索历史对话")
    ap.add_argument("--selftest", action="store_true", help="离线自检")
    ap.add_argument("--no-speak", action="store_true", help="不播报语音（只显示文字）")
    ap.add_argument("--mic-test", action="store_true", help="麦克风音量体检（录 3 秒并给出阈值建议）")
    ap.add_argument("--diag", action="store_true", help="云端服务体检（分别测 ASR/TTS/LLM）")
    args = ap.parse_args()

    cfg = load_config()

    if args.diag:
        diag(cfg)
        return 0

    if args.mic_test:
        mic_test(cfg)
        return 0

    if args.devices:
        for d in audio_io.list_devices():
            print(f"[{d['index']}] {d['name']}  输入{d['inputs']} / 输出{d['outputs']}")
        return 0

    if args.search:
        mem = Memory(cfg["memory"]["db_path"])
        hits = mem.search(args.search, limit=10)
        print(f"检索「{args.search}」命中 {len(hits)} 条：")
        for h in hits:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(h["ts"]))
            print(f"  [{ts}] {h['role']}：{h['content'][:120]}")
        mem.close()
        return 0

    if args.selftest:
        from tests.smoke_test import run_selftest
        return run_selftest(cfg)

    fairy = Fairy(cfg, speak=not args.no_speak)
    if args.text:
        fairy.run_text()
    else:
        fairy.run_voice()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        print("\n❌ 程序异常退出，以下是错误详情（可截图发我）：", flush=True)
        traceback.print_exc()
        try:
            input("\n按回车键关闭窗口……")
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)
