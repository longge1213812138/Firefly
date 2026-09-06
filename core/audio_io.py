"""音频输入/输出：麦克风录音（能量 VAD 自动断句）+ 播放。

只依赖 numpy 与 sounddevice，无需额外音频后端。
"""
from __future__ import annotations

import io
import queue
import time
import wave

import numpy as np
import sounddevice as sd


def list_devices() -> list[dict]:
    devices = []
    for i, d in enumerate(sd.query_devices()):
        devices.append(
            {
                "index": i,
                "name": d.get("name", ""),
                "inputs": d.get("max_input_channels", 0),
                "outputs": d.get("max_output_channels", 0),
            }
        )
    return devices


def _rms(block: np.ndarray) -> float:
    if block.size == 0:
        return 0.0
    f = block.astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(f * f)))


def record_until_silence(
    sample_rate: int = 16000,
    silence_threshold: float = 0.012,
    max_seconds: float = 20.0,
    min_seconds: float = 0.4,
    tail_silence_seconds: float = 1.0,
    device: int | None = None,
    on_level=None,
) -> np.ndarray:
    """录音直到检测到静音结束。返回 int16 单声道 numpy 数组。"""
    q: queue.Queue = queue.Queue()
    block_size = 1024

    def cb(indata, frames, time_info, status):  # noqa: ARG001
        q.put(indata.copy())

    chunks: list[np.ndarray] = []
    started = False
    last_voice_ts = time.time()
    t0 = time.time()

    with sd.InputStream(
        samplerate=sample_rate,
        channels=1,
        dtype="int16",
        blocksize=block_size,
        device=device,
        callback=cb,
    ):
        while True:
            try:
                block = q.get(timeout=1.0)
            except queue.Empty:
                if time.time() - t0 > max_seconds:
                    break
                continue

            chunks.append(block)
            level = _rms(block)
            if on_level:
                on_level(level)

            now = time.time()
            if level > silence_threshold:
                started = True
                last_voice_ts = now

            elapsed = now - t0
            if started:
                if elapsed > min_seconds and (now - last_voice_ts) > tail_silence_seconds:
                    break
            if elapsed > max_seconds:
                break

    if not chunks:
        return np.zeros((0,), dtype=np.int16)
    return np.concatenate(chunks, axis=0).reshape(-1)


def record_seconds(seconds: float = 3.0, sample_rate: int = 16000,
                   device: int | None = None) -> np.ndarray:
    """固定时长录音（用于麦克风测试）。"""
    frames = int(sample_rate * seconds)
    data = sd.rec(frames, samplerate=sample_rate, channels=1, dtype="int16", device=device)
    sd.wait()
    return data.reshape(-1)


def level_stats(data: np.ndarray) -> tuple[float, float]:
    """返回 (平均音量 RMS, 峰值)。"""
    if data.size == 0:
        return 0.0, 0.0
    f = data.astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(f * f))), float(np.max(np.abs(f)))


def to_wav_bytes(data: np.ndarray, sample_rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(data.astype(np.int16).tobytes())
    return buf.getvalue()


def play_wav_bytes(wav_bytes: bytes, device: int | None = None, tail_silence: float = 0.8) -> None:
    """播放 wav 音频字节（MiMo TTS 返回 wav）。

    tail_silence：末尾追加的静音秒数。蓝牙耳机/部分 Windows 声卡在音频快结束时
    会把尾部缓冲区截掉（典型表现为"最后两三个字没念出来"），补一段静音垫底，
    即使尾部被截，丢的也是静音而不是说话。
    """
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        sr = wf.getframerate()
        nch = wf.getnchannels()
        n = wf.getnframes()
        raw = wf.readframes(n)

    audio = np.frombuffer(raw, dtype=np.int16)
    if nch > 1:
        audio = audio.reshape(-1, nch).mean(axis=1)
    a = audio.astype(np.float32) / 32768.0

    if tail_silence > 0:
        pad = np.zeros(int(sr * tail_silence), dtype=np.float32)
        a = np.concatenate([a, pad])

    sd.play(a, samplerate=sr, device=device)
    sd.wait()


def play_beep(freq: int = 880, duration: float = 0.14, sample_rate: int = 16000, device: int | None = None) -> None:
    t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)
    tone = (0.25 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    sd.play(tone, samplerate=sample_rate, device=device)
    sd.wait()
