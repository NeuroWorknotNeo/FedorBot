"""Преобразование Markdown-ответов Codex в HTML для Telegram и нарезка на сообщения.

Telegram принимает ограниченный HTML (``<b>``, ``<i>``, ``<s>``, ``<code>``,
``<pre>``, ``<a>``) и сообщения не длиннее 4096 символов. Здесь нет цели
поддержать весь Markdown — только то, что обычно встречается в ответах
Codex: блоки кода, инлайн-код, жирный/курсив, заголовки, списки,
ссылки и таблицы.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

TELEGRAM_LIMIT = 4096
SAFE_LIMIT = 4000  # запас на служебные теги

_FENCE_OPEN_RE = re.compile(r"^\s*```([^\s`]*)\s*$")
_FENCE_CLOSE_RE = re.compile(r"^\s*```\s*$")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_BOLD_UNDERSCORE_RE = re.compile(r"(?<!\w)__(.+?)__(?!\w)")
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_STRIKE_RE = re.compile(r"~~(.+?)~~")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+")
_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class Segment:
    kind: str  # "text" | "code"
    body: str
    lang: str = ""


def split_segments(markdown: str) -> list[Segment]:
    """Делит текст на обычные фрагменты и блоки кода (``` ... ```)."""
    segments: list[Segment] = []
    buf: list[str] = []
    in_code = False
    lang = ""
    for line in markdown.splitlines():
        if not in_code:
            match = _FENCE_OPEN_RE.match(line)
            if match:
                if buf:
                    segments.append(Segment("text", "\n".join(buf)))
                    buf = []
                in_code = True
                lang = match.group(1)
                continue
            buf.append(line)
        else:
            if _FENCE_CLOSE_RE.match(line):
                segments.append(Segment("code", "\n".join(buf), lang))
                buf = []
                in_code = False
                lang = ""
                continue
            buf.append(line)
    if buf or in_code:
        segments.append(Segment("code" if in_code else "text", "\n".join(buf), lang))
    return segments


def _render_marks(text: str) -> str:
    escaped = html.escape(text, quote=False)
    # Текст уже экранирован, поэтому URL не экранируем повторно — только кавычки.
    escaped = _LINK_RE.sub(
        lambda m: f'<a href="{m.group(2).replace(chr(34), "&quot;")}">{m.group(1)}</a>', escaped
    )
    escaped = _BOLD_RE.sub(r"<b>\1</b>", escaped)
    escaped = _BOLD_UNDERSCORE_RE.sub(r"<b>\1</b>", escaped)
    escaped = _ITALIC_RE.sub(r"<i>\1</i>", escaped)
    escaped = _STRIKE_RE.sub(r"<s>\1</s>", escaped)
    return escaped


def render_inline(text: str) -> str:
    """Рендерит одну строку Markdown в HTML (инлайн-код защищён от разметки)."""
    parts: list[str] = []
    pos = 0
    for match in _INLINE_CODE_RE.finditer(text):
        parts.append(_render_marks(text[pos : match.start()]))
        parts.append(f"<code>{html.escape(match.group(1))}</code>")
        pos = match.end()
    parts.append(_render_marks(text[pos:]))
    return "".join(parts)


def _render_line(line: str) -> str:
    header = _HEADER_RE.match(line)
    if header:
        return f"<b>{render_inline(header.group(1))}</b>"
    if line.strip() in {"---", "***", "___"}:
        return "———"
    bullet = _BULLET_RE.match(line)
    if bullet:
        line = f"{bullet.group(1)}• {line[bullet.end():]}"
    return render_inline(line)


def render_units(markdown: str) -> list[tuple[str, str]]:
    """Возвращает список (raw, html) — по строке, таблицы одним блоком ``<pre>``."""
    units: list[tuple[str, str]] = []
    lines = markdown.split("\n")
    i = 0
    while i < len(lines):
        if _TABLE_LINE_RE.match(lines[i]):
            block: list[str] = []
            while i < len(lines) and _TABLE_LINE_RE.match(lines[i]):
                block.append(lines[i].strip())
                i += 1
            raw = "\n".join(block)
            units.append((raw, f"<pre>{html.escape(raw)}</pre>"))
            continue
        units.append((lines[i], _render_line(lines[i])))
        i += 1
    return units


def render_code(body: str, lang: str = "") -> str:
    cls = f' class="language-{html.escape(lang, quote=True)}"' if lang else ""
    return f"<pre><code{cls}>{html.escape(body)}</code></pre>"


def _hard_split_raw(raw: str, limit: int) -> list[str]:
    """Режет сырой текст так, чтобы каждая часть после html.escape была не длиннее limit."""
    pieces: list[str] = []
    buf: list[str] = []
    size = 0
    for ch in raw:
        escaped_len = len(html.escape(ch))
        if size + escaped_len > limit and buf:
            pieces.append("".join(buf))
            buf = []
            size = 0
        buf.append(ch)
        size += escaped_len
    if buf:
        pieces.append("".join(buf))
    return pieces


def _split_code(body: str, max_len: int) -> list[str]:
    pieces: list[str] = []
    buf: list[str] = []
    size = 0
    for line in body.split("\n"):
        line_len = len(html.escape(line)) + 1
        if line_len > max_len:
            if buf:
                pieces.append("\n".join(buf))
                buf = []
                size = 0
            pieces.extend(_hard_split_raw(line, max_len))
            continue
        if size + line_len > max_len and buf:
            pieces.append("\n".join(buf))
            buf = []
            size = 0
        buf.append(line)
        size += line_len
    if buf:
        pieces.append("\n".join(buf))
    return pieces


def render_chunks(markdown: str, limit: int = SAFE_LIMIT) -> list[str]:
    """Превращает Markdown в список HTML-сообщений, каждое не длиннее ``limit``."""
    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current)
        current = ""

    def append(piece: str) -> None:
        nonlocal current
        if not piece:
            return
        if current and len(current) + 1 + len(piece) > limit:
            flush()
        current = f"{current}\n{piece}" if current else piece

    for segment in split_segments(markdown):
        if segment.kind == "code":
            overhead = len(render_code("", segment.lang))
            for piece in _split_code(segment.body, limit - overhead - 1):
                append(render_code(piece, segment.lang))
        else:
            for raw, rendered in render_units(segment.body):
                if len(rendered) <= limit:
                    append(rendered)
                else:
                    for piece in _hard_split_raw(raw, limit):
                        append(html.escape(piece, quote=False))
    flush()
    return chunks or [""]


def strip_html(text: str) -> str:
    """Запасной вариант: убирает теги, если Telegram не принял HTML."""
    return html.unescape(_TAG_RE.sub("", text))


def escape(text: str) -> str:
    return html.escape(str(text), quote=False)


def truncate(text: str, limit: int) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def format_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}д {hours}ч {minutes:02d}м"
    if hours:
        return f"{hours}ч {minutes:02d}м {sec:02d}с"
    if minutes:
        return f"{minutes}м {sec:02d}с"
    return f"{sec}с"


def format_tokens(count: int) -> str:
    """12 → «12», 12345 → «12.3k», 1234567 → «1.2M»."""
    count = int(count)
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1000:.1f}k"
    return f"{count / 1_000_000:.2f}M"


def format_bytes(size: int) -> str:
    """1536 → «1.5 КБ», 3_000_000 → «2.9 МБ»."""
    value = float(size)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            return f"{value:.0f} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ГБ"
