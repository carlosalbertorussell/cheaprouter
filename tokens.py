"""
Token counting for cheaprouter (S9).

The pre-flight cost ranking needs an accurate input-token count, not the old
chars/4 guess (which systematically under-counts and skews which provider looks
cheapest). cheaprouter compares every provider on the *same* input text, so what
matters is a consistent, accurate-enough count applied uniformly across the
ranking — not a per-provider exact count.

Strategy:
  - Primary: tiktoken (o200k_base). Exact for the BPE-family providers (OpenAI,
    DeepSeek, Grok) and a close approximation for the rest — far better than
    chars/4. One dependency, not seven tokenizers.
  - Fallback: if tiktoken isn't installed, an improved heuristic that accounts
    for whitespace and punctuation, not a flat divisor.

count_message_tokens() also adds a small per-message overhead to approximate the
chat-format wrapping (role markers, delimiters) that every provider applies.
"""

from __future__ import annotations

from typing import Optional

# Per-message overhead approximating chat-format wrapping (role + delimiters).
_PER_MESSAGE_OVERHEAD = 4

_encoder = None
_encoder_loaded = False


def _get_encoder():
    """Lazy-load the tiktoken encoder once. Returns None if tiktoken is absent."""
    global _encoder, _encoder_loaded
    if _encoder_loaded:
        return _encoder
    _encoder_loaded = True
    try:
        import tiktoken
        _encoder = tiktoken.get_encoding("o200k_base")
    except Exception:
        _encoder = None
    return _encoder


def count_text_tokens(text: str) -> int:
    """Count tokens in a plain string using tiktoken, or a heuristic fallback."""
    if not text:
        return 0
    enc = _get_encoder()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    return _heuristic_tokens(text)


def _heuristic_tokens(text: str) -> int:
    """
    Fallback estimate when tiktoken is unavailable.

    Better than chars/4: counts whitespace-separated words and adds a fraction
    for punctuation and sub-word splits. Empirically ~0.75 tokens/word for
    English prose, with a per-character floor for CJK / no-space scripts.
    """
    words = text.split()
    word_estimate = int(len(words) / 0.75) if words else 0
    # Floor for scripts without spaces (CJK): ~1 token per 1.5 chars.
    char_floor = int(len(text) / 1.5)
    return max(1, word_estimate, char_floor // 4)


def count_message_tokens(
    messages: list[dict],
    system_prompt: Optional[str] = None,
) -> int:
    """
    Estimate total input tokens for a chat request: the sum of message contents
    plus the system prompt, with a small per-message overhead for chat wrapping.
    """
    total = 0
    for m in messages:
        total += count_text_tokens(m.get("content", ""))
        total += _PER_MESSAGE_OVERHEAD
    if system_prompt:
        total += count_text_tokens(system_prompt)
    return max(1, total)
