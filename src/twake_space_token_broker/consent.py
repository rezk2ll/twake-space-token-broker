"""The consent link, through which a user lets their agent act for them.

The consent runs in steps: the user signs in to LemonLDAP, then, when LemonLDAP names their Drive
instance, lets the broker in on that instance, both coming back to /callback. When the broker
reaches Twake Space, the consent ends on the Space step, whose form posts the API token the user
pastes. A link bound to its owner checks that the account that signed in is theirs. The owner's
link for Space goes from LemonLDAP straight to the Space step, and leaves their Drive delegation as
it was.

Its pages are in the languages of words.Language. The browser keeps the page it shows last,
signed, so that the user may read it again in another language.
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

from babel.dates import format_date
from babel.lists import format_list
from fastapi import APIRouter, Cookie, Depends, Request
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
from twake_space_token_broker.words import NAMES, WORDS, Language, Words, chosen

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

PAGE_COOKIE = "twake_space_page"
"""The cookie of the page the broker showed last, which the user may read in another language."""

LANGUAGE_COOKIE = "twake_space_lang"
"""The cookie of the language the user chose, which outlasts the trip through LemonLDAP."""

LANGUAGE_KEPT = 365 * 24 * 3600

PAGE = "/consent/page"
"""The page the broker showed last, in the language its link asks for."""

DRIVE_STEP = "drive"
"""The step of a consent in progress whose user is on their Drive instance."""

SPACE_STEP = "space"
"""The step of a consent in progress whose user pastes an API token of Twake Space."""

SPACE_FORM = "/consent/space"
"""Where the form of the Space step posts, relative to the broker."""

FORM_LIMIT = 4096
"""Bytes the form of the Space step may hold: many times a token, which takes about fifty."""


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
    DriveOutcome.DECLINED: 200,
    DriveOutcome.REFUSED: 400,
    DriveOutcome.UNAVAILABLE: 502,
}
"""The Drive steps a consent ends without Drive on, and the status of its page."""


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

View = dict[str, Any]
"""What a page shows, apart from its language: its kind, and what that kind of page names."""


def _languages(words: Words) -> str:
    """The menu of the languages of the page, each a link to the page in it, the one shown marked
    current."""
    links = "".join(
        f'\n<li><a href="{PAGE}?lang={language}" hreflang="{language}" lang="{language}"'
        f"{' aria-current="true"' if language is words.language else ''}>"
        f"{NAMES[language]}</a></li>"
        for language in Language
    )
    return f"""<nav aria-label="{escape(words.language_label)}">
<details class="languages">
<summary>{NAMES[words.language]}</summary>
<ul class="u-m-0 u-pv-half u-ph-0 u-bdrs-4 u-elevation-8">{links}
</ul>
</details>
</nav>"""


def _page(
    words: Words,
    title: str,
    *paragraphs: str,
    tone: Tone | None = None,
    problem: str | None = None,
    retry_url: str | None = None,
    more: str = "",
) -> str:
    """A short page in the look of Twake, in the language of its words.

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
        f"{escape(words.retry)}</a></p>"
        if retry_url
        else ""
    )
    width = "page page-wide" if 'class="how' in more else "page"
    return f"""<!doctype html>
<html lang="{words.language}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} - Twake Space</title>
<style>
{STYLE}</style>
</head>
<body class="u-m-0 u-pv-1-half u-ph-1">
<header class="{width} u-flex u-flex-items-center u-flex-justify-between u-mh-auto u-mb-1">
<div class="brand u-flex u-flex-items-center" role="img" aria-label="Twake">{BRAND}</div>
{_languages(words)}
</header>
<main class="{width} u-mh-auto u-p-2 u-p-1-half-s u-elevation-1">{status}
<h1 class="u-mt-0 u-mb-1">{escape(title)}</h1>{alert}{text}{more}{link}
</main>
</body>
</html>
"""


def _labels(scopes: Collection[str], words: Words) -> str:
    """The scopes, among those the agent uses, named and ordered as Space's list of tokens does."""
    return format_list(
        [words.quote.format(label) for scope, label in words.scopes.items() if scope in scopes],
        locale=words.language,
    )


