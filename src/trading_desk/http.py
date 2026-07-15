from __future__ import annotations

from dataclasses import dataclass

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36 "
    "personal-trading-desk/0.1"
)


@dataclass(frozen=True)
class HttpSettings:
    connect_timeout_seconds: float = 8.0
    read_timeout_seconds: float = 25.0
    total_retries: int = 3


def build_session(settings: HttpSettings | None = None) -> requests.Session:
    settings = settings or HttpSettings()
    retry = Retry(
        total=settings.total_retries,
        connect=settings.total_retries,
        read=settings.total_retries,
        status=settings.total_retries,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": DEFAULT_USER_AGENT})
    session.request_timeout = (
        settings.connect_timeout_seconds,
        settings.read_timeout_seconds,
    )
    return session


def get(session: requests.Session, url: str, **kwargs: object) -> requests.Response:
    timeout = getattr(session, "request_timeout", (8.0, 25.0))
    response = session.get(url, timeout=timeout, **kwargs)
    response.raise_for_status()
    return response

