from __future__ import annotations

from urllib.parse import urlsplit


def is_exact_public_url(url: str, *, host: str, path: str) -> bool:
    """Return whether *url* is the exact credential-free HTTPS endpoint."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
        hostname = parsed.hostname
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and hostname == host
        and parsed.netloc == host
        and port is None
        and parsed.username is None
        and parsed.password is None
        and parsed.path == path
        and not parsed.query
        and not parsed.fragment
    )
