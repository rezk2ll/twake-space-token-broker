"""The Space step of the consent, where the owner pastes the API token their agent reaches Twake
Space with, and the forward-auth that hands it to Space's contracts."""

import html
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from httpx import URL, AsyncClient, Response

from tests.conftest import (
    ALICE,
    MMAUDET,
    PUBLIC_BASE_URL,
    SPACE_URL,
    SPACE_WEB_URL,
    FakeClock,
    as_agent_of,
    consent,
    consent_with_drive,
    database_dump,
    remove_delegation,
    running,
)
from tests.fake_cozy_stack import FakeCozyStack
from tests.fake_lemonldap import FakeLemonLDAP
from tests.fake_space import FakeSpace, Outage, Role
from twake_space_token_broker.consent import COOKIE
from twake_space_token_broker.keys import Signer
from twake_space_token_broker.settings import Settings

SPACE = {"token": "space"}
"""The query of the forward-auth of a Space route."""

EVERY_SCOPE = ("space:read", "feed:read", "members:write", "space:write")
"""What the token the Space step asks for allows."""

SPACE_TOKEN_MISSING = {
    "type": "urn:twake:problem:space_token_missing",
    "title": "Space token missing",
    "status": 401,
    "detail": "The user has given their agent no Twake Space token: they must paste one through"
    " the consent link.",
    "code": "space_token_missing",
    "consent_url": f"{PUBLIC_BASE_URL}/consent?owner=mmaudet%40example.test&app=space",
}
"""The problem of a Space route whose owner has given their agent no Space token."""

PASTED = "Dans vos espaces Twake Space, il agit avec le jeton que vous avez collé."
SKIPPED = "Sans jeton d'API, il n'agit pas dans vos espaces Twake Space."
KEPT = "Dans vos espaces Twake Space, il garde le jeton d'API que vous aviez collé."
REMOVED = (
    "Il n'agit plus dans vos espaces Twake Space : le jeton d'API que vous aviez collé est retiré."
    " Révoquez-le aussi sur la page « Jetons d'API » de Twake Space."
)
NOT_KEPT = "Ce jeton n'a pas été enregistré :"
NOT_A_TOKEN = f"{NOT_KEPT} ce n'est pas un jeton d'API de Twake Space, qui commence par tws_."
NOT_IN_PROGRESS = "Cette page ne correspond pas à l'autorisation en cours."
TOO_BROAD = (
    f"{NOT_KEPT} il permet « Gérer les jetons » : s'il fuyait, il servirait à en créer d'autres."
    " Créez-en un autre, avec Jetons d'API « Aucun accès »."
)
NOT_THEIRS = (
    f"{NOT_KEPT} c'est un jeton d'organisation ou celui d'un autre compte : dans au moins un des"
    " espaces qu'il atteint, il n'agit pas en votre nom. Créez le vôtre dans Twake Space, connecté"
    f" en tant que {MMAUDET}."
)
DRIVE_UNCHANGED = (
    f"Votre assistant Twake Space peut toujours agir pour {MMAUDET}. Son accès à Drive ne change"
    " pas."
)


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """The broker reaches Twake Space, as on dev."""
    return replace(settings, space_url=SPACE_URL, space_web_url=SPACE_WEB_URL)


def bearer(authorization: str) -> str:
    scheme, _, token = authorization.partition(" ")
    assert scheme == "Bearer"
    return token


def text_of(page: Response) -> str:
    """The page as the browser shows it."""
    return html.unescape(page.text)


def consent_warning(caplog: pytest.LogCaptureFixture) -> str:
    """The one warning the consent logged."""
    [warning] = [
        message
        for name, level, message in caplog.record_tuples
        if name == "twake_space_token_broker.consent" and level == logging.WARNING
    ]
    return warning


def state_of(step: Response) -> str:
    """The state the form of the Space step posts back."""
    found = re.search(r'name="state" value="([^"]*)"', step.text)
    assert found is not None
    return html.unescape(found.group(1))


async def paste(client: AsyncClient, step: Response, token: str) -> Response:
    """The user pastes the token on the Space step, then saves it."""
    return await client.post("/consent/space", data={"state": state_of(step), "space_token": token})


async def skip(client: AsyncClient, step: Response) -> Response:
    """The user skips the Space step."""
    return await client.post("/consent/space", data={"state": state_of(step), "skip": "yes"})


async def remove(client: AsyncClient, step: Response) -> Response:
    """The user removes the Space token kept, from the Space step."""
    return await client.post("/consent/space", data={"state": state_of(step), "remove": "yes"})


async def space_route(client: AsyncClient, user: str) -> Response:
    """The forward-auth APISIX calls for a Space route of the user's agent."""
    return await client.get("/forward-auth", params=SPACE, headers=as_agent_of(user))


async def space_link(
    client: AsyncClient, lemonldap: FakeLemonLDAP, owner: str, *, signed_in_as: str | None = None
) -> Response:
    """The owner opens the consent link for Space that their agent gives, in their own browser,
    and signs in."""
    client.cookies.clear()
    started = await client.get("/consent", params={"owner": owner, "app": "space"})
    return await client.get(lemonldap.sign_in(started.headers["location"], signed_in_as or owner))


