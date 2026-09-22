"""Who is allowed to use this, and who they are.

testboard can start runs that mutate a shared environment and can read every trace it has ever
captured. On a laptop, binding to loopback is a complete answer: only someone already on the
machine can reach it. In a cluster it is no answer at all, and "we will put something in front of
it" is the sort of intention that does not survive a deadline.

So there are three modes, and the one in force is decided by where it is listening rather than by
a setting somebody might forget:

| | |
|---|---|
| `trusted` | bound to loopback with no token set. Everyone is "local" |
| `token`   | a shared token is configured. Every request must present it |
| `proxy`   | an authenticating proxy in front sets a user header, and the token authorises the proxy itself |

The rule that matters: **listening on a non-loopback address without a token is refused at
startup.** Not warned about — refused. A warning printed at 2am into a log nobody reads is how an
unauthenticated write endpoint ends up reachable from a cluster network.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
from dataclasses import dataclass

log = logging.getLogger("testboard.auth")

TOKEN_VARS = ("TESTBOARD_TOKEN",)
COOKIE = "testboard_token"
USER_HEADERS = ("x-forwarded-user", "x-remote-user", "x-auth-request-user")
# Deliberately strict. This value lands in an audit field that answers "who approved this test".
USER_OK = re.compile(r"^[\w.@+-]{1,64}$")

LOOPBACK = ("127.0.0.1", "localhost", "::1")


class AuthError(Exception):
    """Raised at startup for a configuration that cannot be made safe."""


@dataclass(frozen=True)
class Policy:
    mode: str                 # trusted | token
    token: str | None
    host: str

    @property
    def requires_token(self) -> bool:
        return self.mode == "token"

    def describe(self) -> str:
        if self.mode == "trusted":
            return f"loopback only ({self.host}), no token — anyone on this machine can use it"
        return f"token required on every request; listening on {self.host}"


def _token() -> str | None:
    for var in TOKEN_VARS:
        value = (os.environ.get(var) or "").strip()
        if value:
            return value
    return None


def policy_for(host: str) -> Policy:
    """Decide the policy, and refuse a combination that cannot be made safe.

    Called before the server binds, so an unsafe deployment fails to start rather than running
    and hoping.
    """
    token = _token()
    loopback = host in LOOPBACK

    if token:
        if len(token) < 16:
            raise AuthError(
                f"{TOKEN_VARS[0]} is {len(token)} characters. Use at least 16 — this is the only "
                f"thing standing between the network and a button that mutates your dev data."
            )
        return Policy("token", token, host)

    if not loopback:
        raise AuthError(
            f"Refusing to listen on {host} without a token.\n\n"
            f"testboard has no login of its own, and it can start runs that change data in a "
            f"shared environment. On loopback that is fine because only this machine can reach "
            f"it. On {host} it is not.\n\n"
            f"Either bind to 127.0.0.1, or set a token:\n"
            f"  {TOKEN_VARS[0]}=$(python -c \"import secrets;print(secrets.token_urlsafe(32))\")\n\n"
            f"In Kubernetes that belongs in the same Secret as CODEX_API_KEY."
        )

    return Policy("trusted", None, host)


def presented_token(request) -> str | None:
    """The token from wherever this client could have put it.

    A header for scripts and the pipeline; a cookie so a browser does not have to re-send it on
    every navigation; a query parameter once, so the first link someone is given can carry it.
    """
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    direct = request.headers.get("x-testboard-token")
    if direct:
        return direct.strip()
    cookie = request.cookies.get(COOKIE)
    if cookie:
        return cookie.strip()
    return request.query_params.get("token")


def token_ok(policy: Policy, presented: str | None) -> bool:
    if not policy.requires_token:
        return True
    if not presented or not policy.token:
        return False
    # compare_digest, because a plain == leaks the length of the shared prefix through timing,
    # and this is the only secret the application has.
    #
    # Compared as **bytes**. Given str it accepts ASCII only and raises TypeError otherwise, so a
    # client presenting a token with any non-ASCII character in it crashed the middleware and got
    # a 500 where it should have got a 401 — a trivially reachable error path that also says more
    # about the server than a refusal should.
    try:
        return hmac.compare_digest(presented.encode("utf-8"), policy.token.encode("utf-8"))
    except (AttributeError, UnicodeError):
        return False


def identity(request, policy: Policy) -> str:
    """Who to record against a run or an approval.

    With a proxy in front, the header it sets is the real answer. Without one, a token holder is
    indistinguishable from any other token holder, and saying so is more honest than inventing a
    name: "token" is a worse audit entry than a username and a better one than a guess.
    """
    for name in USER_HEADERS:
        value = (request.headers.get(name) or "").strip()
        if value and USER_OK.match(value):
            return value
        if value:
            log.warning("ignoring an implausible %s header: %r", name, value[:80])

    client = request.client.host if request.client else "unknown"
    if policy.mode == "trusted":
        return "local"
    return f"token@{client}"
