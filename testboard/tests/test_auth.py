"""The access policy, which is the only thing between the network and a destructive button."""

import pytest

from testboard import auth


class CaseInsensitive(dict):
    """Starlette's Headers are case-insensitive; a plain dict is not.

    Modelling that matters: a test using a plain dict would pass while the production code read
    the wrong case, or fail while the production code was right. Both have happened here.
    """

    def get(self, key, default=None):
        lowered = key.lower()
        for name, value in self.items():
            if name.lower() == lowered:
                return value
        return default


class FakeRequest:
    def __init__(self, headers=None, cookies=None, query=None, client_host="127.0.0.1"):
        self.headers = CaseInsensitive(headers or {})
        self.cookies = cookies or {}
        self.query_params = query or {}
        self.client = type("C", (), {"host": client_host})()


def test_loopback_without_a_token_is_trusted(monkeypatch):
    monkeypatch.delenv("TESTBOARD_TOKEN", raising=False)
    policy = auth.policy_for("127.0.0.1")
    assert policy.mode == "trusted"
    assert not policy.requires_token


@pytest.mark.parametrize("host", ["0.0.0.0", "10.1.2.3", "testboard.svc.cluster.local"])
def test_a_public_bind_without_a_token_is_refused(monkeypatch, host):
    """The central safety property. A warning here would not be good enough."""
    monkeypatch.delenv("TESTBOARD_TOKEN", raising=False)
    with pytest.raises(auth.AuthError) as raised:
        auth.policy_for(host)
    assert host in str(raised.value)
    assert "TESTBOARD_TOKEN" in str(raised.value)


def test_a_short_token_is_refused(monkeypatch):
    monkeypatch.setenv("TESTBOARD_TOKEN", "short")
    with pytest.raises(auth.AuthError):
        auth.policy_for("0.0.0.0")


def test_a_token_enables_a_public_bind(monkeypatch):
    monkeypatch.setenv("TESTBOARD_TOKEN", "x" * 32)
    policy = auth.policy_for("0.0.0.0")
    assert policy.requires_token


def test_token_comparison(monkeypatch):
    monkeypatch.setenv("TESTBOARD_TOKEN", "correct-horse-battery-staple")
    policy = auth.policy_for("0.0.0.0")
    assert auth.token_ok(policy, "correct-horse-battery-staple")
    assert not auth.token_ok(policy, "correct-horse-battery-stapl")   # prefix must not pass
    assert not auth.token_ok(policy, "")
    assert not auth.token_ok(policy, None)
    # A non-ASCII presentation must be rejected, not raise: compare_digest refuses non-ASCII str.
    assert not auth.token_ok(policy, "tøken")


def test_a_trusted_policy_accepts_anything(monkeypatch):
    monkeypatch.delenv("TESTBOARD_TOKEN", raising=False)
    policy = auth.policy_for("127.0.0.1")
    assert auth.token_ok(policy, None)


@pytest.mark.parametrize("where,expected", [
    ({"headers": {"authorization": "Bearer abc"}}, "abc"),
    ({"headers": {"Authorization": "bearer abc"}}, "abc"),
    ({"headers": {"x-testboard-token": " abc "}}, "abc"),
    ({"cookies": {auth.COOKIE: "abc"}}, "abc"),
    ({"query": {"token": "abc"}}, "abc"),
    ({}, None),
])
def test_token_is_found_wherever_a_client_could_put_it(where, expected):
    assert auth.presented_token(FakeRequest(**where)) == expected


def test_identity_prefers_a_validated_proxy_header(monkeypatch):
    monkeypatch.setenv("TESTBOARD_TOKEN", "y" * 32)
    policy = auth.policy_for("0.0.0.0")
    request = FakeRequest(headers={"x-forwarded-user": "Jane Doe"})
    assert auth.identity(request, policy) == "Jane Doe"


@pytest.mark.parametrize("bad", [
    "<script>alert(1)</script>",
    "a" * 200,
    "has spaces",
    "semi;colon",
    "",
])
def test_an_implausible_proxy_header_is_not_recorded(monkeypatch, bad):
    """This value answers "who approved this test". Markup or 4 KB of it makes that useless."""
    monkeypatch.setenv("TESTBOARD_TOKEN", "y" * 32)
    policy = auth.policy_for("0.0.0.0")
    who = auth.identity(FakeRequest(headers={"x-forwarded-user": bad}), policy)
    assert who != bad
    assert who.startswith("token@")


def test_loopback_identity_is_not_the_word_you(monkeypatch):
    """"you" reads as a name in an audit trail and is not one."""
    monkeypatch.delenv("TESTBOARD_TOKEN", raising=False)
    policy = auth.policy_for("127.0.0.1")
    assert auth.identity(FakeRequest(), policy) == "local"