async def test_a_consent_lemonldap_granted_stops_on_the_space_step(
    client: AsyncClient, lemonldap: FakeLemonLDAP
) -> None:
    response = await consent(client, lemonldap, MMAUDET)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'action="/consent/space"' in response.text
    # The token to create, in the words of Space's page of API tokens
    assert f'href="{SPACE_WEB_URL}/settings/api-tokens"' in response.text
    assert "« Assistant »" in response.text
    assert "Tous mes espaces, y compris ceux que je rejoindrai" in response.text
    assert "« 90 jours »" in response.text
    assert "« Lire les espaces » et « Lire les fils »" in response.text
    assert "Passer cette étape" in response.text


async def test_the_agent_of_an_owner_who_pasted_a_space_token_gets_it_with_lemonldaps(
    client: AsyncClient, lemonldap: FakeLemonLDAP, space: FakeSpace
) -> None:
    token = space.create_token(*EVERY_SCOPE)
    step = await consent(client, lemonldap, MMAUDET)

    pasted = await paste(client, step, token)

    assert pasted.status_code == 200
    assert "<title>Votre assistant est autorisé - Twake Space</title>" in pasted.text
    assert (
        f"{PASTED} Ce jeton permet : « Lire les espaces », « Lire les fils », « Gérer les"
        " membres » et « Modifier les espaces »."
    ) in text_of(pasted)
    response = await space_route(client, MMAUDET)
    assert response.status_code == 200
    # LemonLDAP's token still names the owner to the contract
    assert lemonldap.owner_of(bearer(response.headers["authorization"])) == MMAUDET
    assert response.headers["x-twake-space-token"] == token


async def test_checking_a_space_token_changes_nothing_in_twake_space(
    client: AsyncClient, lemonldap: FakeLemonLDAP, space: FakeSpace
) -> None:
    step = await consent(client, lemonldap, MMAUDET)

    await paste(client, step, space.create_token(*EVERY_SCOPE))

    assert space.requests != []
    assert space.writes == []


async def test_a_space_token_without_the_scopes_the_agent_can_do_without_is_kept(
    client: AsyncClient, lemonldap: FakeLemonLDAP, space: FakeSpace
) -> None:
    token = space.create_token("space:read", "feed:read")
    step = await consent(client, lemonldap, MMAUDET)

    pasted = await paste(client, step, token)

    assert pasted.status_code == 200
    assert (
        "Ce jeton permet : « Lire les espaces » et « Lire les fils »."
        " Il ne permet pas : « Gérer les membres » et « Modifier les espaces »."
    ) in text_of(pasted)
    assert (await space_route(client, MMAUDET)).headers["x-twake-space-token"] == token


async def test_a_space_token_pasted_with_blanks_around_it_is_kept_without_them(
    client: AsyncClient, lemonldap: FakeLemonLDAP, space: FakeSpace
) -> None:
    token = space.create_token(*EVERY_SCOPE)
    step = await consent(client, lemonldap, MMAUDET)

    pasted = await paste(client, step, f"  {token}\n")

    assert pasted.status_code == 200
    assert (await space_route(client, MMAUDET)).headers["x-twake-space-token"] == token


async def test_an_owner_who_skipped_the_space_step_gets_the_consent_link_for_space(
    client: AsyncClient, lemonldap: FakeLemonLDAP
) -> None:
    step = await consent(client, lemonldap, MMAUDET)

    skipped = await skip(client, step)

    assert skipped.status_code == 200
    assert "<title>Votre assistant est autorisé - Twake Space</title>" in skipped.text
    assert SKIPPED in text_of(skipped)
    # Their agent acts for them everywhere but in Twake Space
    assert (await client.get("/forward-auth", headers=as_agent_of(MMAUDET))).status_code == 200
    response = await space_route(client, MMAUDET)
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == SPACE_TOKEN_MISSING


@pytest.mark.parametrize(
    ("scopes", "holds"),
    [
        (
            ("feed:read", "members:write", "space:write"),
            "Ce jeton permet : « Lire les fils », « Gérer les membres » et « Modifier les"
            " espaces ». Il ne permet pas : « Lire les espaces ».",
        ),
        (
            ("space:read", "members:write", "space:write"),
            "Ce jeton permet : « Lire les espaces », « Gérer les membres » et « Modifier les"
            " espaces ». Il ne permet pas : « Lire les fils ».",
        ),
        (
            ("notifications:write",),
            "Ce jeton ne permet pas : « Lire les espaces », « Lire les fils », « Gérer les"
            " membres » et « Modifier les espaces ».",
        ),
    ],
    ids=["without space:read", "without feed:read", "with none of them"],
)
async def test_a_space_token_without_a_scope_the_agent_needs_is_not_kept(
    client: AsyncClient,
    lemonldap: FakeLemonLDAP,
    space: FakeSpace,
    scopes: tuple[str, ...],
    holds: str,
) -> None:
    step = await consent(client, lemonldap, MMAUDET)

    refused = await paste(client, step, space.create_token(*scopes))

    assert refused.status_code == 400
    assert (
        f"{NOT_KEPT} il faut au moins « Lire les espaces » et « Lire les fils ». {holds}"
    ) in text_of(refused)
    assert (await space_route(client, MMAUDET)).json() == SPACE_TOKEN_MISSING