def _allows(held: Collection[str], words: Words) -> str:
    """What a token allows the agent, and what it does not, in the words of Space."""
    lacking = SCOPES.keys() - set(held)
    if not held:
        return words.allows_none.format(lacking=_labels(lacking, words))
    if not lacking:
        return words.allows_all.format(held=_labels(held, words))
    return words.allows_some.format(held=_labels(held, words), lacking=_labels(lacking, words))


def _day(moment: datetime, words: Words) -> str:
    """The day of the moment in UTC, written out in the language of the words."""
    return format_date(moment.astimezone(UTC).date(), format="long", locale=words.language)


def _may_act(user: str, drive: str, words: Words) -> str:
    """What the user's agent may now do, Drive included or not, and why not."""
    if drive in WITHOUT_DRIVE:
        return words.may_act["without"].format(user=user, reason=words.without_drive[drive])
    return words.may_act[drive].format(user=user)


def _refused(reason: str, *, retry_url: str) -> View:
    return {"kind": "refused", "reason": reason, "retry": retry_url}


def _wrong_account(owner: str, signed_in: str, *, retry_url: str) -> View:
    """The browser is signed in as another account than the one the link is for."""
    return {"kind": "other_account", "owner": owner, "signed_in": signed_in, "retry": retry_url}


def _last_page(
    user: str,
    owner: str | None,
    drive: DriveOutcome,
    *,
    space: str | None = None,
    held: Collection[str] = (),
) -> View:
    """The last page of a consent LemonLDAP granted: what the user's agent may now do, and in
    Twake Space too once they went through the Space step, with the scopes it holds when they
    pasted a token."""
    return {
        "kind": "last",
        "user": user,
        "drive": drive,
        "space": space,
        "held": sorted(held),
        "retry": _retry_url(owner),
    }


def _space_step(
    user: str,
    drive: DriveOutcome,
    *,
    state: str,
    tokens_page: str,
    kept: KeptToken | None,
    problem: str | None = None,
    held: Collection[str] = (),
) -> View:
    """The Space step: after why the token pasted was not kept, if it was not, with the scopes it
    held when too few."""
    return {
        "kind": "space",
        "user": user,
        "drive": drive,
        "state": state,
        "tokens_page": tokens_page,
        "kept": None
        if kept is None
        else {"pasted_at": kept.pasted_at.isoformat(), "scopes": sorted(kept.scopes)},
        "problem": problem,
        "held": sorted(held),
    }


def _space_html(view: View, words: Words) -> str:
    """Which API token to create in Twake Space, in the words of its page, and where to paste it.

    With a token kept, the step says when it was pasted and what it allows, and offers to keep it,
    to replace it or to remove it.
    """
    kept = view["kept"]
    steps = "".join(f"\n<li>{escape(step)}</li>" for step in words.steps)
    if kept is None:
        paragraphs = [words.space_intro]
        buttons = f"""<button class="button" type="submit">{escape(words.save)}</button>
<button class="button button-outlined" type="submit" name="skip" value="yes"
formnovalidate>{escape(words.skip)}</button>"""
    else:
        pasted_at = datetime.fromisoformat(kept["pasted_at"])
        paragraphs = [
            words.space_kept.format(
                day=_day(pasted_at, words), allows=_allows(kept["scopes"], words)
            ),
            words.space_kept_choice,
        ]
        buttons = f"""<button class="button" type="submit">{escape(words.replace)}</button>
<button class="button button-outlined" type="submit" name="skip" value="yes"
formnovalidate>{escape(words.keep)}</button>
<button class="button button-danger u-ml-auto u-ml-0-s" type="submit" name="remove" value="yes"
formnovalidate>{escape(words.remove)}</button>"""
    problem = view["problem"]
    before, link, after = words.steps_open
    return _page(
        words,
        words.space_title,
        _may_act(view["user"], view["drive"], words),
        *paragraphs,
        problem=None
        if problem is None
        else words.problems[problem].format(user=view["user"], allows=_allows(view["held"], words)),
        more=f"""
<div class="how">
<ol class="steps u-mt-0 u-mb-1 u-p-1 u-pl-2-half u-bdrs-4">
<li>{escape(before)}<a href="{escape(view["tokens_page"])}">{escape(link)}</a>
{escape(after)}</li>{steps}
</ol>
<p class="secondary u-mt-0 u-mb-1 u-fz-small">{escape(words.scopes_needed)}</p>
<form class="u-mt-1 u-pt-1" method="post" action="{SPACE_FORM}">
<input type="hidden" name="state" value="{escape(view["state"])}">
<p class="u-m-0"><label class="u-db u-mb-half u-fz-small" for="space_token">
{escape(words.token)}</label>
<input class="u-w-100" id="space_token" name="space_token" type="text" autocomplete="off"
spellcheck="false" required></p>
<div class="actions u-flex u-flex-wrap u-flex-column-s u-mt-1">{buttons}</div>
</form>
</div>""",
    )


