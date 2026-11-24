import re
import time
import unicodedata
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


class FloodTracker:
    """Скользящее окно сообщений на пользователя. Хранится в памяти процесса."""

    def __init__(self):
        self._hits: dict[tuple[int, int], deque[float]] = {}
        self._albums: dict[tuple[int, int], tuple[str, float]] = {}
        self._ops = 0

    def hit(self, key: tuple[int, int], limit: int, window: int, now: float | None = None) -> int:
        """Регистрирует сообщение, возвращает число сообщений в окне."""
        now = time.monotonic() if now is None else now
        q = self._hits.setdefault(key, deque())
        q.append(now)
        cutoff = now - window
        while q and q[0] <= cutoff:
            q.popleft()
        self._ops += 1
        if self._ops % 5000 == 0:
            self._sweep(now)
        return len(q)

    def is_album_repeat(self, key: tuple[int, int], media_group_id: str, now: float | None = None) -> bool:
        """Альбом из нескольких фото учитывается как одно сообщение."""
        now = time.monotonic() if now is None else now
        last = self._albums.get(key)
        self._albums[key] = (media_group_id, now)
        return bool(last and last[0] == media_group_id)

    def _sweep(self, now: float):
        """Удаляет неактивные ключи, чтобы словари не росли бесконечно."""
        hit_cutoff = now - 120
        for key in [
            key for key, hits in self._hits.items()
            if not hits or hits[-1] < hit_cutoff
        ]:
            del self._hits[key]

        for key in [
            key for key, (_, timestamp) in self._albums.items()
            if timestamp < hit_cutoff
        ]:
            del self._albums[key]
