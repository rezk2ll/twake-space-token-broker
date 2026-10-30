"""The consent link, through which a user lets their agent act for them.

The consent runs in steps: the user signs in to LemonLDAP, then, when LemonLDAP names their Drive
instance, lets the broker in on that instance, both coming back to /callback. When the broker
reaches Twake Space, the consent ends on the Space step, whose form posts the API token the user
pastes. A link bound to its owner checks that the account that signed in is theirs. The owner's
link for Space goes from LemonLDAP straight to the Space step, and leaves their Drive delegation as
it was.
"""

import logging
import secrets
from collections.abc import Callable, Collection
from datetime import UTC, datetime
from enum import StrEnum
from html import escape
from importlib.resources import files
from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Cookie, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from twake_space_token_broker.consent_links import CONSENT, SPACE_APP, consent_link
from twake_space_token_broker.cozy_stack import InstanceRefused, InstanceUnavailable
from twake_space_token_broker.drive import DriveTokens
from twake_space_token_broker.keys import Signer
from twake_space_token_broker.lemonldap import (
    GrantRefused,
    LemonLDAP,
    LemonLDAPUnavailable,
    OfflineAccessDenied,
    SignedIn,
)
from twake_space_token_broker.space import (
    SCOPES,
    KeptToken,
    NotASpaceToken,
    NotTheirs,
    ScopesMissing,
    SpaceRefused,
    SpaceTokens,
    SpaceUnavailable,
    TooBroad,
)
from twake_space_token_broker.tokens import AccessTokens, DelegationMissing

logger = logging.getLogger(__name__)

COOKIE = "twake_space_consent"
COOKIE_LIFETIME = 600
"""Seconds a user has to sign in once the consent has started, to accept on their Drive, and to
paste their Space token."""

COOKIE_KEPT = 3600
"""Seconds the browser keeps the cookie of a consent.

Longer than the consent lasts, so that a consent that expired still starts over from its owner's
link.
"""

DRIVE_STEP = "drive"
"""The step of a consent in progress whose user is on their Drive instance."""

SPACE_STEP = "space"
"""The step of a consent in progress whose user pastes an API token of Twake Space."""

SPACE_FORM = "/consent/space"
"""Where the form of the Space step posts, relative to the broker."""

FORM_LIMIT = 4096
"""Bytes the form of the Space step may hold: many times a token, which takes about fifty."""

TOKEN_TO_CREATE = (
    "Nom : « Assistant ».",
    "Ce qu'il peut faire : Espaces « Écriture », Fil d'activité « Lecture », Membres"
    " « Écriture », Jetons d'API « Aucun accès ».",
    "Espaces accessibles : « Tous les espaces », soit « Tous mes espaces, y compris ceux que je"
    " rejoindrai ».",
    "Expire dans : « 90 jours ».",
    "Copiez le jeton, que Twake Space n'affiche qu'une fois, puis collez-le ci-dessous.",
)
"""The API token to create on Space's page of API tokens, in the words of that page."""

SPACE_SCOPES_NEEDED = (
    "« Lire les espaces » et « Lire les fils » lui sont indispensables. Sans « Modifier les"
    " espaces », il ne crée ni ne renomme d'espace ; sans « Gérer les membres », il n'ajoute ni"
    " ne retire personne."
)

SPACE_SKIPPED = "Sans jeton d'API, il n'agit pas dans vos espaces Twake Space."

SPACE_KEPT = "Dans vos espaces Twake Space, il garde le jeton d'API que vous aviez collé."

SPACE_PASTED = "Dans vos espaces Twake Space, il agit avec le jeton que vous avez collé."

SPACE_REMOVED = (
    "Il n'agit plus dans vos espaces Twake Space : le jeton d'API que vous aviez collé est retiré."
    " Révoquez-le aussi sur la page « Jetons d'API » de Twake Space."
)

NOT_KEPT = "Ce jeton n'a pas été enregistré :"

