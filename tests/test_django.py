"""minty_shared.django: the error envelope, the request middleware, the proxy client."""

import logging
import re
from types import SimpleNamespace

import pytest
import requests

from minty_shared import logctx

pytest.importorskip("django")


@pytest.fixture
def client():
    from django.test import Client

    return Client()


@pytest.fixture
def access_lines():
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = Capture(logging.DEBUG)
    handler.addFilter(logctx.ContextFilter())
    lg = logging.getLogger("shared-test.http")
    lg.setLevel(logging.DEBUG)
    lg.addHandler(handler)
    yield records
    lg.removeHandler(handler)


# --------------------------------------------------------------------------- envelope


def test_schema_validation_is_422_with_a_sentence_and_one_message_per_field(client):
    resp = client.post("/api/items", {"payee_name": "x"}, content_type="application/json")
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "I still need amount to continue."
    assert body["fields"] == {"amount": "The amount is still empty. Add the amount to continue."}
    assert "detail" not in body


def test_a_malformed_field_is_named_and_not_missing(client):
    resp = client.post(
        "/api/items", {"amount": "lots", "payee_name": "x"}, content_type="application/json"
    )
    body = resp.json()
    assert resp.status_code == 422
    assert body["error"] == "Mind checking amount? That didn't look quite right."
    assert body["fields"]["amount"] == "The amount doesn't look right. Check it and try again."


def test_a_refusal_keeps_its_status_fields_and_routing_keys(client):
    resp = client.get("/api/refused")
    assert resp.status_code == 409
    assert resp.json() == {
        "error": "That day is closed. Open it to change it.",
        "fields": {"day": "Closed."},
        "to": "dashboard",
    }


def test_a_mapped_domain_exception_and_a_status_function(client):
    assert client.get("/api/conflict").status_code == 409
    assert client.get("/api/conflict").json() == {
        "error": "That reference already exists. Use another one."
    }
    resp = client.get("/api/status")
    assert (resp.status_code, resp.json()) == (
        402,
        {"error": "Your card was declined. Use another card."},
    )


def test_http_error_and_unauthorized(client):
    resp = client.get("/api/http")
    assert (resp.status_code, resp.json()) == (404, {"error": "I couldn't find that bill."})
    resp = client.get("/api/private")
    assert (resp.status_code, resp.json()) == (401, {"error": "Unauthorized"})
    assert client.get("/api/private", HTTP_AUTHORIZATION="Bearer good").status_code == 200


def test_a_raised_http404_and_ninjas_authorization_error_use_the_error_key(client):
    resp = client.get("/api/missing")
    assert (resp.status_code, resp.json()) == (404, {"error": "I couldn't find that bill."})
    resp = client.get("/api/forbidden")
    assert (resp.status_code, resp.json()) == (403, {"error": "Forbidden"})
    resp = client.get("/api/nowhere")
    assert resp.status_code == 404


def test_unhandled_is_the_house_sentence_and_never_the_detail(client):
    resp = client.get("/api/boom")
    assert resp.status_code == 500
    assert resp.json() == {"error": "Something went wrong on my end. Mind trying again?"}
    assert "secret-detail" not in resp.content.decode()


def test_validation_message_cases():
    from minty_shared.django.errors import field_messages, validation_message

    assert validation_message([]) == "Some of those details didn't look right. Mind checking them?"
    errs = [
        {"type": "missing", "loc": ("body", "bill", "amount")},
        {"type": "missing", "loc": ("body", "bill", "payee_name")},
    ]
    assert validation_message(errs) == "I still need amount and payee name to continue."
    assert set(field_messages(errs)) == {"amount", "payee_name"}
    assert field_messages([{"type": "missing", "loc": ("body",)}]) == {}


# --------------------------------------------------------------------------- middleware


def test_the_trace_is_kept_echoed_and_the_line_written(client, access_lines):
    resp = client.get("/api/items", HTTP_X_REQUEST_ID="deadbeef")
    assert resp["X-Request-ID"] == "deadbeef"
    (record,) = access_lines
    assert record.trace == "deadbeef"
    assert re.fullmatch(r"GET /api/items 200 \d+ms", record.getMessage())
    assert (record.endpoint, record.method) == ("/api/items", "GET")
    assert logctx.current()["trace"] == "-"


