"""Tests for the api-server tenant-resolution middleware.

Focus: tenant resolution must **fail closed**. A transient Redis error during
the cookie- or Bearer-header session-token lookup must propagate (500), not get
swallowed into a silent default-schema fallback — otherwise a request could be
served against the wrong tenant. (Contrast license enforcement, which fails
*open*; tenant routing is a data-isolation boundary and must not.)
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jwt import InvalidTokenError
from starlette.requests import Request
from starlette.types import Message, Receive, Scope, Send

from ee.onyx.server.middleware import tenant_tracking
from onyx.configs.constants import ANONYMOUS_USER_COOKIE_NAME
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
from onyx.server.middleware import api_prefix
from shared_configs.configs import POSTGRES_DEFAULT_SCHEMA
from shared_configs.contextvars import (
    CURRENT_TENANT_ID_CONTEXTVAR,
    get_current_tenant_id,
)

MODULE = "ee.onyx.server.middleware.tenant_tracking"


def _request(headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "path": "/me", "headers": raw})


def test_custom_api_prefix_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_prefix, "APP_API_PREFIX", "v2")
    assert tenant_tracking._is_path_allowed("/v2/auth/login") is True
    assert tenant_tracking._is_path_allowed("/v2/chat") is False


@pytest.mark.parametrize("path", ["/health", "/health/ready", "/metrics"])
def test_probe_paths_skip_tenant_resolution_under_api_prefix(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Probes must never reach the Redis session lookup, prefixed or not."""
    monkeypatch.setattr(api_prefix, "APP_API_PREFIX", "v2")
    stripped = api_prefix.strip_api_prefix(f"/v2{path}")
    assert stripped in tenant_tracking.TENANT_RESOLUTION_SKIP_PATHS


@pytest.mark.asyncio
@patch(f"{MODULE}.retrieve_auth_token_data_from_bearer")
@patch(f"{MODULE}.retrieve_auth_token_data_from_redis")
async def test_bearer_redis_error_propagates_not_default(
    mock_cookie: AsyncMock,
    mock_bearer: AsyncMock,
) -> None:
    """A Redis failure on the Bearer lookup must surface as 500, not fall back to
    the default schema (the bug the `finally` block used to mask)."""
    mock_cookie.return_value = None
    mock_bearer.side_effect = ValueError("redis down")

    req = _request({"Authorization": "Bearer opaque_session_token"})
    with pytest.raises(OnyxError) as exc:
        await tenant_tracking._get_tenant_id_from_request(req, MagicMock())
    assert exc.value.error_code == OnyxErrorCode.INTERNAL_ERROR


@pytest.mark.asyncio
@patch(f"{MODULE}.retrieve_auth_token_data_from_bearer")
@patch(f"{MODULE}.retrieve_auth_token_data_from_redis")
async def test_cookie_redis_error_propagates_not_default(
    mock_cookie: AsyncMock,
    mock_bearer: AsyncMock,
) -> None:
    """Same fail-closed guarantee for the pre-existing cookie path."""
    mock_cookie.side_effect = ValueError("redis down")

    req = _request()
    with pytest.raises(OnyxError) as exc:
        await tenant_tracking._get_tenant_id_from_request(req, MagicMock())
    assert exc.value.error_code == OnyxErrorCode.INTERNAL_ERROR
    mock_bearer.assert_not_called()


@pytest.mark.asyncio
@patch(f"{MODULE}.retrieve_auth_token_data_from_bearer")
@patch(f"{MODULE}.retrieve_auth_token_data_from_redis")
async def test_bearer_resolves_tenant(
    mock_cookie: AsyncMock,
    mock_bearer: AsyncMock,
) -> None:
    """Happy path: a mobile Bearer token resolves its tenant from Redis."""
    mock_cookie.return_value = None
    mock_bearer.return_value = {"tenant_id": "tenant_abc123"}

    req = _request({"Authorization": "Bearer opaque_session_token"})
    tenant_id = await tenant_tracking._get_tenant_id_from_request(req, MagicMock())
    assert tenant_id == "tenant_abc123"


