# twake-space-token-broker

Keeps Twake Space users' delegated tokens for their personal agents.

A personal agent never holds a credential. Its owner consents once, through their own consent link, and the broker keeps the owner's LemonLDAP refresh token, encrypted. When the agent calls a contract through APISIX, APISIX asks the broker for a fresh access token of the agent's owner, through forward-auth, and passes it on to the contract.

Twake Drive accepts only tokens of the owner's own instance, a cozy-stack. The same consent also lets the broker in on that instance, and the routes of Drive contracts get a token of it as well.

Twake Space takes the API tokens its users create. The consent ends on a step where the owner pastes one, and the routes of Space contracts get it as well.

## Consent

| Request | Answer |
|---|---|
| `GET /consent?owner=<email>` | the consent link bound to its owner, as the problems below give it: a redirect to LemonLDAP |
| `GET /consent?owner=<email>&app=space` | the owner's link for their Space token, as `space_token_missing` gives it: a redirect to LemonLDAP, then straight to the Space step |
| `GET /consent` | the plain consent link, the same for every user: a redirect to LemonLDAP |
| `GET /callback` | where LemonLDAP, then the user's Drive instance, send the user back: a short page in French |
| `POST /consent/space` | where the Space step posts the token the user pasted, or skips it: a short page in French |

- The consent signs the user in with the `twake-space-agents` client: an authorization code with PKCE (S256) and the scope `openid email offline_access`. The state and the PKCE verifier wait in a signed, HttpOnly cookie. The consent lasts 10 minutes, but the browser keeps the cookie an hour, so that an expired consent still starts over from the owner's link.
- A browser holds one consent at a time: opening a consent link again while one is in progress replaces it, and the first one then ends on a page saying it no longer matches the consent in progress.
- The callback checks the state, then exchanges the code with `client_secret_basic`. It stores the refresh token under the user's email, which is the `sub` of the ID token on Twake's LemonLDAP.
- The owner's link asks LemonLDAP for no new login and passes the owner as `login_hint`, so an owner already signed in consents in one go. The callback then checks that the account that signed in is the owner, exactly as the link names them, since delegations are keyed by that email. If it is not, it stores nothing, leaves that account's delegations as they were, and asks the user to open the link in a private window and sign in as the owner. That account keeps an offline session in LemonLDAP that nothing holds: LemonLDAP 2.21 cannot revoke it.
- The plain link asks for `prompt=login`, so LemonLDAP has the user sign in again when an SSO session is open, through its unstyled "Upgrade session" page. LemonLDAP's "stay connected" defeats it: its login counts as a fresh one, so a browser that remembers another account can still consent for that account unnoticed. Give users their own link.
- Consenting again replaces the stored token.
- When something fails, the page says why, with a link to start over: the owner's link again, when the consent started from it.

### Drive

- Once LemonLDAP's refresh token is stored, the callback reads the user's `workplaceFqdn` in LemonLDAP's userinfo: the host of their Drive instance. LemonLDAP must release it for the client, in JSON, under one of the scopes the broker asks for, such as `email`.
- The broker registers an OAuth client named `Assistant Twake Space` on that instance, by dynamic client registration (`POST /auth/register`), and sends the user to the instance's consent page for `io.cozy.files:GET,POST`, reading and creating files, with PKCE (S256). The client is listed among the applications connected to the instance, where the user can remove it.
- The instance sends the user back to `/callback` too, which tells the two steps apart by the signed cookie of the consent in progress. The callback checks the state, exchanges the code with the client's secret, and stores the instance's refresh token with the client.
- The user thus consents once, through the same link: they need not open anything else.
- The broker calls the host LemonLDAP names, so it accepts only an instance right under `DRIVE_INSTANCE_DOMAIN`, `<name>.<domain>`: an address, a service of the cluster or a host of another domain counts as no instance, and logs a warning. Without `DRIVE_INSTANCE_DOMAIN`, Drive is off.
- When LemonLDAP names no such instance, or cannot answer, the consent completes for LemonLDAP alone and the page says that Drive is not available. The consent never fails for Drive.
- When the user declines on their instance, or the instance fails, the page says that the agent may act for them except in Drive, and why, with the link to start over. LemonLDAP's part of the consent stays.
- Consenting again replaces the Drive delegation as well: the broker removes its earlier client from the instance, which voids that client's tokens, and Drive comes back only if the user lets the broker in again.