def _html(view: View, language: Language) -> str:
    """The page of the view, in the language given."""
    words = WORDS[language]
    kind = view["kind"]
    if kind == "space":
        return _space_html(view, words)
    if kind == "refused":
        return _page(
            words,
            words.refused,
            words.reasons[view["reason"]],
            tone=Tone.ERROR,
            retry_url=view["retry"],
        )
    if kind == "other_account":
        return _page(
            words,
            words.other_account,
            words.other_account_text.format(owner=view["owner"], signed_in=view["signed_in"]),
            tone=Tone.ERROR,
            retry_url=view["retry"],
        )
    space = view["space"]
    message = _may_act(view["user"], view["drive"], words)
    if space is not None:
        message += f" {words.space_after[space]}"
    if space == "pasted":
        message += f" {_allows(view['held'], words)}"
    if view["drive"] in WITHOUT_DRIVE:
        return _page(
            words,
            words.authorized_but_drive,
            message,
            tone=Tone.WARNING,
            retry_url=view["retry"],
        )
    return _page(words, words.authorized, f"{message} {words.close}", tone=Tone.SUCCESS)


def _language(request: Request) -> Language:
    return chosen(
        request.query_params.get("lang"),
        request.cookies.get(LANGUAGE_COOKIE),
        request.headers.get("accept-language"),
    )


