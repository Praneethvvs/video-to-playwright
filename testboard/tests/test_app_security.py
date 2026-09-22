"""The HTTP surface, exercised through the real middleware stack.

The unit tests cover the auth decisions. These cover the wiring — which is where the interesting
mistakes live: middleware ordering, which paths are exempt, whether a method nobody thought about
slips past, and whether the Referer can steer a redirect off-site.

Startup is deliberately not run. `create_app` is enough to mount the routes and middleware, and
running the lifespan would start the worker and collect tests in a temporary directory, which is
a different test with a much slower clock.
"""

import textwrap

import pytest
from starlette.testclient import TestClient

from testboard import auth
from testboard.app import create_app
from testboard.config import load

CONFIG = textwrap.dedent("""
    schema_version: 1
    repo:
      name: fixture
    runner:
      language: python
      framework: pytest
      python: venv
      test_paths: [tests]
    environment:
      base_url_env: FIXTURE_BASE_URL
""")


@pytest.fixture()
def repo(tmp_path):
    (tmp_path / "testboard.yaml").write_text(CONFIG, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    return tmp_path


@pytest.fixture()
def open_client(repo):
    """Loopback policy: no token required."""
    app = create_app(load(repo), auth.Policy("trusted", None, "127.0.0.1"))
    return TestClient(app)


@pytest.fixture()
def guarded_client(repo):
    """Token policy, as in a cluster."""
    app = create_app(load(repo), auth.Policy("token", "s" * 32, "0.0.0.0"))
    return TestClient(app)


# --- what a token protects ----------------------------------------------------------------
def test_pages_need_the_token(guarded_client):
    assert guarded_client.get("/").status_code == 401
    assert guarded_client.get("/tests").status_code == 401


def test_the_probe_and_the_stylesheet_do_not(guarded_client):
    """A Kubernetes probe must work without the secret, and the 401 page needs its stylesheet."""
    assert guarded_client.get("/healthz").status_code == 200
    assert guarded_client.get("/static/app.css").status_code == 200


def test_a_correct_token_is_accepted_everywhere_it_can_be_put(guarded_client):
    token = "s" * 32
    assert guarded_client.get("/", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert guarded_client.get("/", headers={"X-Testboard-Token": token}).status_code == 200
    assert guarded_client.get(f"/?token={token}").status_code == 200


def test_a_wrong_token_is_refused(guarded_client):
    assert guarded_client.get("/", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_a_non_ascii_token_is_refused_rather_than_crashing(guarded_client):
    """compare_digest raises TypeError on non-ASCII str; that used to surface as a 500.

    Sent as a query parameter, not a header: HTTP headers are latin-1, so a client cannot put a
    non-ASCII value in one — the first version of this test failed inside httpx before reaching
    the server at all. A percent-encoded query parameter is decoded to a str and *is* reachable,
    which makes it the case that actually mattered.
    """
    response = guarded_client.get("/?token=t%C3%B8ken")
    assert response.status_code == 401


@pytest.mark.parametrize("path", [
    "/run", "/tests/decide", "/tests/decide-all", "/inventory/refresh",
    "/jobs/check_drift", "/recordings/upload", "/api/runs/junit",
])
def test_every_mutating_route_needs_the_token(guarded_client, path):
    """Enumerated on purpose: a new write endpoint that forgets the gate should fail here."""
    assert guarded_client.post(path, data={}).status_code == 401


def test_a_writing_method_cannot_slip_past_on_a_readable_path(guarded_client):
    for method in ("post", "put", "patch", "delete"):
        response = getattr(guarded_client, method)("/tests")
        assert response.status_code in (401, 404, 405), f"{method} returned {response.status_code}"


@pytest.mark.parametrize("path", [
    "/static/../app.py",
    "/static/..%2fapp.py",
    "//healthz",
    "/healthz/../",
])
def test_the_exempt_paths_cannot_be_widened(guarded_client, path):
    """The exemptions are a prefix match, so traversal past them is worth asserting about."""
    response = guarded_client.get(path)
    assert response.status_code != 200 or "testboard" not in response.text.lower(), (
        f"{path} served content without a token"
    )


# --- cross-site writes --------------------------------------------------------------------
def test_a_cross_site_post_is_refused(open_client):
    """No token is involved: reachability is the thing being abused, not a credential."""
    response = open_client.post("/inventory/refresh",
                                headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403
    assert "another site" in response.text


def test_a_same_origin_post_is_allowed(open_client):
    response = open_client.post("/inventory/refresh",
                                headers={"Sec-Fetch-Site": "same-origin"},
                                follow_redirects=False)
    assert response.status_code == 303


def test_a_foreign_origin_is_refused_when_the_fetch_header_is_absent(open_client):
    response = open_client.post("/inventory/refresh",
                                headers={"Origin": "https://evil.example.com"})
    assert response.status_code == 403


def test_middleware_order_does_not_let_a_cross_site_write_through(guarded_client):
    """Whichever runs first, the answer must be a refusal — never a 2xx."""
    response = guarded_client.post("/inventory/refresh",
                                   headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code in (401, 403)


# --- redirects ----------------------------------------------------------------------------
@pytest.mark.parametrize("referer", [
    "https://evil.example.com/x",
    "//evil.example.com/x",
    "javascript:alert(1)",
])
def test_the_referer_cannot_steer_the_redirect_off_site(open_client, referer):
    response = open_client.post(
        "/inventory/refresh",
        headers={"Sec-Fetch-Site": "same-origin", "Referer": referer},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert "evil.example.com" not in location
    assert not location.lower().startswith("javascript:")


def test_a_same_origin_referer_is_honoured(open_client):
    response = open_client.post(
        "/inventory/refresh",
        headers={"Sec-Fetch-Site": "same-origin", "Referer": "http://testserver/tests?x=1"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/tests?x=1"


# --- the token must not leak ----------------------------------------------------------------
def test_the_token_is_never_echoed_in_the_refusal(guarded_client):
    response = guarded_client.get("/", headers={"Authorization": "Bearer wrong-but-distinctive"})
    assert "wrong-but-distinctive" not in response.text
    assert "s" * 32 not in response.text


def test_the_cookie_is_set_httponly_and_strict(guarded_client):
    token = "s" * 32
    response = guarded_client.get(f"/?token={token}")
    cookie = response.headers.get("set-cookie", "")
    assert "httponly" in cookie.lower()
    assert "samesite=strict" in cookie.lower().replace(" ", "")