@pytest.mark.parametrize(
    "create",
    [
        lambda space: space.create_token(*EVERY_SCOPE, "tokens:write", account=MMAUDET),
        lambda space: space.create_organization_token(*EVERY_SCOPE, "tokens:write", role="admin"),
    ],
    ids=["of the owner", "of the organization"],
)
async def test_a_space_token_that_can_manage_tokens_is_not_kept(
    client: AsyncClient,
    lemonldap: FakeLemonLDAP,
    space: FakeSpace,
    create: Callable[[FakeSpace], str],
) -> None:
    """Were it to leak, it would create others."""
    step = await consent(client, lemonldap, MMAUDET)

    refused = await paste(client, step, create(space))

    assert refused.status_code == 400
    assert TOO_BROAD in text_of(refused)
    assert (await space_route(client, MMAUDET)).json() == SPACE_TOKEN_MISSING


@pytest.mark.parametrize(
    ("spaces", "create"),
    [
        ([{ALICE: "admin"}], lambda space: space.create_token(*EVERY_SCOPE, account=ALICE)),
        (
            [{ALICE: "admin", MMAUDET: "viewer"}],
            lambda space: space.create_token(*EVERY_SCOPE, account=ALICE),
        ),
        (
            [{ALICE: "editor", MMAUDET: "editor"}, {ALICE: "editor"}],
            lambda space: space.create_token(*EVERY_SCOPE, account=ALICE),
        ),
        (
            [{ALICE: "editor"}],
            lambda space: space.create_organization_token(*EVERY_SCOPE, role="editor"),
        ),
        (
            [{MMAUDET: "admin"}],
            lambda space: space.create_organization_token(*EVERY_SCOPE, role="viewer"),
        ),
    ],
    ids=[
        "of another account, in a space without the owner",
        "of another account, in a space where the owner has another role",
        "of another account, in one of its spaces without the owner",
        "of the organization, in a space without the owner",
        "of the organization, with another role than the owner's",
    ],
)
async def test_a_space_token_that_does_not_act_for_the_owner_in_every_space_is_not_kept(
    client: AsyncClient,
    lemonldap: FakeLemonLDAP,
    space: FakeSpace,
    spaces: list[dict[str, Role]],
    create: Callable[[FakeSpace], str],
) -> None:
    for members in spaces:
        space.create_space(members)
    step = await consent(client, lemonldap, MMAUDET)

    refused = await paste(client, step, create(space))

    assert refused.status_code == 400
    assert NOT_THEIRS in text_of(refused)
    assert (await space_route(client, MMAUDET)).json() == SPACE_TOKEN_MISSING


async def test_a_space_token_of_the_owner_is_kept_whatever_their_role_in_each_space(
    client: AsyncClient, lemonldap: FakeLemonLDAP, space: FakeSpace
) -> None:
    space.create_space({MMAUDET: "admin", ALICE: "viewer"})
    space.create_space({ALICE: "admin", MMAUDET: "viewer"})
    space.create_space({ALICE: "admin"})
    token = space.create_token(*EVERY_SCOPE, account=MMAUDET)
    step = await consent(client, lemonldap, MMAUDET)

    pasted = await paste(client, step, token)

    assert pasted.status_code == 200
    assert (await space_route(client, MMAUDET)).headers["x-twake-space-token"] == token


async def test_twake_space_knows_the_owner_by_their_email_whatever_its_case(
    client: AsyncClient, lemonldap: FakeLemonLDAP, space: FakeSpace
) -> None:
    space.create_space({"MMaudet@Example.TEST": "editor"})
    token = space.create_token(*EVERY_SCOPE, account="MMaudet@Example.TEST")
    step = await consent(client, lemonldap, MMAUDET)

    pasted = await paste(client, step, token)

    assert pasted.status_code == 200
    assert (await space_route(client, MMAUDET)).headers["x-twake-space-token"] == token


async def test_a_space_token_that_reaches_no_space_is_kept_as_nothing_tells_whose_it_is(
    client: AsyncClient, lemonldap: FakeLemonLDAP, space: FakeSpace
) -> None:
    space.create_space({MMAUDET: "admin"})
    token = space.create_token(*EVERY_SCOPE, account=ALICE)
    step = await consent(client, lemonldap, MMAUDET)

    pasted = await paste(client, step, token)

    assert pasted.status_code == 200
    assert (await space_route(client, MMAUDET)).headers["x-twake-space-token"] == token


