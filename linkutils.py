import ipaddress
import re
from urllib.parse import urlsplit


_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_MAX_URL_LENGTH = 2048


def normalize_host(raw: str) -> str | None:
    """
    Приводит URL или hostname к каноническому виду.

    Примеры:
      'https://www.Example.com/path' -> 'example.com'
      'https://пример.рф' -> IDNA/punycode hostname
      некорректный URL/hostname -> None

    Не пытается определять визуально похожие IDN-домены:
    они сравниваются в ASCII-форме (punycode).
    """
    if not isinstance(raw, str):
        return None

    raw = raw.strip()
    if not raw or len(raw) > _MAX_URL_LENGTH:
        return None

    # Обратные слеши и управляющие/встроенные пробелы имеют разное
    # поведение в URL-парсерах и браузерах — неоднозначный ввод отклоняем.
    if "\\" in raw or any(ord(char) <= 32 or ord(char) == 127 for char in raw):
        return None

    candidate = raw if "://" in raw else f"//{raw}"

    try:
        parsed = urlsplit(candidate)
        host = parsed.hostname
        # Обращение к .port также валидирует формат и диапазон порта.
        _ = parsed.port
    except ValueError:
        return None

    if not host:
        return None

    # Разрешаем ровно одну завершающую DNS-точку; несколько точек
    # означают некорректное имя и не должны молча нормализоваться.
    if host.endswith("."):
        host = host[:-1]
    if not host or host.endswith("."):
        return None

    # IP-адреса нормализуем отдельно (включая IPv6).
    try:
        return ipaddress.ip_address(host).compressed.lower()
    except ValueError:
        pass

    try:
        host = host.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return None

    # Сохраняем прежнее поведение: www.example.com эквивалентен example.com.
    if host.startswith("www."):
        host = host[4:]

    if not host or len(host) > 253:
        return None

    labels = host.split(".")
    # Однокомпонентные имена (например, localhost) тоже распознаём:
    # иначе ссылка на внутренний hostname обходила бы блокировку.
    if any(not _DNS_LABEL.fullmatch(label) for label in labels):
        return None

    return host


def is_allowed(host: str | None, whitelist: list[str]) -> bool:
    """
    Домен разрешён только при точном совпадении с белым списком
    или если он является его поддоменом.
    """
    normalized_host = normalize_host(host) if host else None
    if normalized_host is None or not isinstance(whitelist, (list, tuple)):
        return False

    for item in whitelist:
        normalized_item = normalize_host(item)
        if normalized_item and (
            normalized_host == normalized_item
            or normalized_host.endswith("." + normalized_item)
        ):
            return True

    return False
