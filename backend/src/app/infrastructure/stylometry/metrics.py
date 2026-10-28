"""Stage 1 stylometric fingerprint: deterministic, dependency-free text statistics.

All texts are pooled (sentences and tokens from every reference essay) so one long essay does not
dominate per-essay averages. `feature_vector` is a fixed-order list of features scaled to roughly
[0, 1]; FEATURE_NAMES documents the order (stored alongside the vector).
"""

from __future__ import annotations

import math
import re
import statistics
from collections import Counter
from collections.abc import Sequence
from typing import Any

from app.domain.essay.models import StyleAnalysis

SCHEMA_VERSION = 1
MTLD_THRESHOLD = 0.72

_ABBREVIATIONS = (
    "mr",
    "mrs",
    "ms",
    "dr",
    "prof",
    "sr",
    "jr",
    "st",
    "vs",
    "etc",
    "e.g",
    "i.e",
    "u.s",
    "u.k",
    "no",
    "fig",
    "approx",
    "dept",
    "univ",
)
_WORD = re.compile(r"[A-Za-z0-9]+(?:['\u2019][A-Za-z]+)?")
# Split only on the whitespace after terminal punctuation (optionally followed by a closing quote
# or bracket, kept with its sentence) when the next sentence starts with a capital or digit.
_SENTENCE_END = re.compile(
    r"(?:(?<=[.!?])|(?<=[.!?][\"'\u201d\u2019)\]]))\s+(?=[\"'\u201c\u2018(\[]?[A-Z0-9])"
)
_PARAGRAPH = re.compile(r"\n\s*\n")

TRANSITIONS = (
    "however",
    "moreover",
    "furthermore",
    "therefore",
    "thus",
    "hence",
    "consequently",
    "nevertheless",
    "nonetheless",
    "meanwhile",
    "instead",
    "indeed",
    "in addition",
    "additionally",
    "for example",
    "for instance",
    "as a result",
    "on the other hand",
    "in contrast",
    "similarly",
    "likewise",
    "ultimately",
    "finally",
    "first",
    "second",
    "third",
    "then",
    "afterward",
    "eventually",
    "in fact",
    "of course",
    "after all",
    "above all",
    "in particular",
    "specifically",
    "in short",
    "in conclusion",
    "overall",
    "still",
    "yet",
    "although",
    "even though",
    "because",
    "since",
    "while",
    "whereas",
    "at the same time",
    "as well as",
    "not only",
    "rather",
    "otherwise",
    "accordingly",
    "subsequently",
    "to be honest",
    "looking back",
    "that said",
    "so",
)
_TRANSITION_RES = [
    (phrase, re.compile(rf"(?<![A-Za-z]){re.escape(phrase)}(?![A-Za-z])", re.IGNORECASE))
    for phrase in TRANSITIONS
]

PUNCTUATION = {
    "comma": ",",
    "semicolon": ";",
    "colon": ":",
    "dash": "\u2014",
    "hyphen_dash": " - ",
    "parenthesis": "(",
    "exclamation": "!",
    "question": "?",
    "quote": '"',
    "ellipsis": "...",
}

FUNCTION_WORDS = (
    "the",
    "and",
    "of",
    "to",
    "a",
    "in",
    "that",
    "is",
    "i",
    "it",
    "for",
    "was",
    "with",
    "as",
    "my",
    "but",
    "on",
    "be",
    "this",
    "have",
    "not",
    "me",
    "we",
    "which",
    "would",
)

FEATURE_NAMES: list[str] = [
    "sentence_mean/40",
    "sentence_std/25",
    "burstiness_cv",
    "ttr",
    "mtld/150",
    "flesch/100",
    "fk_grade/20",
    "paragraph_words_mean/250",
    "sentences_per_paragraph/12",
    *[f"punct_{name}_per_1k/100" for name in PUNCTUATION],
    *[f"fw_{word}_per_1k/100" for word in FUNCTION_WORDS],
    "transitions_per_1k/50",
]


