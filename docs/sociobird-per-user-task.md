# Scope: per-user SocioBird (OAuth), so every account posts to its own channels

Status: **built 2026-11-19** (the pasted-key connection and the OAuth connection; the OAuth round trip is still untried against the live SocioBird, step 0 below). Originally scoped the same day.

## Why
Today one SocioBird API key (`SOCIOBIRD_API_KEY`) belongs to the operator's workspace. With accounts on, posting is off unless `CLIPCROW_SOCIOBIRD_SHARED=1`,
which lets every person who signs in post to the operator's channels. A SaaS needs each account to connect **its own** SocioBird workspace and to see and use only
its own channels. Code today: `publish.py` (`SocioBird`, built once in `cli.py` from the environment), `api.py` (`need_publishing()` and its five users:
channels, the posts list, bulk scheduling, single publish, and the two checks that go with them).

## What SocioBird offers (read from the `sociobird-app` source, commit 2465d48, not yet tried against the live service)
- **Authorisation-code flow.** `GET /oauth/authorize`, `POST /oauth/authorize` (approve or deny, answers with the redirect carrying `code` and `state`), `POST /oauth/token`.
  The code lives 10 minutes. PKCE `S256` is supported and **required only for dynamically registered clients**.
- **Two kinds of client.** *Static*: a workspace owner creates one app under Developer (name, description, picture, **one redirect URL**), gets `pca_...` and a secret
  `pcs_...`, can rotate the secret. One app per workspace. *Dynamic* (RFC 7591, `POST /oauth/register`): built for MCP clients, scope `mcp:read mcp:write`. Use the **static** kind.
- **A static app keeps the lenient flow:** the redirect goes to the stored redirect URL (the `redirect_uri` parameter is ignored), no PKCE needed. We send PKCE and `state` anyway.
- **The approver's own workspace is what the code is issued for** (`createAuthorizationCode(app.id, user.id, org.id)` takes the org of the person clicking Approve). So an app owned
  by the operator's workspace can be approved by anyone with a SocioBird account, and each token then acts for **that person's** workspace. This answers the open question of
  whether other workspaces can authorise our app; it must still be proven with one real round trip (step 0).
- **The token** is `pos_` plus a random string, sent as the `Authorization` header like an API key (`public.auth.middleware.ts`). The token answer has `access_token`,
  `token_type`, `scope` and nothing else: **no refresh token and no expiry**. It lives until the person revokes it ("Approved apps" screen) or the app is deleted.
- **Plan gate.** The API refuses with 403 "API access is not included in your plan" when the person's SocioBird plan has no public API (the free plan may not), and with 401
  "No subscription found" when billing is on and they have none. An OAuth token cannot use the `X-Postiz-Org` header (no acting on other workspaces).

## Decisions to make first
1. **Which workspace owns the ClipCrow app.** Recommended: the operator's SocioBird workspace (the one behind today's key). Its redirect URL is fixed to
   `https://<clipcrow host>/api/sociobird/callback`. A different host (staging) needs a second app, so keep the host in config.
2. **Keep an API-key fallback?** Recommended yes, as a second way to connect: a person pastes their own SocioBird API key (for people whose plan or setup rules out OAuth).
   Same storage, same per-user client. The operator key stays only for single-user installs (no accounts) and for the `SHARED` opt-in.
3. **Encryption key.** There is no at-rest encryption in ClipCrow yet. Add `CLIPCROW_SECRET_KEY` (32 random bytes, base64) and use AES-GCM or Fernet from the `cryptography`
   package (new dependency of the `service` extra). Rotating it needs a re-encrypt command; keep a key id in each row.

## The work
**Step 0. Prove it (half a day, no code in ClipCrow).** Create the static app in the operator workspace with the callback URL; run the flow once by hand (`curl` or a browser) with a
**second** SocioBird account; call `GET /public/v1/integrations` with the `pos_` token and confirm it lists the second account's channels and not the operator's. Record
the plan-gate answer for a free account. If another workspace is refused, fall back to the API-key path only.

**Step 1. Storage (`accounts.py`).** New table `sociobird_links(user_id PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, kind TEXT, token_enc BLOB, key_id TEXT,
workspace_label TEXT, connected_at TEXT, last_ok_at TEXT, last_error TEXT)`. The token is encrypted; never returned by any API, never logged, never in a URL (rule in CLAUDE.md).
Deleting the account deletes the row; **Disconnect** deletes it.