@pytest.mark.asyncio
@patch(f"{MODULE}.retrieve_auth_token_data_from_bearer")
@patch(f"{MODULE}.retrieve_auth_token_data_from_redis")
async def test_no_auth_returns_default_schema(
    mock_cookie: AsyncMock,
    mock_bearer: AsyncMock,
) -> None:
    """Requests with no credential use the schema that has no workspace data."""
    mock_cookie.return_value = None
    mock_bearer.return_value = None

    req = _request()
    tenant_id = await tenant_tracking._get_tenant_id_from_request(req, MagicMock())
    assert tenant_id == POSTGRES_DEFAULT_SCHEMA


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["cookie", "bearer"])
async def test_authenticated_session_wins_over_anonymous_cookie(transport: str) -> None:
    session = {"tenant_id": "tenant_signed_in"}
    with (
        patch(
            f"{MODULE}.retrieve_auth_token_data_from_redis", new_callable=AsyncMock
        ) as cookie,
        patch(
            f"{MODULE}.retrieve_auth_token_data_from_bearer", new_callable=AsyncMock
        ) as bearer,
        patch(f"{MODULE}.decode_anonymous_user_jwt_token") as anonymous,
    ):
        cookie.return_value = session if transport == "cookie" else None
        bearer.return_value = session
        anonymous.return_value = {"tenant_id": "tenant_anonymous"}
        request = _request({"cookie": f"{ANONYMOUS_USER_COOKIE_NAME}=stale-cookie"})

        tenant = await tenant_tracking._get_tenant_id_from_request(request, MagicMock())

    assert tenant == "tenant_signed_in"
    anonymous.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "tenant"),
    [
        (source, tenant)
        for source in ("session", "anonymous", "api_key", "oauth")
        for tenant in (
            None,
            "",
            POSTGRES_DEFAULT_SCHEMA,
            "bad;schema",
            123,
            ["tenant_a"],
        )
        if tenant is not None or source in {"session", "anonymous"}
    ],
)
async def test_invalid_credential_tenant_fails_closed(
    source: str, tenant: object
) -> None:
    with (
        patch(
            f"{MODULE}.oauth_provider_tenant_from_request", new_callable=AsyncMock
        ) as oauth,
        patch(f"{MODULE}.extract_tenant_from_auth_header") as header,
        patch(
            f"{MODULE}.retrieve_auth_token_data_from_redis", new_callable=AsyncMock
        ) as cookie,
        patch(
            f"{MODULE}.retrieve_auth_token_data_from_bearer", new_callable=AsyncMock
        ) as bearer,
        patch(f"{MODULE}.decode_anonymous_user_jwt_token") as anonymous,
    ):
        oauth.return_value = tenant if source == "oauth" else None
        header.return_value = tenant if source == "api_key" else None
        cookie.return_value = {"tenant_id": tenant} if source == "session" else None
        bearer.return_value = None
        anonymous.return_value = {"tenant_id": tenant}
        request = _request({"cookie": f"{ANONYMOUS_USER_COOKIE_NAME}=signed-cookie"})

        with pytest.raises(OnyxError) as exc:
            await tenant_tracking._get_tenant_id_from_request(request, MagicMock())

    assert exc.value.error_code == OnyxErrorCode.INVALID_TOKEN


@pytest.mark.asyncio
async def test_anonymous_token_is_used_only_without_a_session() -> None:
    with (
        patch(
            f"{MODULE}.retrieve_auth_token_data_from_redis", new_callable=AsyncMock
        ) as cookie,
        patch(
            f"{MODULE}.retrieve_auth_token_data_from_bearer", new_callable=AsyncMock
        ) as bearer,
        patch(f"{MODULE}.decode_anonymous_user_jwt_token") as anonymous,
    ):
        cookie.return_value = bearer.return_value = None
        anonymous.return_value = {"tenant_id": "tenant_anonymous"}
        request = _request({"cookie": f"{ANONYMOUS_USER_COOKIE_NAME}=signed-cookie"})

        assert (
            await tenant_tracking._get_tenant_id_from_request(request, MagicMock())
            == "tenant_anonymous"
        )