### Space

- With `SPACE_URL` set, every consent LemonLDAP granted ends on the Space step, after Drive when LemonLDAP names an instance. The page says whether the agent may act in Drive, and bears the status of the Drive step. It then links to Space's page of API tokens, `<SPACE_WEB_URL>/settings/api-tokens`, and names the token to create there in that page's words: « Assistant », Espaces « Écriture », Fil d'activité « Lecture », Membres « Écriture », Jetons d'API « Aucun accès », « Tous mes espaces, y compris ceux que je rejoindrai », and « 90 jours ».
- The user pastes the token in the page's form, or skips the step. The form posts to `/consent/space`, bound to the consent in progress by its signed cookie and a state of its own, within the consent's 10 minutes.
- The owner's link for Space goes from LemonLDAP straight to the Space step, and leaves their Drive delegation as it was: the page says so. It refuses another account as the owner's link does, and keeps LemonLDAP's new refresh token as any consent does. An owner whose delegation is missing or revoked goes through the whole consent instead, Drive included, so that a revoked Drive delegation, which the broker keeps until it has left the instance, does not come back.
- When a token is kept, the step says when it was pasted, as a day in UTC, and what it allows, from the scopes seen when it was pasted. It offers to keep it, which changes nothing, to replace it, or to remove it. A replacement goes through every check below, and the token kept stays when the new one is not kept. A token removed leaves the agent without Twake Space, and the page asks the owner to revoke it in Space as well.
- The broker keeps a token only if it starts with `tws_` and Space answers `GET /spaces` with it. It then asks Space which scopes the token holds, without changing anything: Space checks a route's scope before anything else, answering 403 `insufficient_scope` without it, and past that check answers 400 or 404 on a space that does not exist. The probes are `GET /spaces/<uuid>/feed` for `feed:read`, then `POST /spaces/<uuid>/members` and `PATCH /spaces/<uuid>`, both with `{}`, for `members:write` and `space:write`, on a random UUID, and `GET /tokens` for `tokens:write`, which lists an account's tokens and refuses an organization token with 403 `forbidden` past that check.
- A token with `tokens:write`, « Gérer les jetons », is not kept: were it to leak, it would create others. The page says so, and the user may paste another.
- A token without `space:read` or `feed:read` is not kept: the page says so, with the scopes the token holds and those it lacks in Space's words, and the user may paste another. A token without `members:write` or `space:write` is kept, and the page says what the agent cannot do with it.
- Space has no route that names whom a token acts for, so the broker reads it from the spaces the token reaches: `GET /spaces`, then `GET /spaces/<id>` for each. In each, the owner must be a member, under their email whatever its case, with the `role` Space gives the token there. A token of the owner's account passes, as it acts with the owner's role in each of their spaces. An organization token, which acts with a role of its own in every space of the organization, or another account's token, fails in a space where the owner is not a member with that role: it is not kept, and the page says it is an organization token or another account's. A token that reaches no space is kept, as nothing tells whose it is then.
- A token Space does not know, mistyped, revoked or expired, is not kept either (400), nor one Space cannot check (502, and a warning in the logs).
- Skipping the step leaves the agent without Twake Space, unless it keeps a token pasted at an earlier consent: a new consent keeps the Space token, and a token pasted again replaces it. Revoking the delegation forgets it.
- No Space token is ever logged, nor shown in a page, nor put in the cookie.
- Without `SPACE_URL`, the consent has no Space step and the broker serves no `/consent/space`.

## Forward-auth

`GET /forward-auth` is for APISIX's `forward-auth` plugin only.