**Step 2. Connect flow (`api.py`, new `sociobird_oauth.py`).**
- `GET /api/sociobird/connect` (signed-in): makes a random `state` and a PKCE verifier, stores their hashes with the session and a 10-minute expiry, answers the authorise URL
  (the browser goes there). Strict ids, one pending flow per user.
- `GET /api/sociobird/callback`: checks `state` against the signed-in session in constant time, refuses a second use, exchanges the code (server to server, the client secret
  from `SOCIOBIRD_OAUTH_CLIENT_SECRET`), stores the token, then redirects to the settings page with a plain "connected" or "could not connect" message. Errors say what to do.
- `POST /api/sociobird/key` (paste-a-key path): validates the key with one `integrations` call before storing it.
- `DELETE /api/sociobird` and `GET /api/sociobird` (status only: connected or not, since when, last error, never the token).
- Env: `SOCIOBIRD_OAUTH_CLIENT_ID`, `SOCIOBIRD_OAUTH_CLIENT_SECRET`, `SOCIOBIRD_OAUTH_URL` (the web address of SocioBird's authorise page, `https://go.sociobird.app`).

**Step 3. Per-user client (`publish.py`, `api.py`).** `need_publishing()` takes the request, finds the signed-in user, decrypts their token, and returns a `SocioBird` built for that
call (`SocioBird(base, token)` already takes the key as an argument; only the construction moves). Never fall back to the operator's client when accounts are on, unless
`CLIPCROW_SOCIOBIRD_SHARED=1`. A 401 or 403 from SocioBird marks the link "needs reconnecting" (`last_error`) and the UI shows "Reconnect SocioBird"; 403 for plan says so in words.
Bulk scheduling and single publish capture the user's client when they queue the work, and a scheduled post uses that user's link, so a disconnect cancels their queued posts with a
clear status instead of posting from the wrong account. Channel ids must be checked against **that user's** channel list on every post (the existing allow-list check, per user).

**Step 4. UI (read `docs/ui.md` first).** Settings or the Brand page: a "Posting" card with **Connect SocioBird** (primary), "Paste an API key instead" (secondary, behind a disclosure),
the connection state, **Disconnect**. The publish panel on a clip says "Connect SocioBird to post" with the button when there is no link. No job data in markup, both themes, 360 px.

**Step 5. Tests (no network).** Fake SocioBird transport like the existing publish tests: the flow with a good `state`; a wrong, reused or expired `state` is refused; the token is
never in a response or a log line; two users see only their own channels; user B cannot post to a channel id that belongs to user A; a 401 marks the link broken; disconnect deletes the row and
cancels queued posts; deleting the account deletes the link; the encryption round trip and a wrong key id; accounts on without a link gives the connect message, not the operator's channels.

**Step 6. Docs and rules.** `docs/deploy.md` env table, `README.md` (the "per-user keys are future work" line), `.claude/development-rules.md` posting rule, dev-log entry, privacy page text
("we store your SocioBird access, encrypted, until you disconnect").

## Security notes
- The stored token can post to the person's real channels: encrypt at rest, restrict who can read the table, redact in logs and error text, rotate `CLIPCROW_SECRET_KEY` on a schedule.
- `state` is bound to the signed-in session, single use, short life. The callback must not trust any user id from the query.
- The client secret is the operator's: environment only, never printed, never in the repo.
- **No expiry means a leaked token stays valid** until the person revokes it in SocioBird; the Disconnect button should also say so, and the privacy text should tell people they can revoke there too.

## Size and risks
About 3 to 4 days after step 0: storage and encryption half a day, the flow one day, per-user client and the five routes one day, the UI and tests one to one and a half.
Risks: step 0 could show other workspaces cannot approve our app (then API-key-only); the SocioBird plan gate may exclude free users (the UI must say so plainly); the lack of refresh
tokens means a revoked token surfaces only as a failed post, so show the link state on the publish panel.
Not in scope: SocioBird's dynamic client registration, MCP scopes, or connecting platforms (YouTube, Instagram, TikTok) directly to ClipCrow.