@pytest.mark.asyncio
async def test_invalid_anonymous_signature_cannot_select_a_tenant() -> None:
    with (
        patch(
            f"{MODULE}.retrieve_auth_token_data_from_redis", new_callable=AsyncMock
        ) as cookie,
        patch(
            f"{MODULE}.retrieve_auth_token_data_from_bearer", new_callable=AsyncMock
        ) as bearer,
        patch(
            f"{MODULE}.decode_anonymous_user_jwt_token", side_effect=InvalidTokenError
        ),
    ):
        cookie.return_value = bearer.return_value = None
        request = _request({"cookie": f"{ANONYMOUS_USER_COOKIE_NAME}=forged-cookie"})

        assert (
            await tenant_tracking._get_tenant_id_from_request(request, MagicMock())
            == POSTGRES_DEFAULT_SCHEMA
        )


@pytest.mark.asyncio
async def test_concurrent_streams_keep_their_tenant_until_completion() -> None:
    async def app(scope: Scope, _receive: Receive, send: Send) -> None:
        tenant = Request(scope).headers["x-test-tenant"]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await asyncio.sleep(0)
        assert get_current_tenant_id() == tenant
        assert await asyncio.to_thread(get_current_tenant_id) == tenant
        await send(
            {"type": "http.response.body", "body": tenant.encode(), "more_body": True}
        )
        await asyncio.sleep(0)
        assert get_current_tenant_id() == tenant
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def resolve(request: Request, _logger: object) -> str:
        await asyncio.sleep(0)
        return request.headers["x-test-tenant"]

    middleware = tenant_tracking.tenant_id_middleware(app, MagicMock())

    async def run(tenant: str) -> None:
        request = _request({"x-test-tenant": tenant})
        messages: list[Message] = []

        async def send(message: Message) -> None:
            assert get_current_tenant_id() == tenant
            messages.append(message)

        await middleware(request.scope, AsyncMock(), send)
        assert get_current_tenant_id() == "tenant_outer"
        assert (
            b"".join(message.get("body", b"") for message in messages)
            == tenant.encode()
        )

    context = CURRENT_TENANT_ID_CONTEXTVAR.set("tenant_outer")
    try:
        with (
            patch(f"{MODULE}.MULTI_TENANT", True),
            patch(f"{MODULE}._get_tenant_id_from_request", side_effect=resolve),
            patch(f"{MODULE}.is_tenant_gated", return_value=False),
        ):
            await asyncio.gather(run("tenant_a"), run("tenant_b"))
    finally:
        CURRENT_TENANT_ID_CONTEXTVAR.reset(context)


@pytest.mark.asyncio
async def test_failed_tenant_lookup_never_calls_the_app_or_leaks_context() -> None:
    app = AsyncMock()
    send = AsyncMock()
    middleware = tenant_tracking.tenant_id_middleware(app, MagicMock())
    context = CURRENT_TENANT_ID_CONTEXTVAR.set("tenant_outer")
    try:
        with (
            patch(f"{MODULE}.MULTI_TENANT", True),
            patch(
                f"{MODULE}._get_tenant_id_from_request",
                side_effect=OnyxError(OnyxErrorCode.INVALID_TOKEN),
            ),
        ):
            await middleware(_request().scope, AsyncMock(), send)
        assert get_current_tenant_id() == "tenant_outer"
    finally:
        CURRENT_TENANT_ID_CONTEXTVAR.reset(context)

    app.assert_not_called()
    assert send.call_args_list[0].args[0]["status"] == 401


@pytest.mark.asyncio
@patch(f"{MODULE}.retrieve_auth_token_data_from_bearer")
@patch(f"{MODULE}.retrieve_auth_token_data_from_redis")
async def test_client_supplied_cookie_cannot_select_a_workspace(
    mock_cookie: AsyncMock,
    mock_bearer: AsyncMock,
) -> None:
    """Every accepted source is signed or server-issued. A bare cookie naming a
    schema is attacker input, and honoring it would hand an unauthenticated
    caller that workspace on any route that reads tenant-scoped data."""
    mock_cookie.return_value = None
    mock_bearer.return_value = None

    req = _request({"cookie": "onyx_tid=tenant_victim"})
    tenant_id = await tenant_tracking._get_tenant_id_from_request(req, MagicMock())
    assert tenant_id == POSTGRES_DEFAULT_SCHEMA
