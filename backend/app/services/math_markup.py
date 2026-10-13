from __future__ import annotations

import re
from dataclasses import dataclass

# Literal Markdown and complete math spans must remain atomic when RAG text is cut.
_PROTECTED = re.compile(
    r"(?P<code>```.*?(?:```|\Z)|~~~.*?(?:~~~|\Z)|`[^`\n]+`)"
    r"|(?P<display>\$\$.*?\$\$)"
    r"|(?P<bracket>\\\[.*?\\\])"
    r"|(?P<inline>\\\(.*?\\\))"
    r"|(?P<dollar>(?<![\\$])\$(?!\$)[^\n$]+?(?<!\\)\$(?!\$))",
    re.S,
)


@dataclass(frozen=True)
class ProtectedSpan:
    start: int
    end: int
    kind: str


def protected_spans(text: str) -> list[ProtectedSpan]:
    return [ProtectedSpan(match.start(), match.end(), match.lastgroup or "code") for match in _PROTECTED.finditer(text)]


def safe_excerpt(text: str, target_chars: int) -> str:
    """Soft length target: never send the model an amputated formula or code block."""
    if target_chars < 1:
        raise ValueError("摘录长度必须为正整数")
    if len(text) <= target_chars:
        return text
    end = target_chars
    for span in protected_spans(text):
        if span.start < end < span.end:
            end = span.end
            break
    return text[:end]


def split_intact(text: str, target_chars: int) -> list[str]:
    """Prefer paragraph/sentence boundaries; allow one atomic formula above target."""
    if target_chars < 1:
        raise ValueError("分块长度必须为正整数")
    if not text.strip():
        return []
    spans = protected_spans(text)
    paragraphs: list[str] = []
    start = 0
    for boundary in re.finditer(r"\n\s*\n+", text):
        if any(span.start < boundary.start() < span.end for span in spans):
            continue
        paragraphs.append(text[start:boundary.start()])
        start = boundary.end()
    paragraphs.append(text[start:])
    pieces: list[str] = []
    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        atomic = protected_spans(paragraph)
        cursor = 0
        while cursor < len(paragraph):
            end = min(cursor + target_chars, len(paragraph))
            for span in atomic:
                if span.start < end < span.end:
                    end = span.start if span.start > cursor else span.end
                    break
            if end < len(paragraph):
                boundaries = [match.end() for match in re.finditer(r"[。；！？.!?]\s*|\s+", paragraph[cursor:end]) if match.end() > (end - cursor) // 2]
                for candidate in reversed(boundaries):
                    point = cursor + candidate
                    if not any(span.start < point < span.end for span in atomic):
                        end = point
                        break
            piece = paragraph[cursor:end].strip()
            if piece:
                pieces.append(piece)
            cursor = end
    return pieces
