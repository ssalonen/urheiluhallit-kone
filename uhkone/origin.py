"""Explicit origins: the real shop or a local simulator on an IPv4 loopback port."""
from urllib.parse import urlsplit


PRODUCTION_URL = "https://kauppa.urheiluhallit.fi"


def validate_base_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Kaupan osoitteen pitää olla merkkijono.")
    try:
        parsed = urlsplit(value)
        if (parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"}):
            raise ValueError("Kaupan perusosoite ei saa sisältää tunnuksia tai polkua.")
        if parsed.scheme == "https" and parsed.hostname == "kauppa.urheiluhallit.fi" and parsed.port in (None, 443):
            return PRODUCTION_URL
        if (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                and parsed.port is not None and 1 <= parsed.port <= 65535):
            return f"http://127.0.0.1:{parsed.port}"
    except ValueError as exc:
        raise ValueError("Kaupaksi sallitaan vain Urheiluhallien HTTPS-osoite tai paikallinen 127.0.0.1:portti-testikauppa.") from exc
    raise ValueError("Kaupaksi sallitaan vain Urheiluhallien HTTPS-osoite tai paikallinen 127.0.0.1:portti-testikauppa.")


def is_local_test(base_url: str) -> bool:
    return validate_base_url(base_url) != PRODUCTION_URL


def same_origin(url: str, base_url: str) -> bool:
    base = urlsplit(validate_base_url(base_url))
    try:
        candidate = urlsplit(url)
        return (not candidate.username and not candidate.password
                and candidate.scheme == base.scheme and candidate.hostname == base.hostname
                and (candidate.port or (443 if candidate.scheme == "https" else 80))
                == (base.port or (443 if base.scheme == "https" else 80)))
    except ValueError:
        return False
