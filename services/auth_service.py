import os
import secrets
from urllib.parse import urlencode

import httpx

from services.user_service import get_user, upsert_user

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

OAUTH_SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
]


def _redirect_uri():
    uri = os.getenv("OAUTH_REDIRECT_URI")
    if not uri:
        raise RuntimeError("OAUTH_REDIRECT_URI must be set in the environment.")
    return uri


def _client_credentials():
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set.")
    return client_id, client_secret


def build_authorize_url(state):
    client_id, _ = _client_credentials()
    params = {
        "client_id": client_id,
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": " ".join(OAUTH_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def new_state_token():
    return secrets.token_urlsafe(32)


def exchange_code_for_tokens(code):
    client_id, client_secret = _client_credentials()
    payload = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": _redirect_uri(),
        "grant_type": "authorization_code",
    }
    response = httpx.post(GOOGLE_TOKEN_URL, data=payload, timeout=20)
    response.raise_for_status()
    return response.json()


def fetch_userinfo(access_token):
    response = httpx.get(
        GOOGLE_USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def complete_oauth(code):
    """Run after Google redirects back with ?code=..."""
    tokens = exchange_code_for_tokens(code)
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise RuntimeError(
            "Google did not return a refresh_token. "
            "Revoke prior consent and sign in again so a fresh refresh_token is issued."
        )

    info = fetch_userinfo(access_token)
    google_sub = info.get("sub") or ""
    email = info.get("email") or ""
    name = info.get("name") or email.split("@")[0]
    picture_url = info.get("picture") or ""

    if not google_sub or not email:
        raise RuntimeError("Google userinfo missing required fields.")

    user = upsert_user(
        google_sub=google_sub,
        email=email,
        name=name,
        picture_url=picture_url,
        refresh_token=refresh_token,
    )
    return user


def get_user_from_session(session):
    user_id = session.get("user_id") if session else None
    if not user_id:
        return None
    return get_user(user_id)