NOT_A_TOKEN = f"{NOT_KEPT} ce n'est pas un jeton d'API de Twake Space, qui commence par tws_."

NOT_IN_PROGRESS = "Cette page ne correspond pas à l'autorisation en cours."

UNAVAILABLE = "Votre instance Drive n'a pas pu terminer l'autorisation."

MONTHS = (
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
)


class DriveOutcome(StrEnum):
    """How the Drive step of a consent ended."""

    GRANTED = "granted"
    ABSENT = "absent"
    """LemonLDAP names no Drive instance of the user."""
    DECLINED = "declined"
    """The user declined on their instance."""
    REFUSED = "refused"
    """The user's instance refused the code it gave."""
    UNAVAILABLE = "unavailable"
    """The user's instance did not answer."""
    UNCHANGED = "unchanged"
    """The consent was for Space alone: it left the user's Drive delegation as it was."""


WITHOUT_DRIVE = {
    DriveOutcome.DECLINED: ("Vous n'avez pas accordé l'accès à vos fichiers.", 200),
    DriveOutcome.REFUSED: ("Votre instance Drive a refusé le code d'autorisation.", 400),
    DriveOutcome.UNAVAILABLE: (UNAVAILABLE, 502),
}
"""Why a consent ends without Drive, and the status of its page."""


def _asset(name: str) -> str:
    return (files("twake_space_token_broker") / "page" / name).read_text()


class Tone(StrEnum):
    """How a page ends the consent, shown by the icon above its title."""

    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


STYLE = _asset("twake-css-vars.css") + _asset("twake-css-utils.css") + _asset("consent.css")
"""The variables and utility classes of @linagora/twake-css, which npm ci copies next to the page,
then the looks of the components twake-mui would give."""

BRAND = _asset("twake-workplace.svg") + _asset("twake-text.svg")

ICONS = {
    Tone.SUCCESS: _asset("check-circle.svg"),
    Tone.WARNING: _asset("warning.svg"),
    Tone.ERROR: _asset("warning-circle.svg"),
}


def _page(
    title: str,
    *paragraphs: str,
    tone: Tone | None = None,
    problem: str | None = None,
    retry_url: str | None = None,
    status_code: int = 200,
    more: str = "",
) -> HTMLResponse:
    """A short page in French, in the look of Twake.

    Its title, problem and paragraphs are text, which the page escapes; more is HTML that follows
    them, such as a form. A problem stands out above the paragraphs. With a retry URL, a button
    lets the user start the consent over. A page whose more holds the steps of the Space token
    shows them in a column of their own on wide screens.
    """
    status = (
        f'\n<div class="status status-{tone} u-flex u-flex-items-center u-flex-justify-center'
        f' u-bdrs-circle u-mb-1" aria-hidden="true">{ICONS[tone]}</div>'
        if tone
        else ""
    )
    alert = (
        f'\n<p class="alert u-flex u-p-1 u-mt-0 u-mb-1 u-fz-small" role="alert">'
        f"{ICONS[Tone.ERROR]}<span>{escape(problem)}</span></p>"
        if problem
        else ""
    )
    text = "".join(
        f'\n<p class="u-mt-0 u-mb-1">{escape(paragraph)}</p>' for paragraph in paragraphs
    )
    link = (
        f'\n<p class="u-flex u-mt-1-half u-mb-0"><a class="button" href="{escape(retry_url)}">'
        "Recommencer</a></p>"
        if retry_url
        else ""
    )
    width = "page page-wide" if 'class="how' in more else "page"
    return HTMLResponse(
        f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} - Twake Space</title>
