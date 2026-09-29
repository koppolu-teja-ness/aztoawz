"""Authentication and authorization stubs for capstone APIs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
import os

from fastapi import Header, HTTPException, status


@dataclass(frozen=True)
class ApiPrincipal:
    """Authenticated caller identity resolved by API auth stub."""

    subject: str
    auth_mode: str
    scopes: tuple[str, ...]
    authenticated_at: datetime


def require_auth(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    x_reviewer_id: str | None = Header(default=None),
) -> ApiPrincipal:
    """Resolve caller identity via API key or OAuth-style bearer token stub."""

    mode = os.getenv("API_AUTH_MODE", "api_key").strip().lower()
    now = datetime.now(tz=timezone.utc)

    if mode == "api_key":
        expected = os.getenv("API_KEY", "capstone-api-key")
        if not x_api_key or not hmac.compare_digest(x_api_key, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication failed",
            )
        subject = (x_reviewer_id or "api-key-reviewer").strip() or "api-key-reviewer"
        return ApiPrincipal(
            subject=subject,
            auth_mode="api_key",
            scopes=("migration:read", "migration:write", "migration:approve"),
            authenticated_at=now,
        )

    if mode == "oauth_stub":
        if not authorization:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication failed",
            )

        token = _extract_bearer_token(authorization)
        subject, scopes = _parse_oauth_stub_token(token)
        return ApiPrincipal(
            subject=subject,
            auth_mode="oauth_stub",
            scopes=scopes,
            authenticated_at=now,
        )

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Server authentication configuration is invalid",
    )


def require_scope(principal: ApiPrincipal, required_scope: str) -> None:
    """Enforce simple scope membership for authorization checks."""

    if required_scope in principal.scopes:
        return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Not authorized for requested action",
    )


def _extract_bearer_token(authorization: str) -> str:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed",
        )
    return token.strip()


def _parse_oauth_stub_token(token: str) -> tuple[str, tuple[str, ...]]:
    if token.startswith("stub:"):
        _, _, payload = token.partition("stub:")
        subject, _, scope_blob = payload.partition("|")
        scopes = tuple(
            item.strip() for item in scope_blob.split(",") if item.strip()
        ) or ("migration:read",)
        normalized_subject = subject.strip() or "oauth-user"
        return normalized_subject, scopes

    # Backward-compatible fallback for simple bearer values in capstone demos.
    return "oauth-user", ("migration:read", "migration:write", "migration:approve")
