from __future__ import annotations

from collections.abc import Iterator
import re
import unicodedata

from markdown_it import MarkdownIt
from markdown_it.token import Token


COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
RAW_HTML_LIKE_LINE_RE = re.compile(r"(?m)^ {0,3}<(?:/?[A-Za-z]|[!?])")
_COMMONMARK = MarkdownIt("commonmark", {"html": True})
_NONVISIBLE_BLOCK_TYPES = {"code_block", "fence", "html_block"}


def strip_html_comments(text: str) -> tuple[str, bool]:
    visible = COMMENT_RE.sub("", text)
    malformed = "<!--" in visible or "-->" in visible
    return visible, malformed


def mask_nonvisible_markdown_blocks(text: str) -> str:
    """Mask source lines CommonMark renders as code or raw HTML blocks."""

    lines = text.splitlines()
    hidden: set[int] = set()
    for token in _COMMONMARK.parse(text):
        if token.type not in _NONVISIBLE_BLOCK_TYPES or token.map is None:
            continue
        start, end = token.map
        hidden.update(range(start, end))
    return "\n".join("" if index in hidden else line for index, line in enumerate(lines))


def _walk_tokens(tokens: list[Token]) -> Iterator[Token]:
    for token in tokens:
        yield token
        if token.children:
            yield from _walk_tokens(token.children)


def contains_raw_html(text: str) -> bool:
    return RAW_HTML_LIKE_LINE_RE.search(text) is not None or any(
        token.type in {"html_block", "html_inline"}
        for token in _walk_tokens(_COMMONMARK.parse(text))
    )


def _inline_text(tokens: list[Token]) -> str:
    pieces: list[str] = []
    for token in tokens:
        if token.children:
            pieces.append(_inline_text(token.children))
        elif token.type in {"text", "code_inline"}:
            pieces.append(token.content)
        elif token.type in {"softbreak", "hardbreak"}:
            pieces.append(" ")
        elif token.type == "image":
            pieces.append(token.content)
    return "".join(pieces)


def normalized_rendered_inline_text(text: str) -> str:
    """Return formatting-insensitive CommonMark inline text for policy checks."""

    rendered = _inline_text(_COMMONMARK.parseInline(text))
    normalized = unicodedata.normalize("NFKC", rendered)
    return " ".join(normalized.split()).casefold()
