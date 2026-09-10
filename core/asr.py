"""语音识别：可插拔服务商。

- provider = "mimo"        → 小米 MiMo-V2.5-ASR（chat/completions 的 input_audio）
- provider = "whisper_api" → 任意 OpenAI 兼容的 /audio/transcriptions 接口
                             （OpenAI Whisper / Groq / 硅基流动 / 各类国内网关都走这个标准接口）
"""
from __future__ import annotations

import base64
import json

from . import http as http_mod

FRIENDLY_ERRORS = {
    401: "API Key 无效或已过期（401）：检查 config.json 里填的 Key 是否完整、有没有多复制空格。",
    402: "账户余额不足（402）：该服务需要充值或领取免费额度后才能调用。",
    403: "没有该服务的访问权限（403）：这个 Key 可能没开通该模型。",
    404: "接口地址不对（404）：检查 base_url 是否填错。",
    429: "请求太频繁或额度用尽（429）：稍后再试或检查套餐余量。",
}


def _friendly(status: int, raw: str) -> str:
    hint = FRIENDLY_ERRORS.get(status, f"HTTP {status}")
    return f"{hint}｜原始返回：{raw[:200]}"


class MiMoASR:
    def __init__(self, api_key: str, base_url: str, model: str = "mimo-v2.5-asr",
                 language: str = "auto", timeout: int = 60):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.language = language
        self.timeout = timeout

    def transcribe(self, wav_bytes: bytes) -> str:
        if not self.api_key:
            raise RuntimeError("未配置 API Key：请在 config.json 填写（mimo.api_key 或 asr.api_key）。")
        b64 = base64.b64encode(wav_bytes).decode("utf-8")
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {"data": f"data:audio/wav;base64,{b64}"},
                        }
                    ],
                }
            ],
            "asr_options": {"language": self.language},
        }
        r = http_mod.session().post(
            f"{self.base_url}/chat/completions",
            headers={"api-key": self.api_key, "Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise RuntimeError(_friendly(r.status_code, r.text))
        try:
            return (r.json()["choices"][0]["message"]["content"] or "").strip()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"ASR 返回结构异常：{r.text[:200]}") from exc


class WhisperAPIASR:
    """OpenAI 兼容的 /audio/transcriptions 标准接口。"""

    def __init__(self, api_key: str, base_url: str, model: str = "whisper-1",
                 language: str = "zh", timeout: int = 60):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.language = language
        self.timeout = timeout

    def transcribe(self, wav_bytes: bytes) -> str:
        if not self.api_key:
            raise RuntimeError("未配置 API Key：请在 config.json 的 asr.api_key 填写。")
        files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
        data = {"model": self.model}
        if self.language and self.language != "auto":
            data["language"] = self.language
        r = http_mod.session().post(
            f"{self.base_url}/audio/transcriptions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            files=files,
            data=data,
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise RuntimeError(_friendly(r.status_code, r.text))
        try:
            return (r.json().get("text") or "").strip()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"ASR 返回结构异常：{r.text[:200]}") from exc


def make_asr(cfg: dict):
    """按配置创建 ASR 客户端（兼容旧配置：只填了 mimo 也能跑）。"""
    mimo = cfg.get("mimo", {})
    a = cfg.get("asr", {}) or {}
    provider = (a.get("provider") or "mimo").lower()
    api_key = a.get("api_key") or mimo.get("api_key", "")
    base_url = a.get("base_url") or mimo.get("base_url", "https://api.xiaomimimo.com/v1")
    model = a.get("model") or mimo.get("asr_model", "mimo-v2.5-asr")
    language = a.get("language") or mimo.get("language", "auto")

    if provider == "whisper_api":
        return WhisperAPIASR(api_key=api_key, base_url=base_url, model=model, language=language)
    return MiMoASR(api_key=api_key, base_url=base_url, model=model, language=language)