async def test_a_space_token_is_not_kept_when_twake_space_fails_to_show_a_space_it_reaches(
    client: AsyncClient,
    lemonldap: FakeLemonLDAP,
    space: FakeSpace,
    caplog: pytest.LogCaptureFixture,
) -> None:
    space.create_space({MMAUDET: "admin"})
    space.failing_spaces.add(space.create_space({MMAUDET: "editor"}))
    step = await consent(client, lemonldap, MMAUDET)

    refused = await paste(client, step, space.create_token(*EVERY_SCOPE, account=MMAUDET))

    assert refused.status_code == 502
    assert (f"{NOT_KEPT} Twake Space n'a pas pu le vérifier. Réessayez dans un moment.") in text_of(
        refused
    )
    assert consent_warning(caplog).startswith(f"Twake Space checked no token of {MMAUDET}: ")
    assert (await space_route(client, MMAUDET)).json() == SPACE_TOKEN_MISSING


@pytest.mark.parametrize(
    "pasted",
    ["", "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJtbWF1ZGV0In0.c2lnbmF0dXJl", "tws_two words"],
    ids=["nothing", "a JWT", "with a blank inside"],
)
async def test_what_is_no_api_token_of_twake_space_is_neither_kept_nor_sent_to_it(
    client: AsyncClient, lemonldap: FakeLemonLDAP, space: FakeSpace, pasted: str
) -> None:
    step = await consent(client, lemonldap, MMAUDET)

    refused = await paste(client, step, pasted)

    assert refused.status_code == 400
    assert NOT_A_TOKEN in text_of(refused)
    assert space.requests == []
    assert (await space_route(client, MMAUDET)).json() == SPACE_TOKEN_MISSING


async def test_a_space_token_twake_space_does_not_know_is_not_kept(
    client: AsyncClient, lemonldap: FakeLemonLDAP, space: FakeSpace
) -> None:
    """Mistyped, revoked, or expired."""
    token = space.create_token(*EVERY_SCOPE)
    space.revoke(token)
    step = await consent(client, lemonldap, MMAUDET)

    refused = await paste(client, step, token)

    assert refused.status_code == 400
    assert f"{NOT_KEPT} Twake Space ne le reconnaît pas." in text_of(refused)
    assert (await space_route(client, MMAUDET)).json() == SPACE_TOKEN_MISSING


