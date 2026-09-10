"""流式回复的按句切分器。

把 LLM 的流式 delta 攒成一句一句，供「边生成边合成语音、抢先播报」使用。

关键点：模型会在回复里输出 ACTION:{"name":...} 指令行，这些行**绝不能念出来**，
必须整行过滤。ACTION 行以「ACTION:」开头、后面是 JSON，通常独占一行且没有句末标点。
"""
from __future__ import annotations

import re

# 中文/英文句末标点 + 换行，都算一句结束
_SENT_END = re.compile(r"([。！？；!?;]|\n)")
# ACTION 行：整行以 ACTION: 开头（后面跟 JSON 指令）
_ACTION_LINE = re.compile(r"^\s*ACTION\s*:")


class SentenceBuffer:
    """增量接收 delta，吐出完整句子；自动剔除 ACTION 行。"""

    def __init__(self, min_len: int = 8, max_buf: int = 120):
        self.buf = ""
        self.min_len = min_len
        self.max_buf = max_buf
        self.dropped_action = False

    def feed(self, delta: str) -> list[str]:
        """喂入一段 delta，返回已凑成的完整句子列表（可能为空）。"""
        self.buf += delta
        out: list[str] = []
        while True:
            m = _SENT_END.search(self.buf)
            if not m:
                # 没有句末标点：积压过长就强制切，避免一直憋着不播
                if len(self.buf) >= self.max_buf:
                    piece = self._clean(self.buf)
                    self.buf = ""
                    if piece:
                        out.append(piece)
                break
            cut = m.end()
            piece = self._clean(self.buf[:cut])
            self.buf = self.buf[cut:]
            if len(piece) >= self.min_len:
                out.append(piece)
            # 太短则丢弃（等下一块拼更长），但标点已从 buf 中移除
        return [s for s in out if s]

    def _clean(self, piece: str) -> str:
        lines = piece.splitlines()
        kept = [ln for ln in lines if not _ACTION_LINE.match(ln)]
        if len(kept) < len(lines):
            self.dropped_action = True
        return "\n".join(kept).strip()

    def flush(self) -> list[str]:
        """流结束，把剩余缓冲吐出来（同样过滤 ACTION 行）。"""
        rest = self._clean(self.buf)
        self.buf = ""
        return [rest] if rest else []
