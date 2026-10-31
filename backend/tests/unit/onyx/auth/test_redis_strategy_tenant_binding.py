"""Session identities must stay in the workspace that issued the session."""

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from onyx.auth.users import TenantAwareRedisStrategy
from shared_configs.contextvars import CURRENT_TENANT_ID_CONTEXTVAR

MODULE = "onyx.auth.users"


@pytest.mark.asyncio
@pytest.mark.parametrize("session_tenant", ["tenant_other", None, "", "public"])
async def test_wrong_workspace_is_rejected_before_user_lookup(
    session_tenant: str | None,
) -> None:
    redis = AsyncMock()
    redis.get.return_value = json.dumps(
        {"sub": str(uuid4()), "tenant_id": session_tenant}
    )
    manager = MagicMock()
    manager.get = AsyncMock()
    context = CURRENT_TENANT_ID_CONTEXTVAR.set("tenant_current")
    try:
        with (
            patch(f"{MODULE}.MULTI_TENANT", True),
            patch(f"{MODULE}.get_async_redis_connection", return_value=redis),
            patch(f"{MODULE}.record_session_rejection") as rejected,
        ):
            user = await TenantAwareRedisStrategy().read_token("session-token", manager)
    finally:
        CURRENT_TENANT_ID_CONTEXTVAR.reset(context)

    assert user is None
    rejected.assert_called_once()
    manager.parse_id.assert_not_called()
    manager.get.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("multitenant", [True, False])
async def test_session_in_its_workspace_authenticates(multitenant: bool) -> None:
    user_id = uuid4()
    redis = AsyncMock()
    redis.get.return_value = f'{{"sub": "{user_id}", "tenant_id": "tenant_current"}}'
    manager = MagicMock()
    manager.parse_id.return_value = user_id
    manager.get = AsyncMock()
    context = CURRENT_TENANT_ID_CONTEXTVAR.set("tenant_current")
    try:
        with (
            patch(f"{MODULE}.MULTI_TENANT", multitenant),
            patch(f"{MODULE}.get_async_redis_connection", return_value=redis),
        ):
            user = await TenantAwareRedisStrategy().read_token("session-token", manager)
    finally:
        CURRENT_TENANT_ID_CONTEXTVAR.reset(context)

    assert user is manager.get.return_value
    manager.get.assert_awaited_once_with(user_id)


@pytest.mark.asyncio
async def test_single_tenant_legacy_session_still_authenticates() -> None:
    user_id = uuid4()
    redis = AsyncMock()
    redis.get.return_value = f'{{"sub": "{user_id}"}}'
    manager = MagicMock()
    manager.parse_id.return_value = user_id
    manager.get = AsyncMock()
    with (
        patch(f"{MODULE}.MULTI_TENANT", False),
        patch(f"{MODULE}.get_async_redis_connection", return_value=redis),
    ):
        user = await TenantAwareRedisStrategy().read_token("legacy-token", manager)

    assert user is manager.get.return_value
