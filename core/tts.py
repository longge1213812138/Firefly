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

import requests


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
            reference_audio_path: voiceclone 模型的参考音频路径（可选）
        """
        if not self.api_key:
            raise RuntimeError("未配置 MiMo API Key：请先在 config.json 的 mimo.api_key 填入。")

        messages = self.build_messages(text, voice_instruction)

        payload = {
            "model": self.model,
            "messages": messages,
            "audio": {"format": self.fmt, "voice": self.voice},
        }

        # 如果是voiceclone模型，需要提供参考音频
        if reference_audio_path and self.model == "mimo-v2.5-tts-voiceclone":
            try:
                audio_bytes = Path(reference_audio_path).read_bytes()
                audio_base64 = base64.b64encode(audio_bytes).decode("utf-8")
                payload["reference_audio"] = audio_base64
            except Exception as e:
                raise RuntimeError(f"无法读取参考音频文件 {reference_audio_path}: {e}")

        r = requests.post(
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