<style>
{STYLE}</style>
</head>
<body class="u-m-0 u-pv-1-half u-ph-1">
<header class="{width} u-flex u-flex-items-center u-mh-auto u-mb-1">
<div class="brand u-flex u-flex-items-center" role="img" aria-label="Twake">{BRAND}</div>
</header>
<main class="{width} u-mh-auto u-p-2 u-p-1-half-s u-elevation-1">{status}
<h1 class="u-mt-0 u-mb-1">{escape(title)}</h1>{alert}{text}{more}{link}
</main>
</body>
</html>
""",
        status_code=status_code,
    )


def _refused(reason: str, *, retry_url: str, status_code: int = 400) -> HTMLResponse:
    response = _page(
        "L'autorisation n'a pas abouti",
        reason,
        tone=Tone.ERROR,
        retry_url=retry_url,
        status_code=status_code,
    )
    response.delete_cookie(COOKIE)
    return response


def _authorized(message: str) -> HTMLResponse:
    response = _page(
        "Votre assistant est autorisé",
        f"{message} Vous pouvez fermer cette page.",
        tone=Tone.SUCCESS,
    )
    response.delete_cookie(COOKIE)
    return response


def _without_drive(message: str, *, retry_url: str, status_code: int) -> HTMLResponse:
    """The user's agent may act for them, but not in Drive: they learn why, and may retry."""
    response = _page(
        "Votre assistant est autorisé, sauf dans Drive",
        message,
        tone=Tone.WARNING,
        retry_url=retry_url,
        status_code=status_code,
    )
    response.delete_cookie(COOKIE)
    return response


def _wrong_account(owner: str, signed_in: str, *, retry_url: str) -> HTMLResponse:
    """The browser is signed in as another account than the one the link is for."""
    response = _page(
        "Ce lien est pour un autre compte",
        f"Ce lien autorise l'assistant de {owner}, mais ce navigateur est connecté à Twake en"
        f" tant que {signed_in} : rien n'a été enregistré. Ouvrez ce lien dans une fenêtre de"
        f" navigation privée, puis connectez-vous en tant que {owner}.",
        tone=Tone.ERROR,
        retry_url=retry_url,
        status_code=403,
    )
    response.delete_cookie(COOKIE)
    return response


def _authorized_for(user: str, drive: DriveOutcome) -> str:
    """What the user's agent may now do, Drive included or not, and why not."""
    if drive is DriveOutcome.GRANTED:
        return f"Votre assistant Twake Space peut désormais agir pour {user}, y compris dans Drive."
    if drive is DriveOutcome.ABSENT:
        return (
            f"Votre assistant Twake Space peut désormais agir pour {user}."
            " Drive n'est pas disponible pour votre compte."
        )
    if drive is DriveOutcome.UNCHANGED:
        return (
            f"Votre assistant Twake Space peut toujours agir pour {user}."
            " Son accès à Drive ne change pas."
        )
    reason, _ = WITHOUT_DRIVE[drive]
    return (
        f"Votre assistant Twake Space peut désormais agir pour {user}, mais pas dans Drive."
        f" {reason}"
    )


def _last_page(
    user: str, owner: str | None, drive: DriveOutcome, *, status_code: int, space: str = ""
) -> HTMLResponse:
    """The last page of a consent LemonLDAP granted: what the user's agent may now do, and in
    Twake Space too once they went through the Space step."""
    message = f"{_authorized_for(user, drive)} {space}".rstrip()
    if drive in WITHOUT_DRIVE:
        return _without_drive(message, retry_url=_retry_url(owner), status_code=status_code)
    return _authorized(message)


def _labels(scopes: Collection[str]) -> str:
    """The scopes, among those the agent uses, named and ordered as Space's list of tokens does."""
    return ", ".join(f"« {label} »" for scope, label in SCOPES.items() if scope in scopes)


def _allows(held: frozenset[str]) -> str:
    """What a token allows the agent, and what it does not, in the words of Space."""
    lacking = SCOPES.keys() - held
    if not held:
        return f"Ce jeton ne permet pas : {_labels(lacking)}."
    if not lacking:
        return f"Ce jeton permet : {_labels(held)}."
    return f"Ce jeton permet : {_labels(held)}. Il ne permet pas : {_labels(lacking)}."