- APISIX names the agent's owner by email in the `X-Twake-User-Email` header. It must remove any value the agent sends and set the header itself, from the agent's consumer.
- On success, the broker answers 200 with `Authorization: Bearer <access token>`. List `Authorization` in the plugin's `upstream_headers`.
- A Drive route appends `?token=drive` to the plugin's URI. The broker then answers 200 with three headers: `Authorization` as above, which names the user to the contract, `X-Twake-Drive-Token`, an access token of the owner's Drive instance with no scheme, and `X-Twake-Drive-Instance`, the instance's host. List the three in the plugin's `upstream_headers`: a 200 of a Drive route always sets them, so they replace any value the agent sent. Without the query, nothing changes.
- When the owner has no Drive delegation, a Drive route asks LemonLDAP's userinfo for their instance. If LemonLDAP names one under `DRIVE_INSTANCE_DOMAIN`, the answer is `delegation_missing` with the consent link, since a new consent lets the broker in. If not, it is `drive_instance_unknown`, without the link, which would only take the owner round in circles.
- A Space route appends `?token=space`. The broker then answers 200 with two headers: `Authorization` as above, and `X-Twake-Space-Token`, the API token the owner pasted, `tws_…`, with no scheme. List both in the plugin's `upstream_headers`. A Space route asks LemonLDAP first, so a delegation that is missing, expired or revoked gives no Space token either. When the owner has given their agent no Space token, the answer is `space_token_missing`. The broker reads the Space token from its database at each call. Without `SPACE_URL`, `?token=space` is `unknown_token`.
- Access tokens are kept in memory and handed out for `TOKEN_REUSE_SECONDS` at most (60 seconds by default) from when the broker asked LemonLDAP for them, and never later than five minutes before they expire, then refreshed. They last 10 hours by LemonLDAP's default (`oidcServiceAccessTokenExpiration`) and 15 minutes for `twake-space-agents` on dev (`oidcRPMetaDataOptionsAccessTokenExpiration`), but only a refresh shows that LemonLDAP no longer honours a delegation.
- Each refresh makes LemonLDAP look the user up and issue a new access token, so an agent in use costs a refresh a minute by default. An outage of LemonLDAP's LDAP directory or session store longer than `TOKEN_REUSE_SECONDS` shows to agents as `delegation_expired`, where the cached token used to hide it.
- Simultaneous calls of one agent share one refresh.
- If LemonLDAP ever returns a new refresh token, it replaces the stored one.
- Drive tokens are kept and refreshed alike, under the same `TOKEN_REUSE_SECONDS`: cozy-stack's last a week, but only a refresh shows that the owner removed the broker from their instance. A Drive route asks LemonLDAP first, so a delegation LemonLDAP no longer honours gives no Drive token either. A Drive delegation the instance refuses stays stored, like LemonLDAP's, until the next consent replaces it.
- The broker deletes no delegation that LemonLDAP refuses, because LemonLDAP 2.21 answers the same errors while its LDAP directory or session store fails, and removes the offline session itself only for a user it no longer finds. A delegation thus works again after an outage without a new consent. A deleted user's row stays, and each call of their agent asks LemonLDAP again and logs a warning.

