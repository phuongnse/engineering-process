from __future__ import annotations

import re


COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
FENCE_RE = re.compile(
    r"^ {0,3}(?:(?P<backticks>`{3,})(?P<backtick_info>[^`]*)|"
    r"(?P<tildes>~{3,})[^\r\n]*)$"
)
RAW_TAG_START_RE = re.compile(
    r"^ {0,3}</?(?P<tag>[A-Za-z][A-Za-z0-9-]*)(?:\s|/?>)",
    re.IGNORECASE,
)
RAW_CONTAINER_TAGS = {"pre", "script", "style", "textarea"}
RAW_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "base",
    "basefont",
    "blockquote",
    "body",
    "caption",
    "center",
    "col",
    "colgroup",
    "dd",
    "details",
    "dialog",
    "dir",
    "div",
    "dl",
    "dt",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "frame",
    "frameset",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "head",
    "header",
    "hr",
    "html",
    "iframe",
    "legend",
    "li",
    "link",
    "main",
    "menu",
    "menuitem",
    "nav",
    "noframes",
    "ol",
    "optgroup",
    "option",
    "p",
    "param",
    "search",
    "section",
    "summary",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "title",
    "tr",
    "track",
    "ul",
}


def strip_html_comments(text: str) -> tuple[str, bool]:
    visible = COMMENT_RE.sub("", text)
    malformed = "<!--" in visible or "-->" in visible
    return visible, malformed


def mask_fenced_code(text: str) -> str:
    visible: list[str] = []
    active_character: str | None = None
    active_length = 0
    for line in text.splitlines():
        match = FENCE_RE.fullmatch(line)
        if active_character is None:
            if match is None:
                visible.append(line)
                continue
            fence = match.group("backticks") or match.group("tildes")
            active_character = fence[0]
            active_length = len(fence)
            visible.append("")
            continue
        closing = re.fullmatch(
            rf" {{0,3}}{re.escape(active_character)}{{{active_length},}}[ \t]*",
            line,
        )
        if closing is not None:
            active_character = None
            active_length = 0
        visible.append("")
    return "\n".join(visible)


def _opening_tag_end(line: str, start: int) -> int | None:
    quote: str | None = None
    for index in range(start, len(line)):
        character = line[index]
        if quote is not None:
            if character == quote:
                quote = None
            continue
        if character in {'"', "'"}:
            quote = character
        elif character == ">":
            return index
    return None


def _closing_tag_end(line: str, tag: str, start: int = 0) -> int | None:
    cursor = start
    closing = re.compile(rf"</{re.escape(tag)}\s*>", re.IGNORECASE)
    while True:
        candidate = line.find("<", cursor)
        if candidate < 0:
            return None
        match = closing.match(line, candidate)
        if match is not None:
            return match.end()
        tag_end = _opening_tag_end(line, candidate)
        if tag_end is None:
            return None
        cursor = tag_end + 1


def mask_raw_html_containers(text: str) -> str:
    visible: list[str] = []
    active: tuple[str, str] | None = None
    for line in text.splitlines():
        if active is not None:
            visible.append("")
            kind, terminator = active
            if kind == "tag" and _closing_tag_end(line, terminator) is not None:
                active = None
            elif kind == "token" and terminator in line:
                active = None
            elif kind == "blank" and not line.strip():
                active = None
            continue

        stripped = line.lstrip(" ")
        indentation = len(line) - len(stripped)
        if indentation <= 3 and stripped.startswith("<?"):
            visible.append("")
            if "?>" not in stripped[2:]:
                active = ("token", "?>")
            continue
        if indentation <= 3 and stripped.startswith("<![CDATA["):
            visible.append("")
            if "]]>" not in stripped[9:]:
                active = ("token", "]]>")
            continue
        if indentation <= 3 and re.match(r"<![A-Z]", stripped):
            visible.append("")
            if _opening_tag_end(stripped, 0) is None:
                active = ("token", ">")
            continue

        match = RAW_TAG_START_RE.match(line)
        if match is None:
            visible.append(line)
            continue
        tag = match.group("tag").casefold()
        tag_end = _opening_tag_end(line, match.start())
        if tag not in RAW_BLOCK_TAGS and tag not in RAW_CONTAINER_TAGS:
            if tag_end is None or line[tag_end + 1 :].strip():
                visible.append(line)
                continue
        visible.append("")
        if tag in RAW_CONTAINER_TAGS:
            if tag_end is not None and _closing_tag_end(
                line, tag, start=tag_end + 1
            ) is not None:
                continue
            active = ("tag", tag)
            continue
        # CommonMark block tags and complete standalone HTML tags remain raw HTML
        # until a blank line. A slash on a non-void HTML element does not make it
        # safe: HTML ignores that self-closing flag.
        active = ("blank", tag)
    return "\n".join(visible)