def _day(moment: datetime) -> str:
    """The day of the moment in UTC, as French writes it: 1er octobre 2026, 21 septembre 2026."""
    day = moment.astimezone(UTC)
    return f"{'1er' if day.day == 1 else day.day} {MONTHS[day.month - 1]} {day.year}"


def _space_step(
    user: str,
    drive: DriveOutcome,
    *,
    state: str,
    tokens_page: str,
    status_code: int,
    kept: KeptToken | None,
    problem: str | None = None,
) -> HTMLResponse:
    """Which API token to create in Twake Space, in the words of its page, and where to paste it:
    after why the token pasted was not kept, if it was not.

    With a token kept, the step says when it was pasted and what it allows, and offers to keep it,
    to replace it or to remove it.
    """
    steps = "".join(f"\n<li>{escape(step)}</li>" for step in TOKEN_TO_CREATE)
    if kept is None:
        paragraphs = [
            "Pour qu'il lise vos espaces Twake Space et leurs fils, et qu'il en gère les membres,"
            " créez-lui un jeton d'API dans Twake Space, puis collez-le ici."
        ]
        buttons = """<button class="button" type="submit">Enregistrer le jeton</button>
<button class="button button-outlined" type="submit" name="skip" value="yes"
formnovalidate>Passer cette étape</button>"""
    else:
        paragraphs = [
            f"Un jeton est déjà enregistré, collé le {_day(kept.pasted_at)}."
            f" {_allows(kept.scopes)}",
            "Gardez-le, retirez-le, ou remplacez-le : créez un autre jeton d'API dans Twake Space,"
            " puis collez-le ici.",
        ]
        buttons = """<button class="button" type="submit">Remplacer le jeton</button>
<button class="button button-outlined" type="submit" name="skip" value="yes"
formnovalidate>Garder ce jeton</button>
<button class="button button-danger u-ml-auto u-ml-0-s" type="submit" name="remove" value="yes"
formnovalidate>Retirer ce jeton</button>"""
    return _page(
        "Votre assistant dans vos espaces",
        _authorized_for(user, drive),
        *paragraphs,
        problem=problem,
        more=f"""
<div class="how">
<ol class="steps u-mt-0 u-mb-1 u-p-1 u-pl-2-half u-bdrs-4">
<li>Ouvrez la page <a href="{escape(tokens_page)}">Jetons d'API</a> de Twake Space, puis cliquez sur
« Créer un jeton ».</li>{steps}
</ol>
<p class="secondary u-mt-0 u-mb-1 u-fz-small">{escape(SPACE_SCOPES_NEEDED)}</p>
<form class="u-mt-1 u-pt-1" method="post" action="{SPACE_FORM}">
<input type="hidden" name="state" value="{escape(state)}">
<p class="u-m-0"><label class="u-db u-mb-half u-fz-small" for="space_token">Jeton</label>
<input class="u-w-100" id="space_token" name="space_token" type="text" autocomplete="off"
spellcheck="false" required></p>
<div class="actions u-flex u-flex-wrap u-flex-column-s u-mt-1">{buttons}</div>
</form>
</div>""",
        status_code=status_code,
    )


async def _form(request: Request) -> dict[str, str] | None:
    """The fields of the urlencoded form the browser posted, the first value of each: None for a
    form bigger than FORM_LIMIT, which holds no token."""
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > FORM_LIMIT:
            return None
    fields = parse_qs(body.decode(errors="replace"), keep_blank_values=True)
    return {name: values[0] for name, values in fields.items()}


def _retry_url(owner: str | None, app: str | None = None) -> str:
    """Where a failed consent starts over: from its owner's link, for the app it was for, when it
    had one."""
    return consent_link(owner, app=app) if owner else CONSENT


def _owner(value: str | None) -> str | None:
    """The owner a consent link names, or None for the plain link.

    It is kept exactly as given, as the broker keys and looks up delegations by email exactly.
    """
    return (value or "").strip() or None


