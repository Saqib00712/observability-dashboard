"""
Phase 10: access control for the read APIs.

Threat model (stated honestly): obsdash exposes request metadata, error
types, cost and token usage -- not prompts or user content, but still
operationally sensitive. Local single-user use needs nothing more than
the loopback default. Anything reachable by other people should set
API_TOKEN: a shared bearer secret, compared in constant time.

Not provided: users/roles, per-route scopes, token rotation, rate
limiting, TLS (put a reverse proxy in front for HTTPS). The token is
accepted ONLY in headers, never in the URL, because URLs end up in
access logs and browser history.
"""

import hmac

from fastapi import HTTPException, Request


def _supplied_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip() or None
    return request.headers.get("x-api-key") or None


def require_token(request: Request) -> None:
    """FastAPI dependency. A no-op when no token is configured."""
    expected: str = request.app.state.api_token
    if not expected:
        return
    supplied = _supplied_token(request)
    # compare_digest: constant-time, so response timing leaks nothing about
    # how much of a guess was right. Missing and wrong tokens look identical.
    if supplied is None or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="authentication required",
                            headers={"WWW-Authenticate": "Bearer"})
