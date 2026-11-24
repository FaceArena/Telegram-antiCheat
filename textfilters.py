import re
import time
import unicodedata
from collections import deque
from functools import lru_cache

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None)


def normalize_text(text: str) -> str:
    """Регистр, ё/е, невидимые символы и «красивые» юникод-шрифты приводим к простому виду."""
    text = unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)
    return text.casefold().replace("ё", "е")


@lru_cache(maxsize=256)
def _compile(words: tuple[str, ...]):
    if not words:
        return None
    parts = sorted((re.escape(w) for w in words), key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")(?!\w)")


def find_stopword(text: str, words: tuple[str, ...]) -> str | None:
    """Ищет стоп-слово или фразу как отдельное слово. words уже нормализованы."""
    rx = _compile(words)
    if rx is None:
        return None
    m = rx.search(normalize_text(text))
    return m.group(0) if m else None


class FloodTracker:
    """Скользящее окно сообщений на пользователя. Хранится в памяти (один экземпляр бота)."""

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
        """Альбом из 10 фото считаем одним сообщением."""
        now = time.monotonic() if now is None else now
        last = self._albums.get(key)
        self._albums[key] = (media_group_id, now)
        return bool(last and last[0] == media_group_id)

    def _sweep(self, now: float):
        for k in [k for k, q in self._hits.items() if not q or q[-1] < now - 120]:
            del self._hits[k]
        for k in [k for k, (_, t) in self._albums.items() if t < now - 120]:
            del self._albums[k]