def _same_state(flow: dict[str, Any], state: str | None) -> bool:
    return secrets.compare_digest(flow["state"].encode(), (state or "").encode())


def router(
    lemonldap: LemonLDAP,
    signer: Signer,
    access_tokens: AccessTokens,
    drive_tokens: DriveTokens,
    clock: Callable[[], float],
    space_tokens: SpaceTokens | None = None,
) -> APIRouter:
    routes = APIRouter()

    def remember(response: Response, flow: dict[str, Any]) -> None:
        """Keeps the step in progress in the user's browser, signed, until it expires."""
        expires = clock() + COOKIE_LIFETIME
        response.set_cookie(
            COOKIE,
            signer.sign({**flow, "expires": expires}),
            max_age=COOKIE_KEPT,
            httponly=True,
            secure=True,
            samesite="lax",
        )

    def in_progress(started: str | None) -> dict[str, Any] | HTMLResponse:
        """The consent the browser has in progress, or why the page that came back is refused."""
        flow = signer.verify(started) if started else None
        if flow is None:
            return _refused(
                "Aucune autorisation n'est en cours dans ce navigateur.", retry_url=CONSENT
            )
        if clock() >= flow["expires"]:
            return _refused(
                "L'autorisation a expiré.",
                retry_url=_retry_url(flow.get("owner"), flow.get("app")),
            )
        return flow

    @routes.get(CONSENT)
    async def consent(owner: str | None = None, app: str | None = None) -> RedirectResponse:
        """Starts a consent, bound to its owner when the link names them, and for Space alone
        when the owner's link names that app."""
        owner = _owner(owner)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        response = RedirectResponse(
            lemonldap.authorize_url(state=state, verifier=verifier, owner=owner),
            status_code=302,
        )
        # Only the owner's link is for an app: the plain link stays the whole consent
        app = SPACE_APP if owner is not None and app == SPACE_APP else None
        remember(response, {"state": state, "verifier": verifier, "owner": owner, "app": app})
        return response

    @routes.get("/callback")
    async def callback(
        code: str | None = None,
        state: str | None = None,
        started: Annotated[str | None, Cookie(alias=COOKIE)] = None,
    ) -> Response:
        flow = in_progress(started)
        if isinstance(flow, HTMLResponse):
            return flow
        retry_url = _retry_url(flow.get("owner"), flow.get("app"))
        step = flow.get("step")
        if step == DRIVE_STEP:
            return await drive_callback(flow, code, state)
        # No step comes back here but LemonLDAP's and Drive's: the Space step posts its form
        if step is not None or not _same_state(flow, state):
            return _refused(NOT_IN_PROGRESS, retry_url=retry_url)
        if code is None:
            return _refused("LemonLDAP n'a pas accordé l'autorisation.", retry_url=retry_url)
        try:
            signed_in = await lemonldap.redeem(code, flow["verifier"])
        except GrantRefused:
            return _refused("LemonLDAP a refusé le code d'autorisation.", retry_url=retry_url)
        except LemonLDAPUnavailable as unavailable:
            logger.warning("LemonLDAP completed no consent: %s", unavailable)
            return _refused(
                "LemonLDAP n'a pas pu terminer l'autorisation.",
                retry_url=retry_url,
                status_code=502,
            )
        except OfflineAccessDenied:
            logger.warning("LemonLDAP granted no offline access: check the client's options")
            return _refused(
                "LemonLDAP n'a pas accordé d'accès hors ligne à l'assistant.",
                retry_url=retry_url,
                status_code=502,
            )
        owner = flow.get("owner")
        if owner is not None and signed_in.user != owner:
            # A browser still signed in as someone else, as LemonLDAP's "stay connected" keeps it:
            # nothing is stored, and that account's own delegations stay as they were
            logger.warning(
                "A consent for %s came back signed in as %s: nothing stored", owner, signed_in.user
            )
            return _wrong_account(owner, signed_in.user, retry_url=retry_url)
        # The owner may come for their Space token alone while their delegation holds: once they
        # revoked it, the broker keeps its Drive delegation only to leave the instance
        space_alone = (
            flow.get("app") == SPACE_APP
            and await access_tokens.consented_at(signed_in.user) is not None
        )
        await access_tokens.consented(signed_in)
        if space_alone:
            return await end(signed_in.user, owner, DriveOutcome.UNCHANGED, app=SPACE_APP)
        # The new consent replaces the whole delegation: Drive comes back only if granted again,
        # while the Space token stays, as the owner pasted it in Space's stead
        await drive_tokens.forget_even_if_unavailable(signed_in.user)
        return await to_drive(signed_in, owner)

    async def to_drive(signed_in: SignedIn, owner: str | None) -> Response:
        """Sends the user on to their Drive instance, if LemonLDAP names one.

        A consent bound to its owner stays bound through the Drive step.
        """
        try:
            instance = await lemonldap.drive_instance(signed_in.tokens.access_token)
        except LemonLDAPUnavailable as unavailable:
            # The agent may need LemonLDAP's token alone: Drive never fails the consent
            logger.warning(
                "LemonLDAP named no Drive instance of %s: %s", signed_in.user, unavailable
            )
            instance = None
        if instance is None:
            return await end(signed_in.user, owner, DriveOutcome.ABSENT)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        try:
            authorize_url = await drive_tokens.request(
                signed_in.user, instance, state=state, verifier=verifier
            )
        except InstanceUnavailable as unavailable:
            logger.warning(
                "The Drive instance of %s took no client: %s", signed_in.user, unavailable
            )
            return await end(signed_in.user, owner, DriveOutcome.UNAVAILABLE)
        response = RedirectResponse(authorize_url, status_code=302)
        remember(
            response,
            {
                "step": DRIVE_STEP,
                "user": signed_in.user,
                "owner": owner,
                "state": state,
                "verifier": verifier,
            },
        )
        return response

    async def drive_callback(flow: dict[str, Any], code: str | None, state: str | None) -> Response:
        """Where the user's Drive instance sends them back."""
        user = flow["user"]
        owner = flow.get("owner")
        if code is None:
            # The instance's page links back with no state when the user declines: the answer
            # changes nothing the broker keeps
            return await end(user, owner, DriveOutcome.DECLINED)
        retry_url = _retry_url(owner)
        if not _same_state(flow, state):
            return _refused(NOT_IN_PROGRESS, retry_url=retry_url)
        try:
            await drive_tokens.consented(user, code, flow["verifier"])
        except DelegationMissing:
            return _refused("Cette autorisation n'est plus en cours.", retry_url=retry_url)
        except InstanceRefused:
            return await end(user, owner, DriveOutcome.REFUSED)
        except InstanceUnavailable as unavailable:
            logger.warning("The Drive instance of %s completed no consent: %s", user, unavailable)
            return await end(user, owner, DriveOutcome.UNAVAILABLE)
        return await end(user, owner, DriveOutcome.GRANTED)

    async def end(
        user: str, owner: str | None, drive: DriveOutcome, *, app: str | None = None
    ) -> Response:
        """Where every consent LemonLDAP granted ends, whether Drive was granted, absent, failed
        or left as it was: on the Space step, when the broker reaches Twake Space.

        The page bears the status of the Drive step. A consent for an app starts over from the
        owner's link for that app.
        """
        status_code = WITHOUT_DRIVE[drive][1] if drive in WITHOUT_DRIVE else 200
        if space_tokens is None:
            return _last_page(user, owner, drive, status_code=status_code)
        state = secrets.token_urlsafe(32)
        response = _space_step(
            user,
            drive,
            state=state,
            tokens_page=space_tokens.tokens_page,
            status_code=status_code,
            kept=await space_tokens.kept(user),
        )
        remember(
            response,
            {
                "step": SPACE_STEP,
                "user": user,
                "owner": owner,
                "app": app,
                "state": state,
                "drive": drive,
            },
        )
        return response

    if space_tokens is None:
        return routes

    async def step_again(flow: dict[str, Any], problem: str, *, status_code: int) -> HTMLResponse:
        """The Space step as it was, after why the token pasted was not kept."""
        return _space_step(
            flow["user"],
            DriveOutcome(flow["drive"]),
            state=flow["state"],
            tokens_page=space_tokens.tokens_page,
            status_code=status_code,
            kept=await space_tokens.kept(flow["user"]),
            problem=problem,
        )

    async def after_skipping(user: str) -> str:
        """What the user's agent does in Twake Space once they skipped the Space step, or kept
        their token."""
        return SPACE_SKIPPED if await space_tokens.kept(user) is None else SPACE_KEPT

    @routes.post(SPACE_FORM)
    async def space_step(
        request: Request,
        started: Annotated[str | None, Cookie(alias=COOKIE)] = None,
    ) -> Response:
        """Where the user pastes their Space token, or skips the Space step."""
        flow = in_progress(started)
        if isinstance(flow, HTMLResponse):
            return flow
        retry_url = _retry_url(flow.get("owner"), flow.get("app"))
        form = await _form(request)
        if flow.get("step") != SPACE_STEP or (
            form is not None and not _same_state(flow, form.get("state"))
        ):
            return _refused(NOT_IN_PROGRESS, retry_url=retry_url)
        if form is None:
            # A form that big holds no token, and its state goes unread: the step stays as it was
            return await step_again(flow, NOT_A_TOKEN, status_code=400)
        user, owner, drive = flow["user"], flow.get("owner"), DriveOutcome(flow["drive"])
        if "skip" in form:
            return _last_page(user, owner, drive, status_code=200, space=await after_skipping(user))
        if "remove" in form:
            await space_tokens.remove(user)
            return _last_page(user, owner, drive, status_code=200, space=SPACE_REMOVED)
        try:
            held = await space_tokens.paste(user, form.get("space_token", ""))
        except NotASpaceToken:
            return await step_again(flow, NOT_A_TOKEN, status_code=400)
        except SpaceRefused:
            return await step_again(
                flow,
                f"{NOT_KEPT} Twake Space ne le reconnaît pas. Copiez-le en entier, ou créez-en un"
                " autre s'il a été révoqué ou s'il a expiré.",
                status_code=400,
            )
        except TooBroad:
            return await step_again(
                flow,
                f"{NOT_KEPT} il permet « Gérer les jetons » : s'il fuyait, il servirait à en créer"
                " d'autres. Créez-en un autre, avec Jetons d'API « Aucun accès ».",
                status_code=400,
            )
        except ScopesMissing as missing:
            return await step_again(
                flow,
                f"{NOT_KEPT} il faut au moins « Lire les espaces » et « Lire les fils »."
                f" {_allows(missing.held)}",
                status_code=400,
            )
        except NotTheirs:
            return await step_again(
                flow,
                f"{NOT_KEPT} c'est un jeton d'organisation ou celui d'un autre compte : dans au"
                " moins un des espaces qu'il atteint, il n'agit pas en votre nom. Créez le vôtre"
                f" dans Twake Space, connecté en tant que {user}.",
                status_code=400,
            )
        except SpaceUnavailable as unavailable:
            logger.warning("Twake Space checked no token of %s: %s", user, unavailable)
            return await step_again(
                flow,
                f"{NOT_KEPT} Twake Space n'a pas pu le vérifier. Réessayez dans un moment.",
                status_code=502,
            )
        except DelegationMissing:
            return _refused("Cette autorisation n'est plus en cours.", retry_url=retry_url)
        return _last_page(
            user, owner, drive, status_code=200, space=f"{SPACE_PASTED} {_allows(held)}"
        )

    return routes
