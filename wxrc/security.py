"""日志脱敏；只处理输出，不更改实际请求。"""
from __future__ import annotations

import re


def redact(text: str) -> str:
    text = re.sub(
        r"(?i)((?:key|uin|pass_ticket|appmsg_token|token|wap_sid2|slave_sid|cookie)\s*[=:]\s*)[^\s&;,]+",
        r"\1[redacted]", str(text),
    )
    return re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", text)
