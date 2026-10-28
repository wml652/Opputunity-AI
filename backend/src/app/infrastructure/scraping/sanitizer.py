"""Indirect prompt-injection defense for scraped HTML.

1. Remove content a human visitor cannot see but an LLM would read: <script>/<style>/<template>/
   <noscript>/<iframe>/<svg>/<object>, comments, and elements hidden by `hidden`,
   `aria-hidden="true"`, or inline CSS (display:none, visibility:hidden, opacity:0, zero size,
   zero font size, off-screen positioning).
2. Strip zero-width and bidirectional-control characters; normalize Unicode with NFKC.
3. Flag instruction-like text (visible OR hidden) into `injection_flags` for audit; hidden
   instructions are always flagged as `hidden_instruction`.

Sanitized text is still untrusted: the extractor wraps it in <untrusted_document> and accepts only
schema-validated JSON back.
"""

from __future__ import annotations

import re
import unicodedata

from bs4 import BeautifulSoup, Comment
from bs4.element import Tag

STRIP_TAGS = (
    "script",
    "style",
    "template",
    "noscript",
    "iframe",
    "svg",
    "object",
    "embed",
    "canvas",
    "head",
    "meta",
    "link",
    "form",
    "input",
    "button",
    "select",
    "textarea",
)

# Zero-width, BOM, soft hyphen, word joiner, bidi embeddings/overrides/isolates, tag characters.
INVISIBLE_CHARS = re.compile(
    "[\u00ad\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff\U000e0000-\U000e007f]"
)

_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(?:\.0+)?(?![.\d])"
    r"|font-size\s*:\s*0(?:px|em|rem|%)?(?![.\d])"
    r"|(?:^|;)\s*(?:width|height)\s*:\s*0(?:px)?\s*(?:;|$)"
    r"|(?:left|top|text-indent)\s*:\s*-\d{3,}px"
    r"|clip\s*:\s*rect\(\s*0",
    re.IGNORECASE,
)

INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "ignore_previous",
        re.compile(
            r"\b(ignore|disregard|forget)\b.{0,40}\b(previous|prior|above|all)\b.{0,40}\b(instructions?|prompts?|rules?)\b",
            re.I | re.S,
        ),
    ),
    ("role_marker", re.compile(r"(^|\n|\s)(system|assistant|developer)\s*:", re.I)),
    (
        "chat_template",
        re.compile(r"<\|?(im_start|im_end|system|endoftext)\|?>|\[/?INST\]|<<SYS>>", re.I),
    ),
    (
        "tool_call",
        re.compile(
            r"<\s*/?\s*(tool_call|function_call|tool_use)\b|\"(tool_calls|function_call)\"\s*:",
            re.I,
        ),
    ),
    (
        "exfiltration",
        re.compile(
            r"\b(send|post|forward|email|upload)\b.{0,60}"
            r"\b(password|api key|token|credentials?|passport|national id)\b",
            re.I | re.S,
        ),
    ),
    (
        "new_instructions",
        re.compile(
            r"\b(new|updated|override)\s+(instructions?|system prompt)\b|\byou are now\b"
            r"|\bact as\b.{0,30}\b(admin|system)\b",
            re.I | re.S,
        ),
    ),
]


def clean_text(text: str) -> str:
    return unicodedata.normalize("NFKC", INVISIBLE_CHARS.sub("", text))


def scan_injection(text: str, *, hidden: bool = False) -> list[dict[str, str]]:
    flags: list[dict[str, str]] = []
    for name, pattern in INJECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            start = max(0, match.start() - 30)
            excerpt = " ".join(text[start : match.end() + 30].split())[:160]
            flags.append(
                {
                    "pattern": "hidden_instruction" if hidden else name,
                    "detail": name,
                    "excerpt": excerpt,
                }
            )
    return flags


def _is_hidden(tag: Tag) -> bool:
    if tag.has_attr("hidden"):
        return True
    if str(tag.get("aria-hidden", "")).lower() == "true":
        return True
    style = tag.get("style")
    if style and _HIDDEN_STYLE.search(str(style)):
        return True
    classes = " ".join(tag.get("class") or []).lower()
    return any(c in classes.split() for c in ("sr-only", "visually-hidden", "hidden", "d-none"))


def sanitize_html(html: str) -> tuple[BeautifulSoup, list[dict[str, str]]]:
    """Return a cleaned soup (visible content only) and the injection flags found."""
    soup = BeautifulSoup(html, "html.parser")
    flags: list[dict[str, str]] = []
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        flags += scan_injection(clean_text(str(comment)), hidden=True)
        comment.extract()
    for tag in soup.find_all(STRIP_TAGS):
        tag.decompose()
    for tag in list(soup.find_all(True)):
        if not isinstance(tag, Tag) or tag.decomposed:
            continue
        if _is_hidden(tag):
            flags += scan_injection(clean_text(tag.get_text(" ")), hidden=True)
            tag.decompose()
    for node in soup.find_all(string=True):
        cleaned = clean_text(str(node))
        if cleaned != str(node):
            node.replace_with(cleaned)
    visible = soup.get_text(" ")
    flags += scan_injection(visible)
    return soup, _dedupe(flags)


def _dedupe(flags: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, str]] = []
    for flag in flags:
        key = (flag["pattern"], flag["excerpt"])
        if key not in seen:
            seen.add(key)
            out.append(flag)
    return out[:20]
