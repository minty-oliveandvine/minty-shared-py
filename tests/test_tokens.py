"""minty_shared.tokens: the bearer decode (60 s skew everywhere) and the service assertion."""

import time
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from minty_shared import logctx, tokens

SECRET = "test-secret-key-shared-with-flask-and-long-enough"


@pytest.fixture(autouse=True)
def _clean():
    logctx.reset()
    yield
    logctx.reset()


def _mint(**claims):
    return jwt.encode(claims, SECRET, algorithm="HS256")


def test_a_good_bearer_decodes():
    now = int(time.time())
    assert (
        tokens.decode_bearer(_mint(user_id="u1", iat=now, exp=now + 60), SECRET)["user_id"] == "u1"
    )


def test_a_token_minted_slightly_in_the_future_is_accepted():
    # Flask's clock 30 s ahead of this host: within the skew, so not "not yet valid".
    now = int(time.time())
    claims = tokens.decode_bearer(
        _mint(user_id="u1", iat=now + 30, nbf=now + 30, exp=now + 600), SECRET
    )
    assert claims["user_id"] == "u1"


def test_a_token_expired_within_the_skew_still_decodes_and_beyond_it_does_not():
    now = int(time.time())
    assert tokens.decode_bearer(_mint(exp=now - 30), SECRET) == {"exp": now - 30}
    with pytest.raises(jwt.ExpiredSignatureError):
        tokens.decode_bearer(_mint(exp=now - 120), SECRET)


def test_a_forged_token_is_refused():
    forged = jwt.encode(
        {"user_id": "u1"}, "not-the-key-not-the-key-not-the-key!!", algorithm="HS256"
    )
    with pytest.raises(jwt.InvalidSignatureError):
        tokens.decode_bearer(forged, SECRET)


def test_another_algorithm_is_refused():
    unsigned = jwt.encode({"user_id": "u1"}, None, algorithm="none")
    with pytest.raises(jwt.InvalidAlgorithmError):
        tokens.decode_bearer(unsigned, SECRET)


def test_required_claims_pass_through():
    with pytest.raises(jwt.MissingRequiredClaimError):
        tokens.decode_bearer(_mint(user_id="u1"), SECRET, options={"require": ["exp"]})


def test_a_service_assertion_is_scoped_short_lived_and_names_the_company():
    before = datetime.now(UTC)
    token = tokens.service_assertion(SECRET, "xero-access-token", 42, user_id="u9")
    claims = jwt.decode(token, SECRET, algorithms=["HS256"])
    assert (claims["scope"], claims["entity_id"], claims["user_id"]) == (
        "xero-access-token",
        "42",
        "u9",
    )
    expires = datetime.fromtimestamp(claims["exp"], UTC)
    assert timedelta(seconds=55) <= expires - before <= timedelta(seconds=62)


def test_assertion_headers_carry_the_trace():
    assert tokens.assertion_headers("t") == {"Authorization": "Bearer t"}
    logctx.start_trace("a1b2c3d4")
    assert tokens.assertion_headers("t") == {
        "Authorization": "Bearer t",
        "X-Request-ID": "a1b2c3d4",
    }
