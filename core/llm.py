"""大脑模型：OpenAI 兼容接口（DeepSeek / MiMo / 其他任意一家均可）。"""
from __future__ import annotations

import json
import re

import requests

ACTION_RE = re.compile(r"^\s*ACTION:\s*(\{.*\})\s*$", re.MULTILINE)


class LLM:
    def __init__(self, llm_cfg: dict, system_prompt: str, timeout: int = 120):
        self.api_key = llm_cfg.get("api_key", "")
        self.base_url = llm_cfg.get("base_url", "").rstrip("/")
        self.model = llm_cfg.get("model", "")
        self.temperature = llm_cfg.get("temperature", 0.9)
        self.auth = llm_cfg.get("auth", "auto")
        self.timeout = timeout
        self.system_prompt = system_prompt

    def chat(self, messages: list[dict]) -> str:
        if not self.api_key and "xiaomimimo" not in self.base_url:
            raise RuntimeError("未配置 LLM API Key：请在 config.json 填写（llm.api_key 或 mimo.api_key）。")
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": self.system_prompt}] + messages,
            "temperature": self.temperature,
            "stream": False,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            # 小米用 api-key 头；其他（DeepSeek/OpenAI 等）用 Bearer
            use_api_key = (self.auth == "api-key") or (self.auth == "auto" and "xiaomimimo" in self.base_url)
            if use_api_key:
                headers["api-key"] = self.api_key
            else:
                headers["Authorization"] = f"Bearer {self.api_key}"
        r = requests.post(
            f"{self.base_url}/chat/completions", headers=headers,
            data=json.dumps(payload), timeout=self.timeout,
        )
        if r.status_code != 200:
            hints = {
                401: "API Key 无效（401）：config.json 里填的 Key 错或已失效（小米 Token Plan 的 Key 是 tp- 开头）",
                402: "账户余额不足（402）：Token Plan 未订阅或额度用尽",
                404: "接口地址不对（404）：检查 base_url",
            }
            hint = hints.get(r.status_code, f"HTTP {r.status_code}")
            raise RuntimeError(f"大脑接口调用失败——{hint}｜原始返回：{r.text[:200]}")
        data = r.json()
        return (data["choices"][0]["message"]["content"] or "").strip()


def make_llm(cfg: dict, system_prompt: str = "") -> "LLM":
    """创建 LLM（llm 没填 Key 且是小米系时，自动复用 mimo 的 Key）。"""
    llm_cfg = dict(cfg.get("llm", {}))
    if not llm_cfg.get("api_key") and "xiaomimimo" in llm_cfg.get("base_url", ""):
        llm_cfg["api_key"] = cfg.get("mimo", {}).get("api_key", "")
    return LLM(llm_cfg, system_prompt=system_prompt)


def extract_action(reply: str) -> tuple[str, dict | None]:
    """从回复中拆出 ACTION 指令，返回 (纯文本回复, 动作字典或 None)。"""
    m = ACTION_RE.search(reply)
    if not m:
        return reply.strip(), None
    text = ACTION_RE.sub("", reply).strip()
    try:
        action = json.loads(m.group(1))
        if isinstance(action, dict) and "name" in action:
            return text, action
    except json.JSONDecodeError:
        pass
    return reply.strip(), None
