"""Model catalog categories for the picker. Pure name heuristics over whatever `GET {base_url}/
models` returned for the USER's key - no model id is ever hardcoded, chosen or substituted here.

Categories: chat, reasoning, fast, vision, embedding, rerank, other (reward / safety / guard /
speech models that cannot answer a chat prompt)."""

from __future__ import annotations

import re
from dataclasses import dataclass

_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("embedding", re.compile(r"embed|[-/]e5[-/]|bge[-_]|arctic-embed|nv-embed|retriever", re.I)),
    ("rerank", re.compile(r"rerank|reranker|[-/]rank[-/]|ranking", re.I)),
    (
        "other",
        re.compile(
            r"reward|guard|safety|content-safety|nemoguard|topic-control|jailbreak|"
            r"parakeet|canary|whisper|riva|tts|asr|fastpitch|audio2|cosmos-predict|"
            r"sdxl|stable-diffusion|flux|edify|esm|alphafold|diffdock|molmim|genmol|"
            r"deplot|nvclip|grounding-dino|ocdrnet|ocr|paddle",
            re.I,
        ),
    ),
    (
        "vision",
        re.compile(
            r"vision|[-_]vl[-_]|[-_]vl$|vlm|vila|neva|paligemma|kosmos|fuyu|llava|-omni", re.I
        ),
    ),
    (
        "reasoning",
        re.compile(
            r"reason|think|[-/]r1\b|r1-|qwq|magistral|deepseek-r|o1|o3|o4|"
            r"[-_](super|ultra)[-_]|gpt-oss",
            re.I,
        ),
    ),
    (
        "fast",
        re.compile(
            r"(^|[-_/])(0\.5|1|1\.5|2|3|4|7|8|9)b\b|mini|nano|tiny|small|flash|lite|haiku|"
            r"phi-?[34]|gemma-?[23]?-?[0-9]?b?",
            re.I,
        ),
    ),
)
CATEGORIES = ("chat", "reasoning", "fast", "vision", "embedding", "rerank", "other")
CHAT_CAPABLE = frozenset({"chat", "reasoning", "fast", "vision"})


@dataclass(frozen=True, slots=True)
class ModelInfo:
    id: str
    category: str
    publisher: str

    @property
    def chat_capable(self) -> bool:
        return self.category in CHAT_CAPABLE


def categorize_model(model_id: str) -> str:
    for category, pattern in _RULES:
        if pattern.search(model_id):
            return category
    return "chat"


def build_catalog(model_ids: list[str]) -> list[ModelInfo]:
    """Every listed model once, sorted by category (as in CATEGORIES) then id."""
    order = {c: i for i, c in enumerate(CATEGORIES)}
    infos = {
        m: ModelInfo(m, categorize_model(m), m.split("/", 1)[0] if "/" in m else "")
        for m in model_ids
        if m.strip()
    }
    return sorted(infos.values(), key=lambda i: (order[i.category], i.id.lower()))
