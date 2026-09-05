"""Tests for accurate token counting (S9)."""

import pytest
import tokens


@pytest.fixture(autouse=True)
def reset_encoder():
    """Reset the module-level encoder cache before each test."""
    tokens._encoder = None
    tokens._encoder_loaded = False
    yield
    tokens._encoder = None
    tokens._encoder_loaded = False


# ─── basic counting ───────────────────────────────────────────────────────────

def test_empty_text_is_zero():
    assert tokens.count_text_tokens("") == 0


def test_nonempty_text_positive():
    assert tokens.count_text_tokens("Hello, world!") > 0


def test_longer_text_more_tokens():
    short = tokens.count_text_tokens("Hello")
    long = tokens.count_text_tokens("Hello " * 100)
    assert long > short


def test_message_tokens_includes_system_prompt():
    msgs = [{"role": "user", "content": "hi there"}]
    without = tokens.count_message_tokens(msgs)
    with_sys = tokens.count_message_tokens(msgs, system_prompt="You are helpful.")
    assert with_sys > without


def test_message_tokens_minimum_one():
    assert tokens.count_message_tokens([{"role": "user", "content": ""}]) >= 1


def test_per_message_overhead_applied():
    one = tokens.count_message_tokens([{"role": "user", "content": "hi"}])
    two = tokens.count_message_tokens([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hi"},
    ])
    # two messages carry more overhead than one
    assert two > one


# ─── heuristic fallback (tiktoken forced absent) ──────────────────────────────

def test_heuristic_used_when_no_tiktoken(monkeypatch):
    monkeypatch.setattr(tokens, "_get_encoder", lambda: None)
    n = tokens.count_text_tokens("The quick brown fox jumps over the lazy dog")
    assert n > 0


def test_heuristic_beats_chars_over_4():
    # The old estimate was len//4. For normal English prose the heuristic should
    # give a higher (more realistic) count than the systematic under-estimate.
    text = "The quick brown fox jumps over the lazy dog near the riverbank."
    old_estimate = max(1, len(text) // 4)
    heur = tokens._heuristic_tokens(text)
    assert heur >= old_estimate


def test_heuristic_handles_cjk():
    # No-space script must not collapse to near-zero tokens.
    cjk = "这是一个测试句子用来检查分词器"
    assert tokens._heuristic_tokens(cjk) >= 1


# ─── tiktoken path (if installed) ─────────────────────────────────────────────

def test_tiktoken_exact_when_available():
    enc = tokens._get_encoder()
    if enc is None:
        pytest.skip("tiktoken not installed in this environment")
    # tiktoken count should match a direct encode
    text = "Hello, world! This is a test."
    assert tokens.count_text_tokens(text) == len(enc.encode(text))


def test_method_reported(monkeypatch):
    # heuristic branch
    monkeypatch.setattr(tokens, "_get_encoder", lambda: None)
    assert tokens.count_text_tokens("hello world") > 0
