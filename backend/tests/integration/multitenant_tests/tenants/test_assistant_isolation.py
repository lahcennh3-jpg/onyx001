"""The assistant keeps workspace and user permissions on its real API routes."""

from http import HTTPStatus
from uuid import uuid4

import pytest

from ee.onyx.auth.users import generate_anonymous_user_jwt_token
from onyx.configs.constants import ANONYMOUS_USER_COOKIE_NAME
from tests.integration.common_utils.constants import API_SERVER_URL
from tests.integration.common_utils.http_client import client
from tests.integration.common_utils.managers.chat import ChatSessionManager
from tests.integration.common_utils.managers.user import UserManager
from tests.integration.common_utils.test_models import DATestUser


@pytest.fixture(scope="module")
def workspace_admins(reset_multitenant: None) -> tuple[DATestUser, DATestUser]:  # noqa: ARG001
    unique = uuid4().hex
    return (
        UserManager.create(name=f"workspace_a_{unique}"),
        UserManager.create(name=f"workspace_b_{unique}"),
    )


@pytest.mark.parametrize("shared", [False, True])
def test_workspace_admin_cannot_read_another_workspaces_chat(
    workspace_admins: tuple[DATestUser, DATestUser], shared: bool
) -> None:
    owner, other_admin = workspace_admins
    chat = ChatSessionManager.create(owner, description="Private workspace A chat")
    url = f"{API_SERVER_URL}/chat/get-chat-session/{chat.id}"

    own = client.get(url, headers=owner.headers)
    own.raise_for_status()
    assert own.json()["description"] == chat.description

    denied = client.get(url, headers=other_admin.headers, params={"is_shared": shared})
    assert denied.status_code == HTTPStatus.NOT_FOUND
    assert chat.description not in denied.text


def test_anonymous_cookie_does_not_move_a_signed_in_user(
    workspace_admins: tuple[DATestUser, DATestUser],
) -> None:
    owner, other_admin = workspace_admins
    other = UserManager.get_user_info(other_admin)
    assert other.team_name is not None
    anonymous = generate_anonymous_user_jwt_token(other.team_name)
    headers = {**owner.headers}
    headers["Cookie"] += f"; {ANONYMOUS_USER_COOKIE_NAME}={anonymous}"

    response = client.get(f"{API_SERVER_URL}/me", headers=headers)

    response.raise_for_status()
    assert response.json()["email"] == owner.email


def test_workspace_member_can_chat_without_admin_access(
    workspace_admins: tuple[DATestUser, DATestUser],
) -> None:
    admin, _ = workspace_admins
    email = f"workspace_member_{uuid4().hex}@example.com"
    UserManager.invite_user(email, admin)
    member = UserManager.create(email=email)
    assert not UserManager.is_admin(member)

    chat = ChatSessionManager.create(member, description="Member assistant chat")
    own = client.get(
        f"{API_SERVER_URL}/chat/get-chat-session/{chat.id}", headers=member.headers
    )
    own.raise_for_status()
    assert own.json()["description"] == chat.description

    private_chat = ChatSessionManager.create(admin, description="Private admin chat")
    private = client.get(
        f"{API_SERVER_URL}/chat/get-chat-session/{private_chat.id}",
        headers=member.headers,
    )
    assert private.status_code == HTTPStatus.FORBIDDEN
    assert private_chat.description not in private.text

    denied = client.get(f"{API_SERVER_URL}/manage/users", headers=member.headers)
    assert denied.status_code == HTTPStatus.FORBIDDEN


def test_unauthenticated_request_cannot_open_an_assistant_chat() -> None:
    client.cookies.clear()
    denied = client.post(
        f"{API_SERVER_URL}/chat/create-chat-session", json={"persona_id": 0}
    )
    assert denied.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}
