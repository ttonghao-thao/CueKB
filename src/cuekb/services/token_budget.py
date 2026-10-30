"""Registered, deterministic token accounting for evidence delivery views."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

_STANDARD = re.compile(
    r"[^\W_\u3400-\u9fff]+(?:[._:/+\-][^\W_\u3400-\u9fff]+)*|[\u3400-\u9fff]|[^\w\s]",
    re.UNICODE,
)


class TokenizerUnavailable(ValueError):
    pass


class RegisteredTokenizer:
    def __init__(self, tokenizer_id: str, encode) -> None:
        self.id = tokenizer_id
        self._encode = encode

    def count(self, text: str) -> int:
        return len(self._encode(text))


@lru_cache(maxsize=8)
def _load_voice_tokenizer(path_value: str, tokenizer_id: str) -> RegisteredTokenizer:
    path = Path(path_value)
    if not path.is_file() or path.stat().st_size > 100 * 1024 * 1024:
        raise TokenizerUnavailable("voice_tokenizer_unavailable")
    try:
        from tokenizers import Tokenizer

        tokenizer = Tokenizer.from_file(str(path))
    except (ImportError, OSError, ValueError) as exc:
        raise TokenizerUnavailable("voice_tokenizer_invalid") from exc
    return RegisteredTokenizer(
        tokenizer_id,
        lambda text: tokenizer.encode(text, add_special_tokens=False).ids,
    )


class TokenizerRegistry:
    """Select only server-registered tokenizers; requests cannot load code or files."""

    def __init__(self, settings) -> None:
        self.settings = settings

    def for_profile(self, profile: str) -> RegisteredTokenizer:
        if profile == "standard":
            return RegisteredTokenizer("cuekb-unicode-lexical-v1", _STANDARD.findall)
        if profile != "voice":
            raise TokenizerUnavailable("unsupported_delivery_profile")
        if not self.settings.evidence_voice_tokenizer_path:
            raise TokenizerUnavailable("voice_tokenizer_not_configured")
        return _load_voice_tokenizer(
            self.settings.evidence_voice_tokenizer_path,
            self.settings.evidence_voice_tokenizer_id,
        )
