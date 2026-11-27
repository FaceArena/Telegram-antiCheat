import re
import time
import unicodedata
from collections import Counter
from collections import deque
from functools import lru_cache


# Zero-width, bidi and other formatting controls can visually split or
# reorder a blocked word without changing what a moderator sees.
_FORMAT_CONTROLS = re.compile(
    "[\\u00ad\\u061c\\u180e\\u200b-\\u200f"
    "\\u202a-\\u202e\\u2060-\\u206f\\ufeff"
    "\\U000e0001\\U000e0020-\\U000e007f]"
)

_SPACE_RUNS = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Нормализует регистр, совместимые Unicode-символы и форматирование."""
    text = unicodedata.normalize("NFKC", text)
    text = _FORMAT_CONTROLS.sub("", text)
    text = text.casefold().replace("ё", "е")
    return _SPACE_RUNS.sub(" ", text).strip()


@lru_cache(maxsize=256)
def _compile(words: tuple[str, ...]):
    normalized = {
        normalize_text(word)
        for word in words
        if isinstance(word, str) and normalize_text(word)
    }
    if not normalized:
        return None

    # Longer phrases first makes the returned match deterministic when one
    # stopword is a prefix of another.
    parts = sorted((re.escape(word) for word in normalized), key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")(?!\w)")


def find_stopword(text: str, words: tuple[str, ...]) -> str | None:
    """Ищет стоп-слово или фразу как отдельное слово."""
    rx = _compile(tuple(words))
    if rx is None:
        return None
    match = rx.search(normalize_text(text))
    return match.group(0) if match else None



def is_unicode_glyph_flood(text: str, *, min_chars: int = 180) -> bool:
    """Detect an excessive count of one repeated Unicode symbol/number.

    The absolute count is intentional: adding arbitrary text or symbols at the
    end must not dilute the signal and bypass the detector. Ordinary letters
    are excluded to avoid flagging normal elongated words.
    """
    normalized = unicodedata.normalize("NFKC", text)
    counts = Counter(
        char for char in normalized
        if not char.isspace() and not unicodedata.category(char).startswith("M")
    )
    if not counts:
        return False
    dominant_char, dominant_count = counts.most_common(1)[0]
    category = unicodedata.category(dominant_char)
    return (
        dominant_count >= min_chars
        and category in {"Nl", "So", "Sm", "Sc", "Sk"}
    )