# ---- primitives -------------------------------------------------------------------------------


def tokenize(text: str) -> list[str]:
    return [w.lower().replace("\u2019", "'") for w in _WORD.findall(text)]


def split_sentences(text: str) -> list[str]:
    flat = re.sub(r"\s+", " ", text.strip())
    if not flat:
        return []
    protected = flat
    for abbr in _ABBREVIATIONS:
        protected = re.sub(
            rf"(?<![A-Za-z]){re.escape(abbr)}\.",
            lambda m: m.group(0)[:-1].replace(".", "\x00") + "\x01",  # keep original casing
            protected,
            flags=re.IGNORECASE,
        )
    parts = _SENTENCE_END.split(protected)
    sentences = [p.replace("\x01", ".").replace("\x00", ".").strip() for p in parts]
    return [s for s in sentences if tokenize(s)]


def split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in _PARAGRAPH.split(text) if p.strip()]


def count_syllables(word: str) -> int:
    word = word.lower().strip("'")
    if not word:
        return 0
    if len(word) <= 3:
        return 1
    word = re.sub(r"(?:[^laeiouy]es|ed|[^laeiouy]e)$", "", word)
    word = re.sub(r"^y", "", word)
    return max(1, len(re.findall(r"[aeiouy]{1,2}", word)))


def _mtld_pass(tokens: Sequence[str], threshold: float) -> float:
    factors = 0.0
    types: set[str] = set()
    count = 0
    ttr = 1.0
    for token in tokens:
        count += 1
        types.add(token)
        ttr = len(types) / count
        if ttr <= threshold:
            factors += 1
            types.clear()
            count = 0
            ttr = 1.0
    if count > 0:
        factors += (1 - ttr) / (1 - threshold)
    return len(tokens) / factors if factors > 0 else float(len(tokens))


def mtld(tokens: Sequence[str], threshold: float = MTLD_THRESHOLD) -> float:
    """Measure of Textual Lexical Diversity (McCarthy & Jarvis 2010): mean of forward and
    backward passes; robust to text length, unlike raw TTR."""
    if not tokens:
        return 0.0
    return (_mtld_pass(tokens, threshold) + _mtld_pass(list(reversed(tokens)), threshold)) / 2


def _rate(count: int, words: int) -> float:
    return round(count * 1000 / words, 3) if words else 0.0


def _clip(value: float) -> float:
    return round(max(0.0, min(1.5, value)), 6)


# ---- analyzer ---------------------------------------------------------------------------------


