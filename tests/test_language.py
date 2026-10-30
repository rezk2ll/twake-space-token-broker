import json
from dataclasses import replace
from importlib.resources import files

import pytest
from httpx import AsyncClient

from tests.conftest import MMAUDET, SPACE_URL, SPACE_WEB_URL, consent
from tests.fake_lemonldap import FakeLemonLDAP
from tests.fake_space import FakeSpace
from tests.test_space import EVERY_SCOPE, paste
from twake_space_token_broker.settings import Settings
from twake_space_token_broker.words import Language, chosen


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """The broker reaches Twake Space, so that consents end on the Space step."""
    return replace(settings, space_url=SPACE_URL, space_web_url=SPACE_WEB_URL)


@pytest.mark.parametrize(
    ("accept_language", "language"),
    [
        ("en-US,en;q=0.9", Language.EN),
        ("fr-FR,fr;q=0.9,en;q=0.8", Language.FR),
        ("ja-JP,en;q=0.5,fr;q=0.7", Language.FR),
        ("de-DE,de;q=0.9", Language.DE),
        ("vi", Language.VI),
        ("ja-JP,ja;q=0.9", Language.EN),
        ("fr;q=0", Language.EN),
        (None, Language.EN),
    ],
)
def test_the_pages_speak_the_language_the_browser_prefers_else_english(
    accept_language: str | None, language: Language
) -> None:
    assert chosen(None, None, accept_language) is language


def test_the_language_asked_or_kept_comes_before_the_browsers() -> None:
    assert chosen("en", "fr", "fr") is Language.EN
    assert chosen(None, "en", "fr") is Language.EN
    assert chosen("ja", None, "fr") is Language.FR


def _shape(words: object) -> object:
    if isinstance(words, dict):
        return {key: _shape(value) for key, value in words.items()}
    if isinstance(words, list):
        return len(words)
    return None


@pytest.mark.parametrize("language", list(Language))
def test_every_language_names_what_english_names(language: Language) -> None:
    locales = files("twake_space_token_broker") / "locales"
    english = json.loads((locales / "en.json").read_text())
    words = json.loads((locales / f"{language}.json").read_text())

    assert _shape(words) == _shape(english)


async def test_a_browser_that_prefers_english_reads_the_space_step_in_english(
    client: AsyncClient, lemonldap: FakeLemonLDAP
) -> None:
    client.headers["Accept-Language"] = "en-GB,en;q=0.9"

    step = await consent(client, lemonldap, MMAUDET)

    assert '<html lang="en">' in step.text
    assert "<title>Your assistant in your spaces - Twake Space</title>" in step.text
    assert "“All my spaces, including the ones I join later”" in step.text
    assert "Skip this step" in step.text


async def test_the_language_asked_in_the_link_outlasts_the_trip_through_lemonldap(
    client: AsyncClient, lemonldap: FakeLemonLDAP
) -> None:
    client.headers["Accept-Language"] = "en"
    started = await client.get("/consent?lang=fr")

    step = await client.get(lemonldap.sign_in(started.headers["location"], MMAUDET))

    assert '<html lang="fr">' in step.text
    assert "Passer cette étape" in step.text


async def test_the_user_reads_the_page_again_in_another_language(
    client: AsyncClient, lemonldap: FakeLemonLDAP, space: FakeSpace
) -> None:
    step = await consent(client, lemonldap, MMAUDET)
    pasted = await paste(client, step, space.create_token(*EVERY_SCOPE))
    assert "<title>Votre assistant est autorisé - Twake Space</title>" in pasted.text
    assert 'href="/consent/page?lang=en"' in pasted.text

    english = await client.get("/consent/page?lang=en")

    assert english.status_code == 200
    assert '<html lang="en">' in english.text
    assert "<title>Your assistant is authorized - Twake Space</title>" in english.text
    assert "This token allows: “Read spaces”, “Read feeds”" in english.text
    assert (
        '<a href="/consent/page?lang=en" hreflang="en" lang="en" aria-current="true">English'
        in english.text
    )
    # The choice holds for the next consent too
    assert "Passer cette étape" not in (await consent(client, lemonldap, MMAUDET)).text


async def test_the_space_step_read_again_in_english_still_takes_the_token(
    client: AsyncClient, lemonldap: FakeLemonLDAP, space: FakeSpace
) -> None:
    step = await consent(client, lemonldap, MMAUDET)

    english = await client.get("/consent/page?lang=en")
    pasted = await paste(client, english, space.create_token(*EVERY_SCOPE))

    assert "Skip this step" in english.text
    assert "<title>Your assistant is authorized - Twake Space</title>" in pasted.text
    assert step.status_code == 200


async def test_a_german_page_writes_its_lists_and_dates_in_german(
    client: AsyncClient, lemonldap: FakeLemonLDAP, space: FakeSpace
) -> None:
    step = await consent(client, lemonldap, MMAUDET)
    await paste(client, step, space.create_token(*EVERY_SCOPE))

    german = await client.get("/consent/page?lang=de")

    assert '<html lang="de">' in german.text
    assert "<title>Ihr Assistent ist autorisiert - Twake Space</title>" in german.text
    assert "„Spaces lesen“, „Feeds lesen“, „Mitglieder verwalten“ und „Spaces ändern“" in (
        german.text
    )


async def test_with_no_page_shown_the_user_learns_no_consent_is_in_progress(
    client: AsyncClient,
) -> None:
    response = await client.get("/consent/page?lang=en")

    assert response.status_code == 400
    assert "No authorization is in progress in this browser." in response.text
    assert 'href="/consent"' in response.text
