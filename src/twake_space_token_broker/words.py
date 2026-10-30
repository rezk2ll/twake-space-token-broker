"""The words of the consent pages, one file per language in locales/, English the source.

The steps of the Space token quote Twake Space's page of API tokens, in the language of the page.
"""

import json
from dataclasses import dataclass
from enum import StrEnum
from importlib.resources import files


class Language(StrEnum):
    EN = "en"
    FR = "fr"
    DE = "de"
    ES = "es"
    IT = "it"
    RU = "ru"
    VI = "vi"


DEFAULT = Language.EN
"""The language of a browser that asks for none of them."""

NAMES = {
    Language.EN: "English",
    Language.FR: "Français",
    Language.DE: "Deutsch",
    Language.ES: "Español",
    Language.IT: "Italiano",
    Language.RU: "Русский",
    Language.VI: "Tiếng Việt",
}
"""Each language, named in itself."""


@dataclass(frozen=True)
class Words:
    language: Language
    quote: str
    """How the pages quote a label of Twake Space, with {} for the label."""
    language_label: str
    retry: str
    close: str
    refused: str
    authorized: str
    authorized_but_drive: str
    other_account: str
    other_account_text: str
    space_title: str
    space_intro: str
    space_kept: str
    space_kept_choice: str
    steps_open: list[str]
    """The first step, before, in and after the link to Space's page of API tokens."""
    steps: list[str]
    scopes_needed: str
    token: str
    save: str
    skip: str
    replace: str
    keep: str
    remove: str
    may_act: dict[str, str]
    """What the agent may do once the consent ends, by how the Drive step ended."""
    without_drive: dict[str, str]
    """Why a consent ends without Drive, by how the Drive step ended."""
    space_after: dict[str, str]
    """What the agent does in Twake Space once the user left the Space step."""
    allows_none: str
    allows_all: str
    allows_some: str
    scopes: dict[str, str]
    """The scopes the agent uses, each named as Space's list of API tokens names it."""
    reasons: dict[str, str]
    """Why a consent did not go through."""
    problems: dict[str, str]
    """Why the Space token pasted was not kept."""


def _load(language: Language) -> Words:
    locale = files("twake_space_token_broker") / "locales" / f"{language}.json"
    return Words(language=language, **json.loads(locale.read_text()))


WORDS = {language: _load(language) for language in Language}


def _weights(accept_language: str) -> dict[Language, float]:
    weights: dict[Language, float] = {}
    for entry in accept_language.split(","):
        tag, _, params = entry.strip().partition(";")
        try:
            language = Language(tag.strip().split("-")[0].lower())
        except ValueError:
            continue
        weight = 1.0
        for param in params.split(";"):
            name, _, value = param.strip().partition("=")
            if name == "q":
                try:
                    weight = float(value)
                except ValueError:
                    weight = 0.0
        weights[language] = max(weights.get(language, 0.0), weight)
    return weights


def chosen(asked: str | None, kept: str | None, accept_language: str | None) -> Language:
    """The language of the pages: the one asked in the link, else the one the browser keeps from
    the user's choice, else the one it prefers among those of the pages, else English."""
    for value in (asked, kept):
        if value is not None and value in Language:
            return Language(value)
    weights = {
        language: weight
        for language, weight in _weights(accept_language or "").items()
        if weight > 0
    }
    return max(weights, key=lambda language: weights[language]) if weights else DEFAULT
