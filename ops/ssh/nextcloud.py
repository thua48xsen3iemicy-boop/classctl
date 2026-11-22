"""Validate the Nextcloud origin used in the portal's frame-ancestors policy."""

import re
from urllib.parse import urlsplit


HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
ORIGIN = re.compile(
    rf"https?://{HOST_LABEL}(?:\.{HOST_LABEL})*\.?(?::[0-9]{{1,5}})?/?",
    re.ASCII | re.IGNORECASE,
)


def nextcloud_origin(value):
    if value is None:
        return None
    message = "nextcloud_origin must be an HTTP or HTTPS origin without a path, or null"
    if not isinstance(value, str) or not ORIGIN.fullmatch(value):
        raise ValueError(message)
    url = urlsplit(value)
    try:
        port = url.port
    except ValueError:
        raise ValueError(message) from None
    if port == 0:
        raise ValueError(message)
    default_port = 443 if url.scheme == "https" else 80
    authority = url.hostname if port in (None, default_port) else f"{url.hostname}:{port}"
    return f"{url.scheme}://{authority}"