@pytest.mark.parametrize("outage", ["error page", "unreachable"])
async def test_a_space_token_twake_space_cannot_check_is_not_kept(
    client: AsyncClient,
    lemonldap: FakeLemonLDAP,
    space: FakeSpace,
    outage: Outage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = space.create_token(*EVERY_SCOPE)
    step = await consent(client, lemonldap, MMAUDET)
    space.outage = outage

    refused = await paste(client, step, token)

    assert refused.status_code == 502
    assert (f"{NOT_KEPT} Twake Space n'a pas pu le vérifier. Réessayez dans un moment.") in text_of(
        refused
    )
    assert consent_warning(caplog).startswith(f"Twake Space checked no token of {MMAUDET}: ")
    space.outage = None
    assert (await space_route(client, MMAUDET)).json() == SPACE_TOKEN_MISSING


async def test_an_owner_whose_token_was_not_kept_pastes_another_on_the_same_page(
    client: AsyncClient, lemonldap: FakeLemonLDAP, space: FakeSpace
) -> None:
    step = await consent(client, lemonldap, MMAUDET)
    refused = await paste(client, step, space.create_token("feed:read"))
    token = space.create_token(*EVERY_SCOPE)

    pasted = await paste(client, refused, token)

    assert pasted.status_code == 200
    assert (await space_route(client, MMAUDET)).headers["x-twake-space-token"] == token


async def test_a_form_too_big_to_hold_a_token_is_not_read_through(
    client: AsyncClient, lemonldap: FakeLemonLDAP, space: FakeSpace
) -> None:
    step = await consent(client, lemonldap, MMAUDET)

    refused = await paste(client, step, f"tws_{'a' * 5000}")

    assert refused.status_code == 400
    assert NOT_A_TOKEN in text_of(refused)
    assert space.requests == []
    # The step stays as it was
    token = space.create_token(*EVERY_SCOPE)
    assert (await paste(client, refused, token)).status_code == 200


async def test_the_space_step_in_a_browser_that_did_not_start_the_consent_is_refused(
    client: AsyncClient, lemonldap: FakeLemonLDAP, space: FakeSpace
) -> None:
    step = await consent(client, lemonldap, MMAUDET)
    client.cookies.clear()

    refused = await paste(client, step, space.create_token(*EVERY_SCOPE))

    assert refused.status_code == 400
    assert "Aucune autorisation n'est en cours dans ce navigateur." in text_of(refused)
    assert space.requests == []


async def test_a_space_step_left_for_more_than_ten_minutes_is_refused(
    client: AsyncClient, lemonldap: FakeLemonLDAP, space: FakeSpace, clock: FakeClock
) -> None:
    step = await consent(client, lemonldap, MMAUDET)
    clock.advance(601)

    refused = await paste(client, step, space.create_token(*EVERY_SCOPE))

    assert refused.status_code == 400
    assert "L'autorisation a expiré." in text_of(refused)
    assert space.requests == []


async def test_a_space_step_whose_state_differs_from_the_consent_in_progress_is_refused(
    client: AsyncClient, lemonldap: FakeLemonLDAP, space: FakeSpace
) -> None:
    await consent(client, lemonldap, MMAUDET)

    refused = await client.post(
        "/consent/space",
        data={"state": "forged", "space_token": space.create_token(*EVERY_SCOPE)},
    )

    assert refused.status_code == 400
    assert NOT_IN_PROGRESS in text_of(refused)
    assert space.requests == []


@pytest.mark.parametrize("step", ["lemonldap", "drive"])
async def test_the_space_form_posted_from_another_step_is_refused(
    client: AsyncClient, lemonldap: FakeLemonLDAP, space: FakeSpace, drive: str, step: str
) -> None:
    """Even with the state of that step."""
    client.cookies.clear()
    elsewhere = await client.get("/consent")
    if step == "drive":
        elsewhere = await client.get(lemonldap.sign_in(elsewhere.headers["location"], MMAUDET))

    refused = await client.post(
        "/consent/space",
        data={
            "state": URL(elsewhere.headers["location"]).params["state"],
            "space_token": space.create_token(*EVERY_SCOPE),
        },
    )

    assert refused.status_code == 400
    assert NOT_IN_PROGRESS in text_of(refused)
    assert space.requests == []


async def test_a_callback_while_on_the_space_step_is_refused(
    client: AsyncClient, lemonldap: FakeLemonLDAP
) -> None:
    """Even with the state of the Space step."""
    step = await consent(client, lemonldap, MMAUDET)

    refused = await client.get("/callback", params={"code": "a-code", "state": state_of(step)})

    assert refused.status_code == 400
    assert NOT_IN_PROGRESS in text_of(refused)


async def test_a_space_token_pasted_once_the_delegation_was_removed_is_refused(
    client: AsyncClient, lemonldap: FakeLemonLDAP, space: FakeSpace, database_url: str
) -> None:
    step = await consent(client, lemonldap, MMAUDET)
    await remove_delegation(database_url, MMAUDET)

    refused = await paste(client, step, space.create_token(*EVERY_SCOPE))

    assert refused.status_code == 400
    assert "Cette autorisation n'est plus en cours." in text_of(refused)
    skipped = await skip(client, await consent(client, lemonldap, MMAUDET))
    assert SKIPPED in text_of(skipped)
    assert (await space_route(client, MMAUDET)).json() == SPACE_TOKEN_MISSING


async def test_the_space_step_follows_the_drive_step(
    client: AsyncClient,
    lemonldap: FakeLemonLDAP,
    cozy_stack: FakeCozyStack,
    drive: str,
    space: FakeSpace,
) -> None:
    step = await consent_with_drive(client, lemonldap, cozy_stack, MMAUDET)

    assert step.status_code == 200
    assert (
        f"Votre assistant Twake Space peut désormais agir pour {MMAUDET}, y compris dans Drive."
    ) in text_of(step)
    pasted = await paste(client, step, space.create_token(*EVERY_SCOPE))
    assert pasted.status_code == 200
    assert "<title>Votre assistant est autorisé - Twake Space</title>" in pasted.text
    assert "y compris dans Drive" in text_of(pasted)
    drive_route = await client.get(
        "/forward-auth", params={"token": "drive"}, headers=as_agent_of(MMAUDET)
    )
    assert drive_route.status_code == 200


async def declined(
    client: AsyncClient, lemonldap: FakeLemonLDAP, cozy_stack: FakeCozyStack
) -> Response:
    """The owner declines on their Drive instance."""
    to_drive = await consent(client, lemonldap, MMAUDET)
    return await client.get(cozy_stack.refuse(to_drive.headers["location"]))


async def unregistered(
    client: AsyncClient, lemonldap: FakeLemonLDAP, cozy_stack: FakeCozyStack
) -> Response:
    """The owner's Drive instance does not answer when the broker registers on it."""
    cozy_stack.outage = "unreachable"
    return await consent(client, lemonldap, MMAUDET)


@pytest.mark.parametrize(
    ("drive_step", "status_code", "reason"),
    [
        (declined, 200, "Vous n'avez pas accordé l'accès à vos fichiers."),
        (unregistered, 502, "Votre instance Drive n'a pas pu terminer l'autorisation."),
    ],
    ids=["declined", "unavailable"],
)
async def test_the_space_step_follows_a_drive_step_that_failed_with_its_status(
    client: AsyncClient,
    lemonldap: FakeLemonLDAP,
    cozy_stack: FakeCozyStack,
    drive: str,
    space: FakeSpace,
    drive_step: Callable[[AsyncClient, FakeLemonLDAP, FakeCozyStack], Awaitable[Response]],
    status_code: int,
    reason: str,
) -> None:
    step = await drive_step(client, lemonldap, cozy_stack)

    assert step.status_code == status_code
    assert (
        f"Votre assistant Twake Space peut désormais agir pour {MMAUDET}, mais pas dans Drive."
        f" {reason}"
    ) in text_of(step)
    assert 'action="/consent/space"' in step.text
    pasted = await paste(client, step, space.create_token(*EVERY_SCOPE))
    assert pasted.status_code == 200
    assert "<title>Votre assistant est autorisé, sauf dans Drive - Twake Space</title>" in (
        pasted.text
    )
    assert f"{reason} {PASTED}" in text_of(pasted)
    assert 'href="/consent"' in pasted.text


async def test_a_new_consent_keeps_the_space_token(
    client: AsyncClient, lemonldap: FakeLemonLDAP, space: FakeSpace
) -> None:
    token = space.create_token(*EVERY_SCOPE)
    await paste(client, await consent(client, lemonldap, MMAUDET), token)

    skipped = await skip(client, await consent(client, lemonldap, MMAUDET))

    assert KEPT in text_of(skipped)
    assert (await space_route(client, MMAUDET)).headers["x-twake-space-token"] == token


async def test_a_space_token_pasted_again_replaces_the_one_before(
    client: AsyncClient, lemonldap: FakeLemonLDAP, space: FakeSpace
) -> None:
    await paste(client, await consent(client, lemonldap, MMAUDET), space.create_token(*EVERY_SCOPE))
    token = space.create_token("space:read", "feed:read")

    await paste(client, await consent(client, lemonldap, MMAUDET), token)

    assert (await space_route(client, MMAUDET)).headers["x-twake-space-token"] == token


async def test_each_owner_gets_their_own_space_token(
    client: AsyncClient, lemonldap: FakeLemonLDAP, space: FakeSpace
) -> None:
    mine = space.create_token(*EVERY_SCOPE)
    hers = space.create_token(*EVERY_SCOPE)
    await paste(client, await consent(client, lemonldap, MMAUDET), mine)
    await paste(client, await consent(client, lemonldap, ALICE), hers)

    assert (await space_route(client, MMAUDET)).headers["x-twake-space-token"] == mine
    assert (await space_route(client, ALICE)).headers["x-twake-space-token"] == hers


async def test_the_consent_link_for_space_goes_straight_to_the_space_step_and_keeps_drive(
    client: AsyncClient,
    lemonldap: FakeLemonLDAP,
    cozy_stack: FakeCozyStack,
    drive: str,
    space: FakeSpace,
) -> None:
    await skip(client, await consent_with_drive(client, lemonldap, cozy_stack, MMAUDET))
    token = space.create_token(*EVERY_SCOPE)

    step = await space_link(client, lemonldap, MMAUDET)

    assert step.status_code == 200
    assert 'action="/consent/space"' in step.text
    assert DRIVE_UNCHANGED in text_of(step)
    pasted = await paste(client, step, token)
    assert pasted.status_code == 200
    assert "<title>Votre assistant est autorisé - Twake Space</title>" in pasted.text
    assert f"{DRIVE_UNCHANGED} {PASTED}" in text_of(pasted)
    assert (await space_route(client, MMAUDET)).headers["x-twake-space-token"] == token
    drive_route = await client.get(
        "/forward-auth", params={"token": "drive"}, headers=as_agent_of(MMAUDET)
    )
    assert drive_route.status_code == 200


async def test_the_consent_link_for_space_signed_in_as_someone_else_stores_nothing(
    client: AsyncClient, lemonldap: FakeLemonLDAP
) -> None:
    refused = await space_link(client, lemonldap, MMAUDET, signed_in_as=ALICE)

    assert refused.status_code == 403
    assert "<title>Ce lien est pour un autre compte - Twake Space</title>" in refused.text
    # Starting over keeps to the link for Space
    assert 'href="/consent?owner=mmaudet%40example.test&app=space"' in text_of(refused)
    for user in (MMAUDET, ALICE):
        response = await client.get("/forward-auth", headers=as_agent_of(user))
        assert response.json()["code"] == "delegation_missing"


async def test_the_consent_link_for_space_of_an_owner_who_revoked_their_delegation_asks_for_drive(
    client: AsyncClient, lemonldap: FakeLemonLDAP, cozy_stack: FakeCozyStack, drive: str
) -> None:
    """The broker keeps a revoked Drive delegation until it has left the instance: it does not
    come back."""
    await skip(client, await consent_with_drive(client, lemonldap, cozy_stack, MMAUDET))
    cozy_stack.outage = "unreachable"
    assert (await client.delete("/delegation", headers=as_agent_of(MMAUDET))).status_code == 502
    cozy_stack.outage = None

    to_drive = await space_link(client, lemonldap, MMAUDET)

    assert to_drive.status_code == 302
    assert URL(to_drive.headers["location"]).host == drive
    drive_route = await client.get(
        "/forward-auth", params={"token": "drive"}, headers=as_agent_of(MMAUDET)
    )
    assert drive_route.json()["code"] == "delegation_missing"


async def test_keeping_the_space_token_changes_nothing(
    client: AsyncClient, lemonldap: FakeLemonLDAP, space: FakeSpace, clock: FakeClock
) -> None:
    token = space.create_token("space:read", "feed:read")
    await paste(client, await consent(client, lemonldap, MMAUDET), token)
    clock.advance(86_400)

    kept = await skip(client, await space_link(client, lemonldap, MMAUDET))

    assert kept.status_code == 200
    assert f"{DRIVE_UNCHANGED} {KEPT}" in text_of(kept)
    assert (await space_route(client, MMAUDET)).headers["x-twake-space-token"] == token
    assert (
        "Un jeton est déjà enregistré, collé le 21 septembre 2026. Ce jeton permet : « Lire les"
        " espaces » et « Lire les fils »."
    ) in text_of(await space_link(client, lemonldap, MMAUDET))


@pytest.mark.parametrize(
    ("create", "problem"),
    [
        (
            lambda space: space.create_token("feed:read", account=MMAUDET),
            f"{NOT_KEPT} il faut au moins « Lire les espaces » et « Lire les fils ».",
        ),
        (
            lambda space: space.create_token(*EVERY_SCOPE, "tokens:write", account=MMAUDET),
            TOO_BROAD,
        ),
        (
            lambda space: space.create_organization_token(*EVERY_SCOPE, role="viewer"),
            NOT_THEIRS,
        ),
    ],
    ids=["lacking a scope", "too broad", "of the organization"],
)
async def test_a_space_token_refused_in_place_of_the_kept_one_leaves_it(
    client: AsyncClient,
    lemonldap: FakeLemonLDAP,
    space: FakeSpace,
    create: Callable[[FakeSpace], str],
    problem: str,
) -> None:
    space.create_space({MMAUDET: "admin"})
    token = space.create_token(*EVERY_SCOPE, account=MMAUDET)
    await paste(client, await consent(client, lemonldap, MMAUDET), token)

    refused = await paste(client, await space_link(client, lemonldap, MMAUDET), create(space))

    assert refused.status_code == 400
    assert problem in text_of(refused)
    assert "Un jeton est déjà enregistré, collé le 21 septembre 2026." in text_of(refused)
    assert (await space_route(client, MMAUDET)).headers["x-twake-space-token"] == token


async def test_removing_the_space_token_closes_twake_space_alone_to_the_agent(
    client: AsyncClient, lemonldap: FakeLemonLDAP, space: FakeSpace
) -> None:
    await paste(client, await consent(client, lemonldap, MMAUDET), space.create_token(*EVERY_SCOPE))

    removed = await remove(client, await space_link(client, lemonldap, MMAUDET))

    assert removed.status_code == 200
    assert "<title>Votre assistant est autorisé - Twake Space</title>" in removed.text
    assert f"{DRIVE_UNCHANGED} {REMOVED}" in text_of(removed)
    assert (await space_route(client, MMAUDET)).json() == SPACE_TOKEN_MISSING
    assert (await client.get("/forward-auth", headers=as_agent_of(MMAUDET))).status_code == 200


async def test_a_consent_for_space_that_expired_starts_over_from_the_link_for_space(
    client: AsyncClient, lemonldap: FakeLemonLDAP, space: FakeSpace, clock: FakeClock
) -> None:
    await skip(client, await consent(client, lemonldap, MMAUDET))
    step = await space_link(client, lemonldap, MMAUDET)
    clock.advance(601)

    refused = await paste(client, step, space.create_token(*EVERY_SCOPE))

    assert refused.status_code == 400
    assert "L'autorisation a expiré." in text_of(refused)
    assert 'href="/consent?owner=mmaudet%40example.test&app=space"' in text_of(refused)


@pytest.mark.parametrize(
    "outage", [None, "unreachable"], ids=["drive answering", "drive not answering"]
)
async def test_revoking_the_delegation_forgets_the_space_token_at_once(
    client: AsyncClient,
    lemonldap: FakeLemonLDAP,
    cozy_stack: FakeCozyStack,
    drive: str,
    space: FakeSpace,
    outage: Outage | None,
) -> None:
    """A new consent does not bring it back, even while the broker stays on the Drive instance
    that did not answer."""
    step = await consent_with_drive(client, lemonldap, cozy_stack, MMAUDET)
    await paste(client, step, space.create_token(*EVERY_SCOPE))
    cozy_stack.outage = outage
    await client.delete("/delegation", headers=as_agent_of(MMAUDET))
    cozy_stack.outage = None

    step = await consent_with_drive(client, lemonldap, cozy_stack, MMAUDET)

    assert SKIPPED in text_of(await skip(client, step))
    assert (await space_route(client, MMAUDET)).json() == SPACE_TOKEN_MISSING


async def test_a_space_route_of_an_owner_who_never_consented_gets_the_consent_link(
    client: AsyncClient,
) -> None:
    response = await space_route(client, MMAUDET)

    assert response.status_code == 401
    assert response.json()["code"] == "delegation_missing"
    assert (
        response.json()["consent_url"] == f"{PUBLIC_BASE_URL}/consent?owner=mmaudet%40example.test"
    )


async def test_a_space_route_of_an_owner_whose_delegation_ended_is_refused(
    client: AsyncClient, lemonldap: FakeLemonLDAP, space: FakeSpace, clock: FakeClock
) -> None:
    await paste(client, await consent(client, lemonldap, MMAUDET), space.create_token(*EVERY_SCOPE))
    lemonldap.end_offline_session(MMAUDET)

    clock.advance(60)
    response = await space_route(client, MMAUDET)

    assert response.status_code == 401
    assert response.json()["code"] == "delegation_expired"
    assert "x-twake-space-token" not in response.headers


async def test_a_space_route_of_an_owner_who_revoked_their_delegation_is_refused(
    client: AsyncClient, lemonldap: FakeLemonLDAP, space: FakeSpace
) -> None:
    await paste(client, await consent(client, lemonldap, MMAUDET), space.create_token(*EVERY_SCOPE))
    assert (await client.delete("/delegation", headers=as_agent_of(MMAUDET))).status_code == 204

    response = await space_route(client, MMAUDET)

    assert response.status_code == 401
    assert response.json()["code"] == "delegation_missing"


async def test_a_space_token_kept_under_another_encryption_key_counts_as_missing(
    settings: Settings, lemonldap: FakeLemonLDAP, clock: FakeClock, space: FakeSpace
) -> None:
    async with running(settings, lemonldap, clock, space=space) as broker:
        step = await consent(broker, lemonldap, MMAUDET)
        await paste(broker, step, space.create_token(*EVERY_SCOPE))

    with_another_key = replace(settings, encryption_key=bytes(32))
    async with running(with_another_key, lemonldap, clock, space=space) as broker:
        await consent(broker, lemonldap, MMAUDET)
        response = await space_route(broker, MMAUDET)

    assert response.json() == SPACE_TOKEN_MISSING


async def test_a_token_the_broker_does_not_give_is_an_invalid_request_naming_space(
    client: AsyncClient, lemonldap: FakeLemonLDAP
) -> None:
    await consent(client, lemonldap, MMAUDET)

    response = await client.get(
        "/forward-auth", params={"token": "spcae"}, headers=as_agent_of(MMAUDET)
    )

    assert response.status_code == 400
    assert response.json()["code"] == "unknown_token"
    assert response.json()["detail"] == "The token query parameter can only ask for drive or space."


async def test_without_twake_space_the_consent_has_no_space_step(
    settings: Settings, lemonldap: FakeLemonLDAP, clock: FakeClock, space: FakeSpace
) -> None:
    without_space = replace(settings, space_url=None, space_web_url=None)
    async with running(without_space, lemonldap, clock, space=space) as broker:
        authorized = await consent(broker, lemonldap, MMAUDET)
        posted = await broker.post(
            "/consent/space",
            data={"state": "any", "space_token": space.create_token(*EVERY_SCOPE)},
        )

    assert authorized.status_code == 200
    assert "<title>Votre assistant est autorisé - Twake Space</title>" in authorized.text
    assert "/consent/space" not in authorized.text
    assert posted.status_code == 404
    assert space.requests == []


@pytest.mark.parametrize(
    ("pasted_at", "day"),
    [
        (datetime(2026, 9, 21, 14, 13, tzinfo=UTC), "21 septembre 2026"),
        (datetime(2026, 10, 1, 9, 30, tzinfo=UTC), "1 octobre 2026"),
    ],
    ids=["any day", "the first of a month"],
)
async def test_a_space_token_is_kept_encrypted_with_its_scopes_and_when_it_was_pasted(
    client: AsyncClient,
    lemonldap: FakeLemonLDAP,
    space: FakeSpace,
    clock: FakeClock,
    database_url: str,
    pasted_at: datetime,
    day: str,
) -> None:
    token = space.create_token("space:read", "feed:read")
    clock.now = pasted_at.timestamp()
    await paste(client, await consent(client, lemonldap, MMAUDET), token)
    clock.advance(86_400)

    step = await space_link(client, lemonldap, MMAUDET)

    assert (
        f"Un jeton est déjà enregistré, collé le {day}. Ce jeton permet : « Lire les espaces »"
        " et « Lire les fils ». Il ne permet pas : « Gérer les membres » et « Modifier les"
        " espaces »."
    ) in text_of(step)
    for button in ("Remplacer le jeton", "Garder ce jeton", "Retirer ce jeton"):
        assert f">{button}</button>" in step.text
    dump = await database_dump(database_url)
    assert token not in dump
    assert token.encode().hex() not in dump


async def test_no_space_token_shows_in_the_logs_the_pages_or_the_cookie(
    client: AsyncClient,
    lemonldap: FakeLemonLDAP,
    space: FakeSpace,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    lacking = space.create_token("feed:read")
    unchecked = space.create_token(*EVERY_SCOPE)
    token = space.create_token(*EVERY_SCOPE)
    step = await consent(client, lemonldap, MMAUDET)

    answers = [await paste(client, step, lacking)]
    cookie = Signer(settings.encryption_key).verify(client.cookies[COOKIE])
    space.outage = "unreachable"
    answers.append(await paste(client, step, unchecked))
    answers.append(await space_route(client, MMAUDET))
    space.outage = None
    answers.append(await paste(client, step, token))
    kept = await space_link(client, lemonldap, MMAUDET)
    answers.extend([kept, await skip(client, kept)])

    for secret in (lacking, unchecked, token):
        assert secret not in caplog.text
        assert all(secret not in answer.text for answer in answers)
        assert secret not in json.dumps(cookie)
