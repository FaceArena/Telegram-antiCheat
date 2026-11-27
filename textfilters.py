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



def is_photo_rating_bait(text: str) -> bool:
    """Detect photo-rating solicitation spam, regardless of emoji or casing."""
    normalized = normalize_text(text)
    patterns = (
        r"\bкто\s+(?:может\s+)?(?:оценить|оценит|заценить)\s+(?:мои?\s+)?(?:фото(?:чки|чек)?|фотки|снимки)\b",
        r"\b(?:оцените|зацените)\s+(?:мои?\s+)?(?:фото(?:чки|чек)?|фотки|снимки)\b",
        r"\bкто\s+хочет\s+(?:оценить|посмотреть)\s+(?:мои?\s+)?(?:фото(?:чки|чек)?|фотки)\b",
    )
    return any(re.search(pattern, normalized) for pattern in patterns)


def is_unicode_glyph_flood(text: str, *, min_chars: int = 180) -> bool:
    """Detect long symbol/control-character floods without flagging normal prose.

    Catches both one repeated glyph and long mixed strings of symbols or
    non-printing Unicode controls. Ordinary letters and digits are excluded.
    """
    normalized = unicodedata.normalize("NFKC", text)
    visible = [char for char in normalized if not char.isspace()]
    if len(visible) < min_chars:
        return False

    counts = Counter(visible)
    dominant_char, dominant_count = counts.most_common(1)[0]
    dominant_category = unicodedata.category(dominant_char)
    if dominant_count >= min_chars and dominant_category[0] in {"S", "C"}:
        return True

    suspicious_count = sum(
        1 for char in visible
        if unicodedata.category(char)[0] in {"S", "C", "P"}
    )
    return suspicious_count >= min_chars and suspicious_count / len(visible) >= 0.75
