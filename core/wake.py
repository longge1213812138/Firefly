"""唤醒：语音唤醒「Hi Firefly」（Porcupine）+ 空格键兜底。

engine = auto 时：
  1. 有 Picovoice access_key 且有 .ppn 唤醒词文件 → 真·语音唤醒
  2. 否则 → 空格键说话（兜底，无需任何 Key，马上可用）
"""
from __future__ import annotations

import time


class WakeListener:
    def __init__(self, wake_cfg: dict, sample_rate: int = 16000, device: int | None = None):
        self.cfg = wake_cfg or {}
        self.engine = self.cfg.get("engine", "auto")
        self.keyword = self.cfg.get("keyword", "Hi Firefly")
        self.access_key = self.cfg.get("porcupine_access_key", "")
        self.keyword_path = self.cfg.get("porcupine_keyword_path", "")
        self.sensitivity = float(self.cfg.get("sensitivity", 0.85))
        self.sample_rate = sample_rate
        self.device = device
        self._porcupine = None
        self._builtin = None

    # ---------- Porcupine 语音唤醒 ----------
    def _try_porcupine(self) -> bool:
        if not (self.access_key and self.keyword_path):
            return False
        try:
            import pvporcupine  # type: ignore
        except Exception:
            return False
        try:
            self._porcupine = pvporcupine.create(
                access_key=self.access_key,
                keyword_paths=[self.keyword_path],
                sensitivities=[self.sensitivity],
            )
            return True
        except Exception:
            return False

    def _porcupine_wait(self, timeout: float | None = None) -> str:
        import numpy as np
        import sounddevice as sd

        p = self._porcupine
        frame_len = p.frame_length
        t0 = time.time()
        with sd.InputStream(samplerate=p.sample_rate, channels=1, dtype="int16",
                            blocksize=frame_len, device=self.device) as stream:
            while True:
                data, _ = stream.read(frame_len)
                pcm = data.reshape(-1)
                if len(pcm) < frame_len:
                    continue
                if p.process(pcm) >= 0:
                    return "wake"
                if timeout and (time.time() - t0) > timeout:
                    return "timeout"

    # ---------- 键盘兜底 ----------
    def _keyboard_wait(self, timeout: float | None = None) -> str:
        import msvcrt

        t0 = time.time()
        while True:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in (" ", "\r", "\n"):
                    return "wake"
                if ch.lower() == "q":
                    return "quit"
            time.sleep(0.05)
            if timeout and (time.time() - t0) > timeout:
                return "timeout"

    def start(self) -> str:
        """确定实际使用的唤醒方式，返回 'porcupine' 或 'keyboard'。"""
        if self.engine == "keyboard":
            return "keyboard"
        if self.engine in ("auto", "porcupine") and self._try_porcupine():
            return "porcupine"
        return "keyboard"

    def wait(self, timeout: float | None = None) -> str:
        """阻塞等待唤醒，返回 'wake' / 'quit' / 'timeout'。"""
        if self._porcupine is not None:
            return self._porcupine_wait(timeout)
        return self._keyboard_wait(timeout)

    def close(self) -> None:
        if self._porcupine is not None:
            try:
                self._porcupine.delete()
            except Exception:
                pass
            self._porcupine = None
