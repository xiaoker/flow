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
    kwargs = dict(kwargs)
    timeout = getattr(session, "request_timeout", (8.0, 25.0))
    parsed = urlparse(url)
    is_sse_source = parsed.scheme == "https" and parsed.hostname == "query.sse.com.cn"
    is_bse_source = (
        parsed.scheme == "https"
        and parsed.hostname in {"www.bse.cn", "www.bseinfo.net"}
        and parsed.path == "/rzrqjyyexxController/summaryInfoResult.do"
    )
    proxy = _source_proxy_config() if (is_sse_source or is_bse_source) else None
    direct_error: requests.RequestException | None = None
    response: requests.Response | None = None

    def looks_like_bse_challenge(value: requests.Response) -> bool:
        if not is_bse_source:
            return False
        prefix = value.content[:256].lstrip().lower()
        # bseinfo.net labels the valid JSONP response as text/html, so the
        # content type alone is not evidence of a challenge.  WAF pages are
        # HTML documents and therefore have an unambiguous markup prefix.
        return prefix.startswith(b"<!doctype html") or prefix.startswith(b"<html")

    try:
        response = session.get(url, timeout=timeout, **kwargs)
        response.raise_for_status()
        if not looks_like_bse_challenge(response):
            if is_sse_source or is_bse_source:
                _record_transport(kwargs, "direct")
            return response
    except requests.RequestException as exc:
        direct_error = exc

    if proxy is None:
        if direct_error is not None:
            raise direct_error
        # A successful but challenge-shaped BSE response is retained for the
        # adapter to archive as pending when no relay credentials are present.
        assert response is not None
        _record_transport(kwargs, "direct")
        return response

    proxy_url, proxy_headers = proxy
    direct_response = response
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
    try:
        response = session.get(
            proxy_url,
            params=proxy_params,
            headers=proxy_headers,
            timeout=timeout,
        )
        response.raise_for_status()
    except requests.RequestException:
        if not (is_bse_source and direct_response is not None):
            raise
        # Keep the original official response as evidence when the relay
        # itself is unavailable.  The BSE adapter will archive it as pending
        # instead of turning a transport fallback outage into a fabricated
        # empty observation or an unrelated hard failure.
        _record_transport(kwargs, "direct_waf")
        return direct_response
    _record_transport(
        kwargs,
        "cloudflare_bse_relay" if is_bse_source else "cloudflare_sse_relay",
    )
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
