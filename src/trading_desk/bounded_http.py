"""One bounded foreground HTTP exchange, with no retry or permission semantics.

A deadline cannot cancel requests' underlying socket reliably. A timed-out
daemon may finish only its original exchange; it never updates caller state.
New exchanges are refused while it lives. Callers must preserve ambiguous
write/allowance outcomes and cannot interpret deadline expiry as cancellation.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any


class BoundedHTTPError(ValueError):
    pass


@dataclass(frozen=True)
class HTTPReply:
    status: int
    content: bytes
    headers: Any


class BoundedHTTPExchange:
    def __init__(self, session: Any, *, monotonic=time.monotonic):
        self._session, self._clock = session, monotonic
        self._lock = threading.RLock()
        self.inflight: threading.Thread | None = None

    def ensure_idle(self) -> None:
        if self.inflight is not None and self.inflight.is_alive():
            raise BoundedHTTPError("request_still_unconfirmed")

    def request(self, method: str, url: str, *, max_bytes: int,
                chunk_size: int, max_chunks: int, deadline_seconds: float,
                **kwargs: Any) -> HTTPReply:
        if (any(isinstance(v, bool) or not isinstance(v, int) or v <= 0
                for v in (max_bytes, chunk_size, max_chunks))
                or isinstance(deadline_seconds, bool)
                or not isinstance(deadline_seconds, (int, float))
                or not 0 < deadline_seconds <= 20):
            raise BoundedHTTPError("invalid_response_bounds")
        with self._lock:
            self.ensure_idle()
            exchange: dict[str, Any] = {}

            def perform():
                response = None
                started = self._clock()
                try:
                    response = self._session.request(method, url, **kwargs,
                        timeout=(5, 10), stream=True, allow_redirects=False)
                    content = bytearray()
                    for count, chunk in enumerate(response.iter_content(chunk_size=chunk_size)):
                        if (count >= max_chunks or not isinstance(chunk, (bytes, bytearray))
                                or len(content) + len(chunk) > max_bytes
                                or self._clock() - started >= deadline_seconds):
                            raise BoundedHTTPError("response_limit")
                        content.extend(chunk)
                    if self._clock() - started >= deadline_seconds:
                        raise BoundedHTTPError("response_limit")
                    exchange["reply"] = HTTPReply(response.status_code, bytes(content),
                                                  getattr(response, "headers", {}).copy())
                except BoundedHTTPError as exc:
                    exchange["error"] = str(exc)
                except Exception:
                    exchange["error"] = "request_unavailable"
                finally:
                    if response is not None:
                        try:
                            response.close()
                        except Exception:
                            exchange["error"] = "request_unavailable"

            self.inflight = threading.Thread(target=perform, daemon=True)
            self.inflight.start()
            self.inflight.join(timeout=deadline_seconds)
            if self.inflight.is_alive():
                raise BoundedHTTPError("response_deadline")
            if "error" in exchange:
                raise BoundedHTTPError(exchange["error"])
            return exchange["reply"]