class StylometryAnalyzer:
    def analyze(self, texts: Sequence[str]) -> StyleAnalysis:
        joined = "\n\n".join(t.strip() for t in texts if t.strip())
        tokens = tokenize(joined)
        n_words = len(tokens)
        sentences = [s for t in texts for s in split_sentences(t)]
        lengths = [len(tokenize(s)) for s in sentences]
        paragraphs = [p for t in texts for p in split_paragraphs(t)]

        mean = statistics.fmean(lengths) if lengths else 0.0
        std = statistics.pstdev(lengths) if len(lengths) > 1 else 0.0
        cv = std / mean if mean else 0.0
        ttr = len(set(tokens)) / n_words if n_words else 0.0
        lexical_mtld = mtld(tokens)

        syllables = sum(count_syllables(w) for w in tokens)
        n_sent = max(1, len(sentences))
        words_per_sentence = n_words / n_sent if n_words else 0.0
        syll_per_word = syllables / n_words if n_words else 0.0
        flesch = 206.835 - 1.015 * words_per_sentence - 84.6 * syll_per_word
        fk_grade = 0.39 * words_per_sentence + 11.8 * syll_per_word - 15.59

        para_words = [len(tokenize(p)) for p in paragraphs]
        para_sents = [len(split_sentences(p)) for p in paragraphs]

        punct_counts = {name: joined.count(sym) for name, sym in PUNCTUATION.items()}
        punct_counts["dash"] += joined.count("\u2013") + joined.count("--")
        punctuation = {name: _rate(c, n_words) for name, c in punct_counts.items()}

        word_counts = Counter(tokens)
        function_words = {w: _rate(word_counts[w], n_words) for w in FUNCTION_WORDS}

        transitions = Counter({phrase: len(rx.findall(joined)) for phrase, rx in _TRANSITION_RES})
        transitions = Counter({k: v for k, v in transitions.items() if v})
        transition_total = sum(transitions.values())

        metrics: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "word_count": n_words,
            "sentence_count": len(sentences),
            "paragraph_count": len(paragraphs),
            "sentence_length": {
                "mean": round(mean, 3),
                "std": round(std, 3),
                "burstiness": round(cv, 4),
                "min": min(lengths) if lengths else 0,
                "max": max(lengths) if lengths else 0,
                "median": statistics.median(lengths) if lengths else 0,
                "histogram": _histogram(lengths),
            },
            "lexical": {
                "ttr": round(ttr, 4),
                "mtld": round(lexical_mtld, 2),
                "unique_words": len(set(tokens)),
                "avg_word_length": round(sum(len(w) for w in tokens) / n_words, 3)
                if n_words
                else 0.0,
            },
            "readability": {
                "flesch_reading_ease": round(flesch, 2),
                "flesch_kincaid_grade": round(fk_grade, 2),
                "syllables_per_word": round(syll_per_word, 3),
            },
            "paragraphs": {
                "words_mean": round(statistics.fmean(para_words), 2) if para_words else 0.0,
                "words_std": round(statistics.pstdev(para_words), 2)
                if len(para_words) > 1
                else 0.0,
                "sentences_mean": round(statistics.fmean(para_sents), 2) if para_sents else 0.0,
            },
            "punctuation_per_1k": punctuation,
            "function_words_per_1k": function_words,
            "transitions": {
                "per_1k": _rate(transition_total, n_words),
                "top": [
                    {"phrase": p, "count": c, "per_1k": _rate(c, n_words)}
                    for p, c in transitions.most_common(12)
                ],
                "openers": _sentence_openers(sentences),
            },
        }

        vector = [
            _clip(mean / 40),
            _clip(std / 25),
            _clip(cv),
            _clip(ttr),
            _clip(lexical_mtld / 150),
            _clip(max(0.0, flesch) / 100),
            _clip(max(0.0, fk_grade) / 20),
            _clip(metrics["paragraphs"]["words_mean"] / 250),
            _clip(metrics["paragraphs"]["sentences_mean"] / 12),
            *[_clip(punctuation[name] / 100) for name in PUNCTUATION],
            *[_clip(function_words[w] / 100) for w in FUNCTION_WORDS],
            _clip(_rate(transition_total, n_words) / 50),
        ]
        assert len(vector) == len(FEATURE_NAMES)
        return StyleAnalysis(
            metrics=metrics, feature_names=list(FEATURE_NAMES), feature_vector=vector
        )


def _histogram(lengths: Sequence[int]) -> list[dict[str, int | str]]:
    bins = [(1, 5), (6, 10), (11, 15), (16, 20), (21, 25), (26, 30), (31, 40), (41, math.inf)]
    out: list[dict[str, int | str]] = []
    for lo, hi in bins:
        label = f"{lo}+" if hi == math.inf else f"{lo}-{int(hi)}"
        out.append({"bin": label, "count": sum(1 for n in lengths if lo <= n <= hi)})
    return out


def _sentence_openers(sentences: Sequence[str]) -> list[dict[str, int | str]]:
    openers = Counter(tokenize(s)[0] for s in sentences if tokenize(s))
    return [{"word": w, "count": c} for w, c in openers.most_common(8)]