Every error is an [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem (`application/problem+json`) with a stable `code`:

| Status | `code` | When |
|---|---|---|
| 400 | `missing_user_email` | the `X-Twake-User-Email` header is missing or empty |
| 400 | `unknown_token` | the `token` query asks for another token than `drive`, or `space` when `SPACE_URL` is set |
| 401 | `delegation_missing` | the owner never consented or revoked their delegation, or, on a Drive route, never let the broker in on the Drive instance LemonLDAP names |
| 401 | `delegation_expired` | LemonLDAP refuses the refresh token with `invalid_request` or `invalid_grant`, such as when the user was deleted, or their offline session expired (after 30 days by default) or was revoked; or, on a Drive route, the instance refuses its refresh token, such as once the owner removed the broker from it |
| 401 | `space_token_missing` | on a Space route, the owner has given their agent no Space token, or the one kept no longer decrypts |
| 404 | `drive_instance_unknown` | on a Drive route, the owner has no Drive delegation, and LemonLDAP names no instance under `DRIVE_INSTANCE_DOMAIN`, or that setting is unset |
| 502 | `lemonldap_unavailable` | LemonLDAP gave no usable answer, or answered another error |
| 502 | `drive_unavailable` | on a Drive route, the owner's Drive instance gave no usable answer |

- The 401 problems carry the consent link bound to the owner in `consent_url`, for the agent to send to them. That of `space_token_missing` also names the app, `app=space`.
- APISIX passes an error's status and body on to the agent. List `Content-Type` in the plugin's `client_headers` to keep `application/problem+json`.
- Routing errors, such as an unknown path, use the same format, with a `code` named after their HTTP status (`not_found`, `method_not_allowed`).

## Delegation

`GET /delegation` and `DELETE /delegation` are for the agent's harness, through APISIX only, which names the agent's owner in `X-Twake-User-Email` as for forward-auth: it must remove any value the agent sends and set the header itself.

| Request | Answer |
|---|---|
| `GET /delegation` | 200 with `consented_at`, when the owner consented, and `expires_at`, when their delegation expires, both in UTC as RFC 3339 writes them, and `consent_url`, the consent link bound to the owner |
| `DELETE /delegation` | 204 once the owner's delegation is revoked and the broker has left their Drive instance, also when there was nothing to revoke |

- A delegation dates from its owner's last consent, and lasts `DELEGATION_LIFETIME_SECONDS` from it, 30 days by default: the offline session of LemonLDAP's client. LemonLDAP 2.21 counts that session from the consent, and no refresh extends it, so `expires_at` holds until the owner consents again. Set the variable to the client's offline session lifetime when the client sets one, or else to LemonLDAP's `oidcServiceOfflineSessionExpiration`.
- `GET /delegation` gives the dates of an expired delegation too.
- `DELETE /delegation` revokes the delegation first, and forgets the owner's Space token with it: from then on the agent gets no token, and forward-auth answers `delegation_missing`. The broker then removes its client from the owner's Drive instance, which voids that client's tokens, and forgets the Drive delegation.
- When the instance does not answer, the delegation is revoked all the same, but the broker keeps its Drive client to remove it later, and answers 502 `drive_unavailable`: calling `DELETE /delegation` again removes the client once the instance answers.
- LemonLDAP 2.21 cannot revoke an offline session: the revoked refresh token stays valid at LemonLDAP until its session expires, held by no one.

| Status | `code` | When |
|---|---|---|
| 400 | `missing_user_email` | the `X-Twake-User-Email` header is missing or empty |
| 404 | `delegation_missing` | on `GET`, the owner never consented or revoked their delegation: the problem carries the consent link bound to them in `consent_url` |
| 502 | `drive_unavailable` | on `DELETE`, the owner's Drive instance gave no usable answer |

## Storage

The broker creates its tables, `delegations`, `drive_delegations` and `space_tokens`, at startup if they are missing. A row of `delegations` holds:

- the user's email;
- the refresh token, encrypted with AES-GCM and bound to that email, or none once the user revoked their delegation;
- the time of the user's consent;
- the time of its last change.

A revoked delegation stays, without its refresh token or Space token, only until the broker has removed its client from the user's Drive instance. A delegation kept by an earlier version of the broker takes its last change as the time of its consent: a refresh changed it only when LemonLDAP rotated its refresh token, which LemonLDAP does only for a client set to rotate its refresh tokens.

A row of `drive_delegations` holds the user's email, which refers to their row of `delegations`; the host of their Drive instance, the broker's client on it and the instance's refresh token, encrypted together the same way; and the time of its last change. Deleting a user's delegation deletes their Drive delegation with it.

A row of `space_tokens` holds the user's email, which refers to their row of `delegations`; the Space token they pasted, encrypted the same way; the scopes the broker saw it hold; when they pasted it; and the time of its last change. A new consent keeps it. Removing it on the Space step deletes it, as does revoking the user's delegation, at once.

The key of the tokens and the key of the consent cookie are both derived from `ENCRYPTION_KEY` with HKDF. A token that no longer decrypts, after a change of that key, counts as expired, so the next consent replaces it. A Space token that no longer decrypts counts as missing, so the next one pasted replaces it.

## Run

| Variable | Value |
|---|---|
| `DATABASE_URL` | the PostgreSQL database, owned by the broker's user, since the broker creates its tables |
| `OIDC_ISSUER` | LemonLDAP's issuer, such as `https://sign-up.dev.twake.lin-saas.com/` |
| `OIDC_CLIENT_ID` | `twake-space-agents` by default |
| `OIDC_CLIENT_SECRET` | the client's secret |
| `ENCRYPTION_KEY` | at least 32 random bytes, base64 encoded, such as from `openssl rand -base64 32` |
| `PUBLIC_BASE_URL` | where users reach the broker, such as `https://agent-consent.dev.twake.lin-saas.com` |
| `TOKEN_REUSE_SECONDS` | seconds an access token is handed out before LemonLDAP, or the Drive instance, is asked again, `60` by default: a revoked delegation still gets tokens for that long at most |
| `DELEGATION_LIFETIME_SECONDS` | seconds a delegation lasts from its consent, `2592000` (30 days) by default: the offline session lifetime of the LemonLDAP client, or LemonLDAP's default when the client sets none |
| `DRIVE_INSTANCE_DOMAIN` | the domain of the users' Drive instances, such as `dev.twake.lin-saas.com`: the broker lets itself in only on `<name>.<domain>`. Unset by default, which turns Drive off |
| `SPACE_URL` | Twake Space's API, under which the broker calls `/spaces` and `/tokens` to check the tokens users paste, such as Space's backend inside the cluster. Unset by default, which turns the Space step and `?token=space` off |
| `SPACE_WEB_URL` | where users open Twake Space, such as `https://space.dev.twake.lin-saas.com`: the Space step links to its page of API tokens. Required with `SPACE_URL` |

```sh
DATABASE_URL=postgresql://broker:secret@localhost:5432/broker \
OIDC_ISSUER=https://sign-up.dev.twake.lin-saas.com/ \
OIDC_CLIENT_SECRET=... ENCRYPTION_KEY=... \
PUBLIC_BASE_URL=https://agent-consent.dev.twake.lin-saas.com \
  uv run uvicorn --factory twake_space_token_broker.app:create_app_from_env --port 8080
```

The image `ghcr.io/linagora/twake-space-token-broker` listens on 8080 as user 10001 and answers the probes at `/healthz`. It logs no access lines, since the callback's query string holds the user's authorization code. It is published as `latest` from `main` and with the version from `v*` tags.

To deploy it:

- register `<PUBLIC_BASE_URL>/callback` as a redirect of the LemonLDAP client;
- publish only `/consent`, `/callback` and `/consent/space` on the public host, and let only APISIX reach `/forward-auth` and `/delegation`;
- for Drive, set `DRIVE_INSTANCE_DOMAIN`, have LemonLDAP release `workplaceFqdn` in the client's userinfo, and let the broker reach the users' Drive instances over HTTPS;
- for Twake Space, set `SPACE_URL` and `SPACE_WEB_URL`, and let the broker reach Space's API;
- run a single replica, since the access tokens and the refresh lock are kept in memory.

## Test

The tests call the HTTP API against a real PostgreSQL that they start with Docker, a fake LemonLDAP, a fake cozy-stack and a fake Twake Space API.

The pages take their look from `@linagora/twake-css`: `npm ci` installs it and copies its variables and utility classes next to the pages, before the broker runs or the tests start.

```sh
npm ci
uv run pytest
uv run mypy
uv run ruff check . && uv run ruff format --check .
```

## License

[AGPL-3.0](LICENSE)
