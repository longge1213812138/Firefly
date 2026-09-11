"""语音合成：小米 MiMo-V2.5-TTS（chat/completions + audio 参数，返回 base64 音频）。

支持三种模型：
- mimo-v2.5-tts：预置音色
- mimo-v2.5-tts-voicedesign：文本描述定制音色
- mimo-v2.5-tts-voiceclone：音频样本复刻音色
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from . import http as http_mod


class MiMoTTS:
    def __init__(self, mimo_cfg: dict, timeout: int = 60):
        self.api_key = mimo_cfg.get("api_key", "")
        self.base_url = mimo_cfg.get("base_url", "https://api.xiaomimimo.com/v1").rstrip("/")
        self.model = mimo_cfg.get("tts_model", "mimo-v2.5-tts")
        self.voice = mimo_cfg.get("voice", "mimo_default")
        self.fmt = mimo_cfg.get("audio_format", "wav")
        self.timeout = timeout

    def _headers(self) -> dict:
        return {"api-key": self.api_key, "Content-Type": "application/json"}

    def build_messages(self, text: str, voice_instruction: str | None = None) -> list[dict]:
        """按 MiMo 官方规范组织 messages：自然语言指令 → role=user，要念的正文 → role=assistant。

        单独抽出来便于离线测试（不需要真的发请求）。
        """
        messages: list[dict] = []
        if voice_instruction and str(voice_instruction).strip():
            messages.append({"role": "user", "content": str(voice_instruction).strip()})
        messages.append({"role": "assistant", "content": text})
        return messages

    def synth(self, text: str, voice_instruction: str = None, reference_audio_path: str = None) -> bytes:
        """返回音频字节（默认 wav）。

        按 MiMo 官方规范组织 messages：自然语言指令放 role=user，要念的文本放 role=assistant。

        Args:
            text: 要合成的文本
            voice_instruction: 发在 role=user 的自然语言指令。
                - voicedesign 模型：这是「音色设计描述」（必填）
                - 预置音色 / voiceclone 模型：这是「发音风格指令」，用来控制语速、情绪
                  （支持复合情绪，如"温柔但疲惫"），也可用导演模式的「角色/场景/指导」写法
            reference_audio_path: voiceclone 模型的参考音频路径（仅支持 wav/mp3）。
                按官方规范，音频以 data:{MIME};base64,... 形式放进 audio.voice 字段。
        """
        if not self.api_key:
            raise RuntimeError("未配置 MiMo API Key：请先在 config.json 的 mimo.api_key 填入。")

        messages = self.build_messages(text, voice_instruction)

        voice = self.voice
        # voiceclone：参考音频按官方规范以 data URI 形式放进 audio.voice
        if self.model == "mimo-v2.5-tts-voiceclone":
            if not reference_audio_path:
                raise RuntimeError("声音克隆（voiceclone）需要提供参考音频："
                                   "请在控制台「配置 → 声音设置」里选择或录制一段 10~30 秒的清晰人声（wav/mp3）。")
            ok, detail = validate_reference_audio(reference_audio_path)
            if not ok:
                raise RuntimeError(f"参考音频不可用：{detail}")
            audio_bytes = Path(reference_audio_path).read_bytes()
            mime = "audio/wav" if str(reference_audio_path).lower().endswith(".wav") else "audio/mpeg"
            voice = f"data:{mime};base64,{base64.b64encode(audio_bytes).decode('utf-8')}"

        # 官方约定：voicedesign 不需要 voice 参数（音色由 user 消息里的描述生成）
        audio: dict = {"format": self.fmt}
        if self.model != "mimo-v2.5-tts-voicedesign":
            audio["voice"] = voice

        payload = {
            "model": self.model,
            "messages": messages,
            "audio": audio,
        }

        r = http_mod.session().post(
            f"{self.base_url}/chat/completions",
            headers=self._headers(),
            data=json.dumps(payload),
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise RuntimeError(f"TTS 请求失败 {r.status_code}: {r.text[:300]}")
        data = r.json()
        msg = data.get("choices", [{}])[0].get("message", {}) or {}
        audio = msg.get("audio") or {}
        b64 = audio.get("data") or audio.get("audio") or msg.get("audio_data")
        if not b64:
            raise RuntimeError(f"TTS 返回中未找到音频数据: {str(data)[:300]}")
        return base64.b64decode(b64)


def make_tts(cfg: dict) -> "MiMoTTS":
    """按配置创建 TTS（可被独立的 tts 段覆盖 base_url/api_key，否则复用 mimo）。"""
    mimo = cfg.get("mimo", {})
    t = cfg.get("tts", {}) or {}
    merged = dict(mimo)
    for k in ("api_key", "base_url", "voice", "audio_format"):
        if t.get(k):
            merged[k] = t[k]
    if t.get("model"):
        merged["tts_model"] = t["model"]
    return MiMoTTS(merged)


def list_available_voices() -> list[dict]:
    """返回可用音色列表（预置音色 + 自定义音色）。"""
    return [
        {"id": "mimo_default", "name": "MiMo默认（冰糖）", "type": "preset", "language": "zh"},
        {"id": "冰糖", "name": "冰糖", "type": "preset", "language": "zh"},
        {"id": "茉莉", "name": "茉莉", "type": "preset", "language": "zh"},
        {"id": "苏打", "name": "苏打", "type": "preset", "language": "zh"},
        {"id": "白桦", "name": "白桦", "type": "preset", "language": "zh"},
        {"id": "Mia", "name": "Mia", "type": "preset", "language": "en"},
        {"id": "Chloe", "name": "Chloe", "type": "preset", "language": "en"},
        {"id": "Milo", "name": "Milo", "type": "preset", "language": "en"},
        {"id": "Dean", "name": "Dean", "type": "preset", "language": "en"},
        {"id": "custom", "name": "自定义音色（需配置）", "type": "custom", "language": "auto"},
    ]


def get_voice_info(voice_id: str) -> dict | None:
    """获取指定音色的详细信息。"""
    voices = list_available_voices()
    for v in voices:
        if v["id"] == voice_id:
            return v
    return None


# ---------- 声音设置的纯校验 / 映射（离线可用，供控制台与自检测用） ----------

TTS_MODELS = ("mimo-v2.5-tts", "mimo-v2.5-tts-voicedesign", "mimo-v2.5-tts-voiceclone")
REF_AUDIO_EXTS = (".wav", ".mp3")  # 官方仅支持这两种格式
REF_AUDIO_MAX_B64 = 10 * 1024 * 1024  # 官方：Base64 串不超过 10 MB


def voice_choices() -> list[tuple[str, str]]:
    """预置音色下拉选项：[(展示名, 音色ID)]。展示名给真人看，ID 写进配置。"""
    out: list[tuple[str, str]] = []
    for v in list_available_voices():
        if v["type"] != "preset":
            continue
        lang = "中文" if v["language"] == "zh" else "英文"
        out.append((f"{v['name']}（{lang}）", v["id"]))
    return out


def voice_display_for_id(voice_id: str) -> str:
    """把配置里的音色 ID 翻译成下拉框展示名；未知 ID 原样返回（兼容自定义）。"""
    for display, vid in voice_choices():
        if vid == voice_id:
            return display
    return voice_id


def voice_id_for_display(text: str) -> str:
    """把下拉框里的内容还原成音色 ID；本身就是 ID 或自定义值时原样返回。"""
    text = (text or "").strip()
    for display, vid in voice_choices():
        if text == display or text == vid:
            return vid
    return text


def validate_reference_audio(path: str) -> tuple[bool, str]:
    """校验 voiceclone 参考音频（不联网）。返回 (是否可用, 人话说明)。

    按官方要求：仅 wav/mp3、Base64 后不超过 10 MB；wav 额外读出时长，
    偏离建议的 10~30 秒会写进说明（仍判可用，留给用户决定）。
    """
    p = Path(str(path or "").strip())
    if not str(p).strip() or str(p) == ".":
        return False, "还没有选择参考音频文件"
    if not p.exists():
        return False, f"文件不存在：{p}"
    if p.suffix.lower() not in REF_AUDIO_EXTS:
        return False, (f"格式不支持（{p.suffix or '无扩展名'}）：官方仅支持 wav / mp3，"
                       "请先把音频转成这两种格式之一")
    try:
        size = p.stat().st_size
    except OSError as exc:
        return False, f"读不到文件信息：{exc}"
    if size == 0:
        return False, "文件是空的（0 字节）"
    # Base64 体积约为原始 4/3，提前按官方 10MB 上限卡掉
    if size * 4 / 3 > REF_AUDIO_MAX_B64:
        return False, (f"文件太大（{size / 1024 / 1024:.1f} MB，Base64 后约 "
                       f"{size * 4 / 3 / 1024 / 1024:.1f} MB，官方上限 10 MB），请剪短一些")

    if p.suffix.lower() == ".wav":
        try:
            import wave

            with wave.open(str(p), "rb") as wf:
                secs = wf.getnframes() / float(wf.getframerate() or 1)
        except Exception as exc:  # noqa: BLE001
            return False, f"wav 文件读不出来（可能损坏）：{exc}"
        note = f"时长 {secs:.1f} 秒"
        if secs < 10:
            note += "（偏短，官方建议 10~30 秒清晰人声，克隆效果会更好）"
        elif secs > 30:
            note += "（偏长，官方建议 10~30 秒即可）"
        return True, note
    return True, f"大小 {size / 1024:.0f} KB（mp3 无法离线预检时长，建议 10~30 秒清晰人声）"


def tts_settings_issues(settings: dict) -> list[str]:
    """纯校验一组声音设置（不联网），返回问题清单；空列表 = 可以保存。

    settings 键：model / voice / voice_instruction / reference_audio_path
    对齐官方约定：voicedesign 必须有音色描述；voiceclone 必须有可用的参考音频。
    """
    s = settings or {}
    model = str(s.get("model", "") or "").strip()
    issues: list[str] = []
    if model not in TTS_MODELS:
        issues.append(f"TTS 模型不认识：{model or '（空）'}（可选：{' / '.join(TTS_MODELS)}）")
    if model == "mimo-v2.5-tts" and not str(s.get("voice", "") or "").strip():
        issues.append("还没有选音色：请在下拉框里选一个预置音色")
    if model == "mimo-v2.5-tts-voicedesign":
        if not str(s.get("voice_instruction", "") or "").strip():
            issues.append("音色设计（voicedesign）必须填「音色描述」，"
                          "例如：温柔甜美的年轻女性，语速适中")
    if model == "mimo-v2.5-tts-voiceclone":
        ok, detail = validate_reference_audio(str(s.get("reference_audio_path", "") or ""))
        if not ok:
            issues.append(f"声音克隆需要可用的参考音频：{detail}")
    return issues


# 三种合成方式下，各输入项是「可用 / 必填」还是「不适用」。
# 以前三个框一直全开，导致 voiceclone 下还能选音色，让人以为"克隆要先选音色"（审查报告 D3）。
_FIELD_STATES = {
    "mimo-v2.5-tts": {
        "voice_enabled": True,
        "voice_label": "音色",
        "voice_hint": "预置音色方式必填；也可以手动输入音色 ID",
        "instr_enabled": True,
        "instr_label": "风格指令",
        "instr_hint": "可选：想固定一种说话风格就写在这里（实时情绪会自动叠加）",
        "ref_enabled": False,
        "ref_hint": "预置音色方式不需要参考音频",
    },
    "mimo-v2.5-tts-voicedesign": {
        "voice_enabled": False,
        "voice_label": "音色（本方式不用）",
        "voice_hint": "音色设计方式的声音由下方「音色描述」生成，不选音色",
        "instr_enabled": True,
        "instr_label": "音色描述",
        "instr_hint": "必填，例：温柔甜美的年轻女性，语速适中",
        "ref_enabled": False,
        "ref_hint": "音色设计方式不需要参考音频",
    },
    "mimo-v2.5-tts-voiceclone": {
        "voice_enabled": False,
        "voice_label": "音色（本方式不用）",
        "voice_hint": "克隆出来的声音来自下方参考音频，不选音色",
        "instr_enabled": True,
        "instr_label": "风格指令",
        "instr_hint": "可选：想固定说话风格就写在这里（实时情绪会自动叠加）",
        "ref_enabled": True,
        "ref_hint": "必填：10~30 秒清晰人声，仅支持 wav/mp3",
    },
}

# 认不出的模型（例如以后官方加了新名字）一律全开，别把用户的手脚捆住
_DEFAULT_FIELD_STATE = {
    "voice_enabled": True,
    "voice_label": "音色",
    "voice_hint": "也可以手动输入音色 ID",
    "instr_enabled": True,
    "instr_label": "音色描述 / 风格指令",
    "instr_hint": "按所选模型的要求填写",
    "ref_enabled": True,
    "ref_hint": "仅声音克隆需要；10~30 秒清晰人声，wav/mp3",
}


def tts_field_states(model: str) -> dict:
    """当前合成方式下，「音色 / 音色描述 / 参考音频」三个输入项的可用性与文案。

    纯函数（不碰界面、不联网），GUI 与自检共用同一套判断，避免两边说法不一致。
    """
    m = str(model or "").strip()
    return dict(_FIELD_STATES.get(m, _DEFAULT_FIELD_STATE))

