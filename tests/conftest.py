import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

import psycopg
import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient, Response
from psycopg import sql
from psycopg.conninfo import make_conninfo
from testcontainers.community.postgres import PostgresContainer

from tests.fake_cozy_stack import FakeCozyStack
from tests.fake_lemonldap import FakeLemonLDAP
from tests.fake_space import FakeSpace
from twake_space_token_broker.app import create_app
from twake_space_token_broker.settings import Settings

ISSUER = "https://sign-up.example.test/"
PUBLIC_BASE_URL = "https://agent-consent.example.test"
CLIENT_ID = "twake-space-agents"
CLIENT_SECRET = "client-secret-for-tests"

MMAUDET = "mmaudet@example.test"
ALICE = "alice@example.test"
DRIVE_INSTANCE_DOMAIN = "twake.example.test"
"""The domain of the users' Drive instances."""
MMAUDET_DRIVE = f"mmaudet.{DRIVE_INSTANCE_DOMAIN}"
"""The host of MMAUDET's Drive instance."""

SPACE_API = "space-api.example.test"
"""The host of Twake Space's API, which the broker reaches inside the cluster."""
SPACE_URL = f"http://{SPACE_API}"
SPACE_WEB_URL = "https://space.example.test"
"""Where users open Twake Space."""


class FakeClock:
    """The time the broker reads, in seconds since the epoch, moved on by the test only."""

    def __init__(self) -> None:
        self.now = 1_790_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(scope="session")
def postgres() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:18-alpine", driver=None) as postgres:
        yield postgres


@pytest.fixture
def database_url(postgres: PostgresContainer) -> Iterator[str]:
    """An empty database of its own for each test."""
    server = postgres.get_connection_url()
    name = f"broker_{uuid.uuid4().hex}"
    with psycopg.connect(server, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    yield make_conninfo(server, dbname=name)
    with psycopg.connect(server, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        encryption_key=bytes(range(32)),
        public_base_url=PUBLIC_BASE_URL,
        drive_instance_domain=DRIVE_INSTANCE_DOMAIN,
    )


@pytest.fixture
def lemonldap(clock: FakeClock) -> FakeLemonLDAP:
    return FakeLemonLDAP(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        let_time_pass=clock.advance,
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def cozy_stack() -> FakeCozyStack:
    return FakeCozyStack()


@pytest.fixture
def space() -> FakeSpace:
    return FakeSpace(SPACE_API)


@pytest.fixture
def drive(lemonldap: FakeLemonLDAP, cozy_stack: FakeCozyStack) -> str:
    """MMAUDET's Drive instance, which LemonLDAP names in workplaceFqdn."""
    cozy_stack.create_instance(MMAUDET_DRIVE)
    lemonldap.workplaces[MMAUDET] = MMAUDET_DRIVE
    return MMAUDET_DRIVE


@asynccontextmanager
async def running(
    settings: Settings,
    lemonldap: FakeLemonLDAP,
    clock: FakeClock,
    cozy_stack: FakeCozyStack | None = None,
    space: FakeSpace | None = None,
) -> AsyncIterator[AsyncClient]:
    """The broker, from its start to its stop, for a browser that prefers French."""
    app = create_app(
        settings,
        lemonldap_transport=lemonldap.transport,
        cozy_stack_transport=(cozy_stack or FakeCozyStack()).transport,
        space_transport=(space or FakeSpace(SPACE_API)).transport,
        clock=clock,
    )
    async with (
        LifespanManager(app) as manager,
        AsyncClient(
            transport=ASGITransport(app=manager.app),
            base_url=PUBLIC_BASE_URL,
            headers={"Accept-Language": "fr"},
        ) as client,
    ):
        yield client


@pytest.fixture
async def client(
    settings: Settings,
    lemonldap: FakeLemonLDAP,
    clock: FakeClock,
    cozy_stack: FakeCozyStack,
    space: FakeSpace,
) -> AsyncIterator[AsyncClient]:
    async with running(settings, lemonldap, clock, cozy_stack, space) as client:
        yield client


async def consent(client: AsyncClient, lemonldap: FakeLemonLDAP, user: str) -> Response:
    """The user opens the consent link in their own browser and signs in."""
    client.cookies.clear()
    started = await client.get("/consent")
    return await client.get(lemonldap.sign_in(started.headers["location"], user))


async def consent_with_drive(
    client: AsyncClient, lemonldap: FakeLemonLDAP, cozy_stack: FakeCozyStack, user: str
) -> Response:
    """The user consents, then accepts on their Drive instance, where the consent took them."""
    to_drive = await consent(client, lemonldap, user)
    assert to_drive.status_code == 302
    return await client.get(cozy_stack.authorize(to_drive.headers["location"]))


def as_agent_of(user: str) -> dict[str, str]:
    """The header APISIX sets on a forward-auth request, naming the agent's owner."""
    return {"X-Twake-User-Email": user}


async def remove_delegation(database_url: str, user: str) -> None:
    """An operator deletes the user's delegation from the broker's database."""
    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        await connection.execute("DELETE FROM delegations WHERE user_email = %s", (user,))


async def as_left_by_an_earlier_broker(database_url: str, *, changed_at: str) -> None:
    """The database as a broker of before consent dates and revocations left it, its delegations
    last changed at changed_at, in RFC 3339."""
    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        await connection.execute("ALTER TABLE delegations DROP COLUMN consented_at")
        await connection.execute("ALTER TABLE delegations ALTER COLUMN refresh_token SET NOT NULL")
        await connection.execute(
            "UPDATE delegations SET updated_at = %s::timestamptz", (changed_at,)
        )


async def database_dump(database_url: str) -> str:
    """Every row of every table, as PostgreSQL prints them."""
    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        tables = await connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
        rows: list[str] = []
        for (table,) in await tables.fetchall():
            select = sql.SQL("SELECT t::text FROM {} AS t").format(sql.Identifier(table))
            rows.extend(row for (row,) in await (await connection.execute(select)).fetchall())
        return "\n".join(rows)
