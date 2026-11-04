"""minty_shared.ai: the engine every document reader shares. Lifted with the generic cases of
Flask's tests/test_expense_ai.py (2026-11-04); each reader keeps its own tests for its prompt,
context and the check of the reply against the lists it sent.

No network: the client is a stand-in throughout - a test that reached Google would test Google.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pydantic")

from pydantic import BaseModel  # noqa: E402

from minty_shared import ai  # noqa: E402

CONFIG = ai.ReaderConfig("TEST_AI")


class Reply(BaseModel):
    amount_value: float = 0.0
    amount_confidence: float = 0.0
    contact_id: str = ""
    contact_confidence: float = 0.0


SENT_CONTACTS = {"con-sf"}


def _validate(parsed: Reply) -> dict:
    contact = parsed.contact_id if parsed.contact_id in SENT_CONTACTS else ""
    amount = parsed.amount_value if 0 < parsed.amount_value <= 10_000_000 else ""
    return {
        "amount": ai.field(amount, parsed.amount_confidence, CONFIG),
        "contact": ai.field(contact, parsed.contact_confidence if contact else 0, CONFIG),
    }


def _reply(**over) -> str:
    body = {
        "amount_value": 120.0,
        "amount_confidence": 0.97,
        "contact_id": "con-sf",
        "contact_confidence": 0.9,
    }
    body.update(over)
    return json.dumps(body)


class FakeUsage:
    total_input_tokens = 5000
    total_output_tokens = 300
    total_thought_tokens = 400
    total_cached_tokens = 0


class FakeInteraction:
    def __init__(self, output_text=None, status="completed", errors=None):
        self.id = "interaction-1"
        self.output_text = output_text
        self.status = status
        self.errors = errors or []
        self.usage = FakeUsage()


class FakeInteractions:
    def __init__(self, result):
        self._result = result
        self.calls = 0
        self.last_request = None

    def create(self, **kwargs):
        self.calls += 1
        self.last_request = kwargs
        if isinstance(self._result, Exception):
            raise self._result
        if callable(self._result):
            return self._result(self.calls)
        return self._result


class FakeClient:
    def __init__(self, result):
        self.interactions = FakeInteractions(result)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv("TEST_AI_CONF_HIGH", "0.85")
    monkeypatch.setenv("TEST_AI_CONF_MEDIUM", "0.60")
    monkeypatch.setenv("TEST_AI_MODEL", "gemini-3.8-flash")
    monkeypatch.setattr(ai.time, "sleep", lambda _s: None)
    ai.reset_client()
    yield
    ai.reset_client()


def _run(monkeypatch, result):
    client = FakeClient(result)
    monkeypatch.setattr(ai, "get_client", lambda timeout_s: client)
    extraction = ai.read(
        b"%PDF-1.4 fake",
        "application/pdf",
        config=CONFIG,
        instruction="Read it.",
        prefix_text="## Lists",
        closing_text="Go.",
        reply_model=Reply,
        validate=_validate,
    )
    return extraction, client


def _status_error(klass, status, message):
    """A real SDK status error: ``interactions.create`` raises ``google.genai._gaos...
    compat_errors``, NOT the legacy ``google.genai.errors`` family."""
    import httpx

    request = httpx.Request("POST", "https://generativelanguage.googleapis.com/x")
    return klass(message, response=httpx.Response(status, request=request), body=None)


def _compat():
    pytest.importorskip("google.genai")
    from google.genai._gaos.lib import compat_errors

    return compat_errors


# --- the request ---------------------------------------------------------------------------------


def test_a_good_reply_is_validated_into_applied_fields(monkeypatch):
    extraction, _ = _run(monkeypatch, FakeInteraction(_reply()))
    assert extraction.reason is None
    assert extraction.suggestions["amount"] == {
        "value": 120.0,
        "confidence": 0.97,
        "band": "high",
        "applied": True,
    }
    assert extraction.answer() == {"suggestions": extraction.suggestions, "reason": None}


def test_the_stable_prefix_goes_first_then_the_document_then_the_closing(monkeypatch):
    _, client = _run(monkeypatch, FakeInteraction(_reply()))
    request = client.interactions.last_request
    kinds = [part["type"] for part in request["input"]]
    assert kinds == ["text", "document", "text"]
    assert request["input"][0]["text"] == "## Lists"
    assert request["input"][1]["mime_type"] == "application/pdf"
    assert request["system_instruction"] == "Read it."
    assert request["model"] == "gemini-3.8-flash"


def test_the_schema_requires_every_field_and_carries_no_titles(monkeypatch):
    _, client = _run(monkeypatch, FakeInteraction(_reply()))
    schema = client.interactions.last_request["response_format"]["schema"]
    assert schema["required"] == sorted(Reply.model_fields)
    assert all("title" not in p and "default" not in p for p in schema["properties"].values())


def test_an_image_is_sent_as_an_image(monkeypatch):
    client = FakeClient(FakeInteraction(_reply()))
    monkeypatch.setattr(ai, "get_client", lambda timeout_s: client)
    ai.read(
        b"\xff\xd8\xff",
        "image/jpeg",
        config=CONFIG,
        instruction="",
        prefix_text="",
        closing_text="",
        reply_model=Reply,
        validate=_validate,
    )
    assert client.interactions.last_request["input"][1]["type"] == "image"


def test_the_output_budget_leaves_room_to_think(monkeypatch):
    monkeypatch.delenv("TEST_AI_MAX_OUTPUT_TOKENS", raising=False)
    _, client = _run(monkeypatch, FakeInteraction(_reply()))
    assert client.interactions.last_request["generation_config"]["max_output_tokens"] >= 4096


# --- bands -----------------------------------------------------------------------------------


def test_low_confidence_is_never_applied_and_medium_is(monkeypatch):
    extraction, _ = _run(
        monkeypatch, FakeInteraction(_reply(amount_confidence=0.3, contact_confidence=0.7))
    )
    assert extraction.suggestions["amount"]["band"] == "low"
    assert extraction.suggestions["amount"]["applied"] is False
    assert extraction.suggestions["contact"]["band"] == "medium"
    assert extraction.suggestions["contact"]["applied"] is True


def test_nothing_usable_keeps_the_suggestions_and_says_why(monkeypatch):
    extraction, _ = _run(monkeypatch, FakeInteraction(_reply(amount_value=0, contact_id="nope")))
    assert extraction.reason == ai.REASON_NO_USABLE_FIELD
    assert extraction.answer() == {
        "suggestions": extraction.suggestions,
        "reason": ai.REASON_NO_USABLE_FIELD,
    }


@pytest.mark.parametrize("value, expected", [(1.5, 1.0), (-1, 0.0), ("x", 0.0), (None, 0.0)])
def test_confidence_is_clamped(value, expected):
    assert ai.clamp_confidence(value) == expected


# --- the failure matrix: every one is "no suggestion" ------------------------------------------


def test_malformed_json_is_discarded_whole(monkeypatch):
    extraction, _ = _run(monkeypatch, FakeInteraction("not json at all"))
    assert extraction.suggestions is None and extraction.reason == ai.REASON_INVALID_REPLY
    assert extraction.answer() == {"suggestions": None, "reason": ai.REASON_INVALID_REPLY}


def test_a_reply_failing_the_schema_is_discarded_whole(monkeypatch):
    extraction, _ = _run(monkeypatch, FakeInteraction(json.dumps({"amount_value": "lots"})))
    assert extraction.suggestions is None and extraction.reason == ai.REASON_INVALID_REPLY


def test_an_empty_reply_is_invalid(monkeypatch):
    extraction, _ = _run(monkeypatch, FakeInteraction(""))
    assert extraction.reason == ai.REASON_INVALID_REPLY


def test_a_safety_block_is_not_retried(monkeypatch):
    class Err:
        message = "Blocked by safety filters: dangerous_content"

    extraction, client = _run(
        monkeypatch, FakeInteraction(None, status="incomplete", errors=[Err()])
    )
    assert extraction.reason == ai.REASON_SAFETY_BLOCKED and client.interactions.calls == 1


def test_a_truncated_reply_is_discarded(monkeypatch):
    class Err:
        message = "max output tokens reached"

    extraction, _ = _run(monkeypatch, FakeInteraction(None, status="incomplete", errors=[Err()]))
    assert extraction.reason == ai.REASON_TRUNCATED


def test_any_other_status_is_a_provider_error(monkeypatch):
    extraction, _ = _run(monkeypatch, FakeInteraction(None, status="failed"))
    assert extraction.reason == ai.REASON_PROVIDER_ERROR


def test_a_timeout_is_not_retried(monkeypatch):
    extraction, client = _run(monkeypatch, TimeoutError("deadline exceeded"))
    assert extraction.reason == ai.REASON_TIMEOUT and client.interactions.calls == 1


def test_interactions_raises_a_different_family_than_the_legacy_api():
    compat = _compat()
    from google.genai import errors as legacy

    assert not issubclass(compat.RateLimitError, legacy.APIError)
    assert compat.RateLimitError.status_code == 429


@pytest.mark.parametrize("name, status", [("NotFoundError", 404), ("PermissionDeniedError", 403)])
def test_404_and_403_are_not_retried(monkeypatch, name, status):
    compat = _compat()
    extraction, client = _run(monkeypatch, _status_error(getattr(compat, name), status, "no"))
    assert extraction.reason == ai.REASON_PROVIDER_ERROR and client.interactions.calls == 1


def test_a_429_is_rate_limited_not_a_transport_error(monkeypatch):
    compat = _compat()
    extraction, _ = _run(monkeypatch, _status_error(compat.RateLimitError, 429, "quota exceeded"))
    assert extraction.reason == ai.REASON_RATE_LIMITED


def test_a_429_does_not_retry_when_the_advised_wait_blows_the_deadline(monkeypatch):
    compat = _compat()
    monkeypatch.setenv("TEST_AI_TIMEOUT_S", "25")
    slept = []
    monkeypatch.setattr(ai.time, "sleep", slept.append)
    error = _status_error(compat.RateLimitError, 429, "Quota exceeded. Please retry in 39.6s.")
    extraction, client = _run(monkeypatch, error)
    assert extraction.reason == ai.REASON_RATE_LIMITED
    assert client.interactions.calls == 1 and slept == []


def test_a_429_retries_once_when_the_advised_wait_fits(monkeypatch):
    compat = _compat()
    monkeypatch.setenv("TEST_AI_TIMEOUT_S", "25")

    def flaky(call_number):
        if call_number == 1:
            raise _status_error(compat.RateLimitError, 429, "Slow down. Please retry in 1.5s.")
        return FakeInteraction(_reply())

    extraction, client = _run(monkeypatch, flaky)
    assert extraction.reason is None and client.interactions.calls == 2


def test_an_sdk_timeout_is_recognised_by_type_and_not_retried(monkeypatch):
    compat = _compat()
    import httpx

    request = httpx.Request("POST", "https://generativelanguage.googleapis.com/x")
    extraction, client = _run(monkeypatch, compat.APITimeoutError(request=request))
    assert extraction.reason == ai.REASON_TIMEOUT and client.interactions.calls == 1


def test_a_server_error_retries_once_then_succeeds(monkeypatch):
    compat = _compat()

    def flaky(call_number):
        if call_number == 1:
            raise _status_error(compat.InternalServerError, 503, "unavailable")
        return FakeInteraction(_reply())

    extraction, client = _run(monkeypatch, flaky)
    assert extraction.reason is None and client.interactions.calls == 2


def test_a_transport_error_twice_is_a_provider_error(monkeypatch):
    extraction, client = _run(monkeypatch, ConnectionError("reset"))
    assert extraction.reason == ai.REASON_PROVIDER_ERROR and client.interactions.calls == 2


def test_an_unconfigured_client_makes_no_call(monkeypatch):
    def boom(timeout_s):
        raise RuntimeError("GEMINI_API_KEY is not set")

    monkeypatch.setattr(ai, "get_client", boom)
    extraction = ai.read(
        b"%PDF",
        "application/pdf",
        config=CONFIG,
        instruction="",
        prefix_text="",
        closing_text="",
        reply_model=Reply,
        validate=_validate,
    )
    assert extraction.suggestions is None and extraction.reason == ai.REASON_NOT_CONFIGURED


# --- usage and cost ---------------------------------------------------------------------------


def test_thinking_tokens_are_recorded_separately(monkeypatch):
    extraction, _ = _run(monkeypatch, FakeInteraction(_reply()))
    assert extraction.audit["output_tokens"] == 300 and extraction.audit["thought_tokens"] == 400


def test_cost_bills_thinking_at_the_output_rate(monkeypatch):
    extraction, _ = _run(monkeypatch, FakeInteraction(_reply()))
    expected = (5000 / 1_000_000) * 0.75 + ((300 + 400) / 1_000_000) * 3.75
    assert extraction.audit["estimated_cost"] == round(expected, 6)


def test_an_unpriced_model_has_no_cost():
    assert ai.estimate_cost("gemini-unknown", 100, 100) is None


def test_the_log_line_carries_numbers_never_values(monkeypatch, caplog):
    caplog.set_level("INFO", logger="minty-shared.ai")
    extraction, _ = _run(monkeypatch, FakeInteraction(_reply(amount_value=4321.5)))
    ai.log_read(CONFIG, extraction, entity_id="ent-1", user_id="user-1")
    line = caplog.records[-1].getMessage()
    assert line.startswith("test_ai: extract entity=ent-1 user=user-1 model=gemini-3.8-flash")
    assert "tokens_in=5000" in line and "status=ok" in line
    assert "4321" not in line and "con-sf" not in line


# --- sniffing and rate limiting ----------------------------------------------------------------


@pytest.mark.parametrize(
    "data, expected",
    [
        (b"%PDF-1.7 ...", "application/pdf"),
        (b"\xff\xd8\xff\xe0 JFIF", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\n rest", "image/png"),
        (b"MZ\x90\x00 an executable named receipt.pdf", None),
        (b"", None),
    ],
)
def test_the_type_is_sniffed_from_the_bytes(data, expected):
    assert ai.sniff_mime(data) == expected


def test_the_rate_limiter_sheds_load_per_user(monkeypatch):
    monkeypatch.setenv("TEST_AI_RATE_USER_PER_MIN", "3")
    monkeypatch.setenv("TEST_AI_RATE_ENTITY_PER_HOUR", "1000")
    limiter = ai.RateLimiter()
    assert [limiter.allow("u", "e", CONFIG) for _ in range(4)] == [True, True, True, False]
    assert limiter.allow("u2", "e", CONFIG) is True


def test_the_entity_limit_is_separate_from_the_user_limit(monkeypatch):
    monkeypatch.setenv("TEST_AI_RATE_USER_PER_MIN", "1000")
    monkeypatch.setenv("TEST_AI_RATE_ENTITY_PER_HOUR", "2")
    limiter = ai.RateLimiter()
    assert [limiter.allow(u, "shared", CONFIG) for u in ("a", "b", "c")] == [True, True, False]
    limiter.reset()
    assert limiter.allow("c", "shared", CONFIG) is True


def test_each_reader_has_its_own_limits_and_switch(monkeypatch):
    monkeypatch.setenv("TEST_AI_ENABLED", "true")
    monkeypatch.delenv("OTHER_AI_ENABLED", raising=False)
    assert CONFIG.enabled() is True
    assert ai.ReaderConfig("OTHER_AI").enabled() is False
    assert CONFIG.label == "test_ai"


# --- the switches that keep it safe by default --------------------------------------------------


def test_a_reader_is_off_by_default(monkeypatch):
    monkeypatch.delenv("TEST_AI_ENABLED", raising=False)
    assert CONFIG.enabled() is False


def test_the_direct_api_needs_an_explicit_opt_in(monkeypatch):
    pytest.importorskip("google.genai")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("EXPENSE_AI_ALLOW_DIRECT_API", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "a-key-that-should-not-be-used")
    with pytest.raises(RuntimeError, match="not opted in"):
        ai.get_client(25)


def test_the_direct_api_needs_a_key(monkeypatch):
    pytest.importorskip("google.genai")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv("EXPENSE_AI_ALLOW_DIRECT_API", "true")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        ai.get_client(25)


def test_vertex_is_the_route_whenever_a_project_is_set(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "minty-expense-ai")
    monkeypatch.delenv("EXPENSE_AI_USE_VERTEX", raising=False)
    assert ai.uses_vertex() is True and ai.location() == "asia-southeast1"


def test_the_location_names_the_direct_route_and_its_tier(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv("EXPENSE_AI_DIRECT_TIER", "paid")
    assert ai.location() == "gemini-api-direct-paid"
    monkeypatch.setenv("EXPENSE_AI_DIRECT_TIER", "enterprise")
    assert ai.direct_tier() == "free"
    monkeypatch.delenv("EXPENSE_AI_DIRECT_TIER")
    assert ai.location() == "gemini-api-direct-free"


def test_a_free_tier_quota_error_while_configured_paid_is_reported_but_switches_nothing_off(
    monkeypatch, caplog
):
    compat = _compat()
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv("EXPENSE_AI_DIRECT_TIER", "paid")
    monkeypatch.setenv("TEST_AI_ENABLED", "true")
    free = _status_error(
        compat.RateLimitError, 429, "Quota exceeded for metric: generate_content_free_tier_requests"
    )
    assert ai._check_tier_matches_reality(free, CONFIG) is True
    assert "CONFIGURATION MISMATCH" in caplog.text
    assert CONFIG.enabled() is True
    paid = _status_error(compat.RateLimitError, 429, "Quota exceeded. Retry later.")
    assert ai._check_tier_matches_reality(paid, CONFIG) is False
    monkeypatch.setenv("EXPENSE_AI_DIRECT_TIER", "free")
    assert ai._check_tier_matches_reality(free, CONFIG) is False
