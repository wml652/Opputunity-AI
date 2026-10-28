"""Stylometry metrics against hand-computed fixtures."""

from __future__ import annotations

import math

import pytest

from app.infrastructure.stylometry.metrics import (
    FEATURE_NAMES,
    StylometryAnalyzer,
    count_syllables,
    mtld,
    split_sentences,
    tokenize,
)

analyzer = StylometryAnalyzer()


def test_sentence_splitting_handles_abbreviations() -> None:
    text = "Dr. Smith arrived at 9 a.m. today. He said: \u201cWe start now!\u201d Did it work? Yes."
    assert split_sentences(text) == [
        "Dr. Smith arrived at 9 a.m. today.",
        "He said: \u201cWe start now!\u201d",
        "Did it work?",
        "Yes.",
    ]


def test_hand_computed_sentence_stats() -> None:
    # sentence lengths: 2, 4, 1 words
    metrics = analyzer.analyze(["I ran. I ran far away. Go."]).metrics
    s = metrics["sentence_length"]
    mean = 7 / 3
    std = math.sqrt(((2 - mean) ** 2 + (4 - mean) ** 2 + (1 - mean) ** 2) / 3)  # population
    assert s["mean"] == pytest.approx(mean, abs=1e-3)
    assert s["std"] == pytest.approx(std, abs=1e-3)  # 1.247
    assert s["burstiness"] == pytest.approx(std / mean, abs=1e-4)  # 0.5345
    assert (s["min"], s["max"]) == (1, 4)
    # tokens: i ran i ran far away go -> 7 tokens, 5 types
    assert metrics["lexical"]["ttr"] == pytest.approx(5 / 7, abs=1e-4)
    assert metrics["word_count"] == 7
    assert metrics["sentence_count"] == 3


def test_uniform_sentences_have_zero_burstiness() -> None:
    text = "The cat sat down. The dog ran off. A bird flew by."
    assert analyzer.analyze([text]).metrics["sentence_length"]["burstiness"] == 0.0


def test_mtld_hand_computed() -> None:
    # Forward: a b a -> TTR 2/3=0.667 <= 0.72 closes factor 1 after 3 tokens; then "b a b a ..."
    tokens = ["a", "b"] * 6  # 12 tokens
    value = mtld(tokens)
    # Each full factor consumes 3 tokens (TTR hits 2/3); 12 tokens = 4 factors, no remainder.
    assert value == pytest.approx(3.0, abs=1e-6)
    assert mtld([]) == 0.0


def test_mtld_rewards_diverse_vocabulary() -> None:
    diverse = tokenize(
        "Curiosity pulled me toward robotics, then biology, later economics; each field "
        "taught distinct habits of evidence, patience, modeling, and argument."
    )
    repetitive = tokenize("I like it and I like it and I like it and I like it and I like it")
    assert mtld(diverse) > mtld(repetitive) * 3


def test_punctuation_and_transitions_per_1k() -> None:
    text = (
        "However, I stayed; the lab needed help. For example, we rebuilt the rig \u2014 twice. "
        "As a result, our data improved. However, questions remained?"
    )
    metrics = analyzer.analyze([text]).metrics
    words = metrics["word_count"]
    assert metrics["punctuation_per_1k"]["semicolon"] == pytest.approx(1000 / words, abs=0.01)
    assert metrics["punctuation_per_1k"]["dash"] == pytest.approx(1000 / words, abs=0.01)
    top = {t["phrase"]: t["count"] for t in metrics["transitions"]["top"]}
    assert top["however"] == 2
    assert top["for example"] == 1
    assert top["as a result"] == 1


def test_paragraph_cadence_and_readability() -> None:
    text = "One short line here.\n\nA second paragraph that is a bit longer than the first one."
    metrics = analyzer.analyze([text]).metrics
    assert metrics["paragraph_count"] == 2
    assert metrics["paragraphs"]["words_mean"] == pytest.approx((4 + 12) / 2)
    assert 0 < metrics["readability"]["flesch_reading_ease"] < 120


def test_feature_vector_is_stable_and_bounded() -> None:
    essays = ["I ran. I ran far away. Go.", "However, the second essay is longer; it adds depth."]
    a = analyzer.analyze(essays)
    b = analyzer.analyze(essays)
    assert a.feature_vector == b.feature_vector
    assert len(a.feature_vector) == len(FEATURE_NAMES) == len(a.feature_names)
    assert all(0.0 <= v <= 1.5 for v in a.feature_vector)


@pytest.mark.parametrize(
    ("word", "syllables"), [("cat", 1), ("water", 2), ("university", 5), ("the", 1)]
)
def test_syllable_heuristic(word: str, syllables: int) -> None:
    assert count_syllables(word) == syllables
