import threading
import time

import pytest

from trading_desk.bounded_http import BoundedHTTPExchange, BoundedHTTPError


class Reply:
    status_code = 200
    headers = {"X-Fixture": "yes"}

    def __init__(self, chunks):
        self.chunks, self.closed = chunks, False

    def iter_content(self, **kwargs):
        yield from self.chunks

    def close(self):
        self.closed = True


class Session:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def request(self, *args, **kwargs):
        self.calls.append(kwargs)
        return self.reply


def request(client, **extra):
    return client.request("GET", "https://fixture.invalid", max_bytes=8,
        max_chunks=2, chunk_size=4, deadline_seconds=0.1, **extra)


def test_normal_reply_closes_and_disallows_redirects_or_retries():
    reply = Reply([b"a", b"b"]); session = Session(reply)
    result = request(BoundedHTTPExchange(session))
    assert result.content == b"ab" and result.headers["X-Fixture"] == "yes"
    assert reply.closed and len(session.calls) == 1
    assert session.calls[0]["timeout"] == (5, 10)
    assert session.calls[0]["allow_redirects"] is False


@pytest.mark.parametrize("chunks", [[b"a" * 9], [b"", b"", b""], ["not-bytes"]])
def test_bounds_reject_and_close_without_partial_success(chunks):
    reply = Reply(chunks); session = Session(reply)
    with pytest.raises(BoundedHTTPError, match="response_limit"):
        request(BoundedHTTPExchange(session))
    assert reply.closed and len(session.calls) == 1


@pytest.mark.parametrize("where", ["headers", "body"])
def test_deadline_does_not_cancel_late_effect_or_permit_second_request(where):
    finish = threading.Event()
    reply = Reply([b"ok"])
    class SlowSession(Session):
        def request(self, *args, **kwargs):
            if where == "headers":
                finish.wait(2)
            return super().request(*args, **kwargs)
    class SlowReply(Reply):
        def iter_content(self, **kwargs):
            finish.wait(2)
            yield b"ok"
    if where == "body":
        reply = SlowReply([])
    session = SlowSession(reply); client = BoundedHTTPExchange(session)
    started = time.monotonic()
    try:
        with pytest.raises(BoundedHTTPError, match="response_deadline"):
            request(client)
        assert time.monotonic() - started < 0.5
        with pytest.raises(BoundedHTTPError, match="still_unconfirmed"):
            request(client)
    finally:
        finish.set(); client.inflight.join(1)
    assert reply.closed and len(session.calls) == 1


def test_close_failure_does_not_return_success_or_leak_secret():
    class BadClose(Reply):
        def close(self):
            raise RuntimeError("fixture-secret")
    with pytest.raises(BoundedHTTPError, match="request_unavailable") as error:
        request(BoundedHTTPExchange(Session(BadClose([b"ok"]))))
    assert "fixture-secret" not in str(error.value)
