"""
JWT authentication — login and token management.
"""

import json
import time
from base64 import urlsafe_b64decode

import requests

from config import LOGIN_URL, BASE_HEADERS, CREDENTIALS, TOKEN_EXPIRY_BUFFER

_token_cache: str | None = None


def _decode_jwt_payload(token: str) -> dict:
    """Decode the payload of a JWT without verifying signature."""
    try:
        payload_b64 = token.split(".")[1]
        # Add padding for base64url decode
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload_bytes = urlsafe_b64decode(payload_b64)
        return json.loads(payload_bytes)
    except Exception:
        return {}


def _token_expired(token: str) -> bool:
    """Check if a JWT token is expired (or about to expire within buffer)."""
    payload = _decode_jwt_payload(token)
    exp = payload.get("exp", 0)
    return time.time() + TOKEN_EXPIRY_BUFFER >= exp


def login() -> str:
    """Log into the API and return a JWT token."""
    resp = requests.post(
        LOGIN_URL,
        json=CREDENTIALS,
        headers=BASE_HEADERS,
        timeout=10,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 0:
        raise RuntimeError(f"Login failed: {body.get('message', 'unknown error')}")
    return body["data"]["token"]


def get_token() -> str:
    """Return a valid token, re-logging in if necessary."""
    global _token_cache
    if _token_cache is None or _token_expired(_token_cache):
        _token_cache = login()
    return _token_cache


def get_auth_headers() -> dict:
    """Return headers for an authenticated API request."""
    return {
        **BASE_HEADERS,
        "Authorization": get_token(),
    }
