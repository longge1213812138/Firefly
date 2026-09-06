"""语音合成：小米 MiMo-V2.5-TTS（chat/completions + audio 参数，返回 base64 音频）。"""
from __future__ import annotations

import base64
import json

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

    def synth(self, text: str) -> bytes:
        """返回音频字节（默认 wav）。"""
        if not self.api_key:
            raise RuntimeError("未配置 MiMo API Key：请先在 config.json 的 mimo.api_key 填入。")
        payload = {
            "model": self.model,
            # 注意：要合成的文本必须放在 assistant 消息里
            "messages": [{"role": "assistant", "content": text}],
            "audio": {"format": self.fmt, "voice": self.voice},
        }
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
