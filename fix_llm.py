#!/usr/bin/env python3
"""Fix core/llm.py to support <function=xxx> format from LLM."""
import re

# Read the file
with open('core/llm.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Add the new regex patterns after ACTION_RE
old = 'ACTION_RE = re.compile(r"^\\s*ACTION:\\s*(\\{.*\\})\\s*$", re.MULTILINE)\n\n# 常见 HTTP 错误的人话解释'
new = '''ACTION_RE = re.compile(r"^\\s*ACTION:\\s*(\\{.*\\})\\s*$", re.MULTILINE)

# 兼容 LLM 生成的 <function=xxx> 格式（MiMo 等模型可能用这种格式）
FUNCTION_CALL_RE = re.compile(
    r"<function=(\\w+)>(.*?)</function>",
    re.DOTALL
)
FUNCTION_PARAM_RE = re.compile(
    r"<parameter=(\\w+)>(.*?)