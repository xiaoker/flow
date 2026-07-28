from __future__ import annotations

from dataclasses import dataclass
import os
from urllib.parse import parse_qsl, urlparse

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
    parsed = urlparse(url)
    is_sse_source = parsed.scheme == "https" and parsed.hostname == "query.sse.com.cn"
    try:
        response = session.get(url, timeout=timeout, **kwargs)
        response.raise_for_status()
        if is_sse_source:
            _record_transport(kwargs, "direct")
        return response
    except requests.RequestException:
        proxy = _source_proxy_config()
        if not is_sse_source or proxy is None:
            raise

    proxy_url, proxy_headers = proxy
    proxy_params: dict[str, object] = {
        "path": parsed.path,
        **dict(parse_qsl(parsed.query, keep_blank_values=True)),
    }
    request_params = kwargs.get("params")
    if isinstance(request_params, dict):
        proxy_params.update(
            {
                str(key): value
                for key, value in request_params.items()
                if not str(key).startswith("_flow_")
            }
        )
    response = session.get(
        proxy_url,
        params=proxy_params,
        headers=proxy_headers,
        timeout=timeout,
    )
    response.raise_for_status()
    _record_transport(kwargs, "cloudflare_sse_relay")
    return response


def _source_proxy_config() -> tuple[str, dict[str, str]] | None:
    proxy_url = os.getenv("FLOW_SOURCE_PROXY_URL", "")
    ingest_key = os.getenv("FLOW_INGEST_API_KEY", "")
    access_id = os.getenv("FLOW_CF_ACCESS_CLIENT_ID", "")
    access_secret = os.getenv("FLOW_CF_ACCESS_CLIENT_SECRET", "")
    parsed = urlparse(proxy_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "flow.xiaoker.com"
        or not ingest_key
        or not access_id
        or not access_secret
    ):
        return None
    return proxy_url, {
        "Authorization": f"Bearer {ingest_key}",
        "CF-Access-Client-Id": access_id,
        "CF-Access-Client-Secret": access_secret,
    }


def _record_transport(kwargs: dict[str, object], transport: str) -> None:
    request_params = kwargs.get("params")
    if isinstance(request_params, dict):
        request_params["_flow_transport"] = transport
