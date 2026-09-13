# Credentials and preflight

Run `python -m statelet.cli doctor` after setting these. It checks all
four apps and names exactly which are not ready. The live adapters are
the untested path — ten minutes here is the difference between a working
live run and a dead demo.

Use throwaway workspaces, not anything real.

## Slack

api.slack.com/apps → new app → OAuth & Permissions.
Bot scopes: `channels:read`, `channels:manage`, `users:read.email`.
Install to workspace, copy the `xoxb-` token.

```bash
export SLACK_BOT_TOKEN=xoxb-...
```

Slack returns HTTP 200 with `{"ok": false}` on failure. The adapter
checks `.ok` explicitly — this is the silent-failure shape the whole
project is about.

## Notion

notion.so/my-integrations → new internal integration → copy secret.

Then **open the parent page in Notion → `...` → Connections → add your
integration.** Skipping this is the most common Notion failure: the
token is valid, every call 404s, and nothing explains why.

Parent page id is the 32-char hex in the page URL.

```bash
export NOTION_TOKEN=secret_...
export NOTION_PARENT_PAGE_ID=...
```

## Linear

linear.app → Settings → API → Personal API key. Linear wants the raw key
in `Authorization`, **without** `Bearer` (the adapter handles this).

```bash
export LINEAR_API_KEY=lin_api_...
curl -s -X POST https://api.linear.app/graphql \
  -H "Authorization: $LINEAR_API_KEY" -H "Content-Type: application/json" \
  -d '{"query":"{ teams { nodes { id name } } }"}'
export LINEAR_TEAM_ID=...        # from the output above
```

## Google Calendar

Two options. **Prefer the refresh-token route** — the static token dies
after about an hour, which is long enough to kill a demo you are still
rehearsing.

### Refresh token (recommended)

console.cloud.google.com → create OAuth 2.0 Client ID (type: Desktop).
Then developers.google.com/oauthplayground → gear icon → "Use your own
OAuth credentials", paste client id and secret. Select scope
`https://www.googleapis.com/auth/calendar.events`, authorize, exchange
for tokens, copy the **refresh** token.

```bash
export GOOGLE_CLIENT_ID=...
export GOOGLE_CLIENT_SECRET=...
export GOOGLE_REFRESH_TOKEN=...
```

The adapter mints access tokens itself and re-mints on expiry or a
mid-run 401.

### Static token (fallback)

```bash
export GOOGLE_OAUTH_TOKEN=ya29...
```

Expires in ~1 hour with no renewal. Fine if you are recording in the
next few minutes.

## Then

```bash
python -m statelet.cli doctor
python -m statelet.cli apply --spec specs/new-hire.yaml --live --dry-run
```

`doctor` confirms reachability. `--dry-run` reads from all four apps and
prints the plan without issuing a single write — the fastest way to
confirm scopes are right before anything is created.