@pytest.mark.parametrize("bad", [None, "SHORT", "deadbeef\nforged"])
def test_a_missing_or_malformed_trace_is_replaced(client, access_lines, bad):
    kw = {"HTTP_X_REQUEST_ID": bad} if bad else {}
    resp = client.get("/api/items", **kw)
    assert re.fullmatch(r"[0-9a-f]{8}", resp["X-Request-ID"])


def test_each_method_of_one_path_names_its_own_operation(client, access_lines):
    client.get("/api/items")
    client.post("/api/items", {"amount": 1, "payee_name": "x"}, content_type="application/json")
    assert [r.step for r in access_lines] == ["list_items", "create_item"]


def test_the_loggable_path_setting_blanks_a_secret(client, access_lines):
    client.get("/api/share/abc123secret")
    assert access_lines[0].getMessage().startswith("GET /api/share/[redacted] 200")
    assert "abc123secret" not in access_lines[0].getMessage()


def test_the_middleware_refuses_to_start_without_its_logger(settings):
    from django.core.exceptions import ImproperlyConfigured

    from minty_shared.django.middleware import RequestLoggingMiddleware

    settings.MINTY_HTTP_LOGGER = ""
    with pytest.raises(ImproperlyConfigured, match="MINTY_HTTP_LOGGER"):
        RequestLoggingMiddleware(lambda r: None)


# --------------------------------------------------------------------------- client


def _request(auth="Bearer t"):
    return SimpleNamespace(headers={"Authorization": auth} if auth else {})


def test_forward_sends_the_bearer_and_the_trace_and_keeps_the_status(monkeypatch):
    from minty_shared.django import client as mclient

    sent = {}

    def fake(method, url, headers=None, **kw):
        sent.update(method=method, url=url, headers=headers, **kw)
        return SimpleNamespace(status_code=402, json=lambda: {"error": "declined"})

    monkeypatch.setattr(requests, "request", fake)
    logctx.start_trace("a1b2c3d4")
    try:
        payload, status = mclient.forward(
            _request(),
            "/api/x",
            base="http://flask.test/",
            timeout=5,
            method="get",
            params={"a": 1},
        )
    finally:
        logctx.reset()
    assert (payload, status) == ({"error": "declined"}, 402)
    assert sent["url"] == "http://flask.test/api/x" and sent["method"] == "GET"
    assert sent["headers"]["Authorization"] == "Bearer t"
    assert sent["headers"]["X-Request-ID"] == "a1b2c3d4"


@pytest.mark.parametrize(
    ("raise_", "status"), [(requests.Timeout(), 504), (requests.ConnectionError(), 502)]
)
def test_forward_unreachable_is_its_own_status(monkeypatch, raise_, status):
    from minty_shared.django import client as mclient

    def fake(*a, **k):
        raise raise_

    monkeypatch.setattr(requests, "request", fake)
    with pytest.raises(mclient.UpstreamUnavailable) as caught:
        mclient.forward(_request(), "/x", base="http://f", timeout=1)
    assert caught.value.status == status


def test_forward_never_relays_html(monkeypatch):
    from minty_shared.django import client as mclient

    def not_json():
        raise ValueError("not json")

    monkeypatch.setattr(
        requests, "request", lambda *a, **k: SimpleNamespace(status_code=500, json=not_json)
    )
    with pytest.raises(mclient.UpstreamUnavailable) as caught:
        mclient.forward(_request(), "/x", base="http://f", timeout=1)
    assert caught.value.status == 502


def test_forward_without_a_bearer_is_a_programming_error():
    from minty_shared.django import client as mclient

    with pytest.raises(mclient.UpstreamUnavailable) as caught:
        mclient.forward(_request(auth=None), "/x", base="http://f", timeout=1)
    assert caught.value.status == 500


def test_proxy_is_a_json_response(monkeypatch):
    from minty_shared.django import client as mclient

    monkeypatch.setattr(
        requests, "request", lambda *a, **k: SimpleNamespace(status_code=201, json=lambda: [1, 2])
    )
    resp = mclient.proxy(_request(), "/x", base="http://f", timeout=1)
    assert resp.status_code == 201 and resp.content == b"[1, 2]"
