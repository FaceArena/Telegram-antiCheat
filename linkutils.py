from urllib.parse import urlparse


def normalize_host(raw: str) -> str | None:
    """'https://www.Example.com/path' -> 'example.com'. None, если не разобрать."""
    raw = (raw or "").strip().lower()
    if not raw:
        return None
    if "://" not in raw:
        raw = "//" + raw
    try:
        host = urlparse(raw).hostname
    except ValueError:
        return None
    if not host:
        return None
    return host.removeprefix("www.")


def is_allowed(host: str | None, whitelist: list[str]) -> bool:
    """Домен разрешён, если совпадает с белым списком или является его поддоменом."""
    if host is None:
        return False
    return any(host == d or host.endswith("." + d) for d in whitelist)