def _keep_language(response: Response, request: Request, language: Language) -> None:
    """Keeps the language the link asked for, if it asked for one, beyond this consent."""
    if request.query_params.get("lang") == language:
        response.set_cookie(
            LANGUAGE_COOKIE,
            language,
            max_age=LANGUAGE_KEPT,
            httponly=True,
            secure=True,
            samesite="lax",
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

    def show(view: View, language: Language, *, status_code: int, ends: bool) -> HTMLResponse:
        """The page of the view, kept in the browser to be shown again in the other language.

        A page that ends the consent drops the consent in progress.
        """
        response = HTMLResponse(_html(view, language), status_code=status_code)
        response.set_cookie(
            PAGE_COOKIE,
            signer.sign({"view": view, "status": status_code}),
            max_age=COOKIE_KEPT,
            httponly=True,
            secure=True,
            samesite="lax",
        )
        if ends:
            response.delete_cookie(COOKIE)
        return response

    def refused(
        reason: str, language: Language, *, retry_url: str, status_code: int = 400
    ) -> HTMLResponse:
        return show(
            _refused(reason, retry_url=retry_url), language, status_code=status_code, ends=True
        )

    def last(view: View, language: Language, *, status_code: int = 200) -> HTMLResponse:
        return show(view, language, status_code=status_code, ends=True)

    def in_progress(started: str | None, language: Language) -> dict[str, Any] | HTMLResponse:
        """The consent the browser has in progress, or why the page that came back is refused."""
        flow = signer.verify(started) if started else None
        if flow is None:
            return refused("none_in_progress", language, retry_url=CONSENT)
        if clock() >= flow["expires"]:
            return refused(
                "expired",
                language,
                retry_url=_retry_url(flow.get("owner"), flow.get("app")),
            )
        return flow

    @routes.get(CONSENT)
    async def consent(
        request: Request,
        language: Annotated[Language, Depends(_language)],
        owner: str | None = None,
        app: str | None = None,
    ) -> RedirectResponse:
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
        _keep_language(response, request, language)
        return response

    @routes.get(PAGE)
    async def page(
        request: Request,
        language: Annotated[Language, Depends(_language)],
        shown: Annotated[str | None, Cookie(alias=PAGE_COOKIE)] = None,
    ) -> HTMLResponse:
        """The page the broker showed last, in the language the link asks for: the consent in
        progress, if any, stays as it was."""
        kept = signer.verify(shown) if shown else None
        view, status_code = (
            (kept["view"], kept["status"])
            if kept is not None
            else (_refused("none_in_progress", retry_url=CONSENT), 400)
        )
        response = HTMLResponse(_html(view, language), status_code=status_code)
        _keep_language(response, request, language)
        return response

    @routes.get("/callback")
    async def callback(
        language: Annotated[Language, Depends(_language)],
        code: str | None = None,
        state: str | None = None,
        started: Annotated[str | None, Cookie(alias=COOKIE)] = None,
    ) -> Response:
        flow = in_progress(started, language)
        if isinstance(flow, HTMLResponse):
            return flow
        retry_url = _retry_url(flow.get("owner"), flow.get("app"))
        step = flow.get("step")
        if step == DRIVE_STEP:
            return await drive_callback(flow, code, state, language)
        # No step comes back here but LemonLDAP's and Drive's: the Space step posts its form
        if step is not None or not _same_state(flow, state):
            return refused("not_in_progress", language, retry_url=retry_url)
        if code is None:
            return refused("lemonldap_declined", language, retry_url=retry_url)
        try:
            signed_in = await lemonldap.redeem(code, flow["verifier"])
        except GrantRefused:
            return refused("lemonldap_refused", language, retry_url=retry_url)
        except LemonLDAPUnavailable as unavailable:
            logger.warning("LemonLDAP completed no consent: %s", unavailable)
            return refused("lemonldap_unavailable", language, retry_url=retry_url, status_code=502)
        except OfflineAccessDenied:
            logger.warning("LemonLDAP granted no offline access: check the client's options")
            return refused("offline_access_denied", language, retry_url=retry_url, status_code=502)
        owner = flow.get("owner")
        if owner is not None and signed_in.user != owner:
            # A browser still signed in as someone else, as LemonLDAP's "stay connected" keeps it:
            # nothing is stored, and that account's own delegations stay as they were
            logger.warning(
                "A consent for %s came back signed in as %s: nothing stored", owner, signed_in.user
            )
            return last(
                _wrong_account(owner, signed_in.user, retry_url=retry_url),
                language,
                status_code=403,
            )
        # The owner may come for their Space token alone while their delegation holds: once they
        # revoked it, the broker keeps its Drive delegation only to leave the instance
        space_alone = (
            flow.get("app") == SPACE_APP
            and await access_tokens.consented_at(signed_in.user) is not None
        )
        await access_tokens.consented(signed_in)
        if space_alone:
            return await end(signed_in.user, owner, DriveOutcome.UNCHANGED, language, app=SPACE_APP)
        # The new consent replaces the whole delegation: Drive comes back only if granted again,
        # while the Space token stays, as the owner pasted it in Space's stead
        await drive_tokens.forget_even_if_unavailable(signed_in.user)
        return await to_drive(signed_in, owner, language)

    async def to_drive(signed_in: SignedIn, owner: str | None, language: Language) -> Response:
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
            return await end(signed_in.user, owner, DriveOutcome.ABSENT, language)
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
            return await end(signed_in.user, owner, DriveOutcome.UNAVAILABLE, language)
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

    async def drive_callback(
        flow: dict[str, Any], code: str | None, state: str | None, language: Language
    ) -> Response:
        """Where the user's Drive instance sends them back."""
        user = flow["user"]
        owner = flow.get("owner")
        if code is None:
            # The instance's page links back with no state when the user declines: the answer
            # changes nothing the broker keeps
            return await end(user, owner, DriveOutcome.DECLINED, language)
        retry_url = _retry_url(owner)
        if not _same_state(flow, state):
            return refused("not_in_progress", language, retry_url=retry_url)
        try:
            await drive_tokens.consented(user, code, flow["verifier"])
        except DelegationMissing:
            return refused("no_longer_in_progress", language, retry_url=retry_url)
        except InstanceRefused:
            return await end(user, owner, DriveOutcome.REFUSED, language)
        except InstanceUnavailable as unavailable:
            logger.warning("The Drive instance of %s completed no consent: %s", user, unavailable)
            return await end(user, owner, DriveOutcome.UNAVAILABLE, language)
        return await end(user, owner, DriveOutcome.GRANTED, language)

    async def end(
        user: str,
        owner: str | None,
        drive: DriveOutcome,
        language: Language,
        *,
        app: str | None = None,
    ) -> Response:
        """Where every consent LemonLDAP granted ends, whether Drive was granted, absent, failed
        or left as it was: on the Space step, when the broker reaches Twake Space.

        The page bears the status of the Drive step. A consent for an app starts over from the
        owner's link for that app.
        """
        status_code = WITHOUT_DRIVE.get(drive, 200)
        if space_tokens is None:
            return last(_last_page(user, owner, drive), language, status_code=status_code)
        state = secrets.token_urlsafe(32)
        response = show(
            _space_step(
                user,
                drive,
                state=state,
                tokens_page=space_tokens.tokens_page,
                kept=await space_tokens.kept(user),
            ),
            language,
            status_code=status_code,
            ends=False,
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

    async def step_again(
        flow: dict[str, Any],
        problem: str,
        language: Language,
        *,
        status_code: int,
        held: Collection[str] = (),
    ) -> HTMLResponse:
        """The Space step as it was, after why the token pasted was not kept."""
        return show(
            _space_step(
                flow["user"],
                DriveOutcome(flow["drive"]),
                state=flow["state"],
                tokens_page=space_tokens.tokens_page,
                kept=await space_tokens.kept(flow["user"]),
                problem=problem,
                held=held,
            ),
            language,
            status_code=status_code,
            ends=False,
        )

    async def after_skipping(user: str) -> str:
        """What the user's agent does in Twake Space once they skipped the Space step, or kept
        their token."""
        return "skipped" if await space_tokens.kept(user) is None else "kept"

    @routes.post(SPACE_FORM)
    async def space_step(
        request: Request,
        language: Annotated[Language, Depends(_language)],
        started: Annotated[str | None, Cookie(alias=COOKIE)] = None,
    ) -> Response:
        """Where the user pastes their Space token, or skips the Space step."""
        flow = in_progress(started, language)
        if isinstance(flow, HTMLResponse):
            return flow
        retry_url = _retry_url(flow.get("owner"), flow.get("app"))
        form = await _form(request)
        if flow.get("step") != SPACE_STEP or (
            form is not None and not _same_state(flow, form.get("state"))
        ):
            return refused("not_in_progress", language, retry_url=retry_url)
        if form is None:
            # A form that big holds no token, and its state goes unread: the step stays as it was
            return await step_again(flow, "not_a_token", language, status_code=400)
        user, owner, drive = flow["user"], flow.get("owner"), DriveOutcome(flow["drive"])
        if "skip" in form:
            return last(_last_page(user, owner, drive, space=await after_skipping(user)), language)
        if "remove" in form:
            await space_tokens.remove(user)
            return last(_last_page(user, owner, drive, space="removed"), language)
        try:
            held = await space_tokens.paste(user, form.get("space_token", ""))
        except NotASpaceToken:
            return await step_again(flow, "not_a_token", language, status_code=400)
        except SpaceRefused:
            return await step_again(flow, "refused", language, status_code=400)
        except TooBroad:
            return await step_again(flow, "too_broad", language, status_code=400)
        except ScopesMissing as missing:
            return await step_again(
                flow, "scopes_missing", language, status_code=400, held=missing.held
            )
        except NotTheirs:
            return await step_again(flow, "not_theirs", language, status_code=400)
        except SpaceUnavailable as unavailable:
            logger.warning("Twake Space checked no token of %s: %s", user, unavailable)
            return await step_again(flow, "unavailable", language, status_code=502)
        except DelegationMissing:
            return refused("no_longer_in_progress", language, retry_url=retry_url)
        return last(_last_page(user, owner, drive, space="pasted", held=held), language)

    return routes
