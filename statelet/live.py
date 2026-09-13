"""Live app adapters: Slack, Notion, Linear, Google Calendar.

STATUS: written from each app's public API docs, NOT yet verified
against real credentials. Run `statelet doctor` before trusting this
path; the in-memory backend is the tested one.

Two rules every adapter follows:

- `read` returns only resources this tool created, identified by a
  marker embedded in a free-text field. Without that, "delete what is
  not in the spec" would delete things humans made.
- Errors are classified transient (429/5xx/timeout) vs permanent so the
  executor retries the right ones.
"""

from __future__ import annotations

import os
import time

import httpx

from statelet.adapters import AdapterError, BaseAdapter
from statelet.core import Resource, ResourceKey

MARKER = "statelet"
TIMEOUT = 15.0


def marker(subject: str) -> str:
    return f"[{MARKER}:{subject}]"


class _Http(BaseAdapter):
    base_url = ""

    def __init__(self, token: str) -> None:
        self._client = httpx.Client(
            timeout=TIMEOUT, headers={"Authorization": f"Bearer {token}"}
        )

    def _req(self, method: str, path: str, **kw) -> dict:
        url = path if path.startswith("http") else self.base_url + path
        try:
            resp = self._client.request(method, url, **kw)
        except httpx.TimeoutException as e:
            raise AdapterError(f"{self.name}: timeout ({e})", transient=True)
        except httpx.HTTPError as e:
            raise AdapterError(f"{self.name}: {e}", transient=True)
        if resp.status_code >= 400:
            raise AdapterError(
                f"{self.name}: HTTP {resp.status_code}: {resp.text[:200]}",
                transient=resp.status_code == 429 or resp.status_code >= 500,
            )
        return resp.json() if resp.content else {}


# -- Slack ----------------------------------------------------------------


class SlackAdapter(_Http):
    """Channel membership. Scopes: channels:read, channels:manage,
    users:read.email. Slack returns HTTP 200 with {"ok": false} on
    failure -- exactly the silent-failure shape, checked explicitly."""

    name = "slack"
    base_url = "https://slack.com/api"

    def _api(self, method: str, endpoint: str, **kw) -> dict:
        data = self._req(method, f"/{endpoint}", **kw)
        if not data.get("ok", False):
            err = data.get("error", "unknown")
            raise AdapterError(
                f"slack: {err}",
                transient=err in ("ratelimited", "internal_error"),
            )
        return data

    def _uid(self, email: str) -> str:
        return self._api("GET", "users.lookupByEmail",
                         params={"email": email})["user"]["id"]

    def _cid(self, name: str) -> str:
        cursor = None
        while True:
            p = {"limit": 200, "exclude_archived": "true"}
            if cursor:
                p["cursor"] = cursor
            data = self._api("GET", "conversations.list", params=p)
            for ch in data.get("channels", []):
                if ch["name"] == name:
                    return ch["id"]
            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                raise AdapterError(f"slack: no channel named {name!r}")

    def read(self, subject: str) -> list[Resource]:
        data = self._api("GET", "users.conversations", params={
            "user": self._uid(subject), "types": "public_channel", "limit": 200,
        })
        return [
            Resource(key=ResourceKey("slack", "channel_membership",
                                     subject, ch["name"]),
                     remote_id=ch["id"])
            for ch in data.get("channels", [])
        ]

    def create(self, resource: Resource) -> Resource:
        cid = self._cid(resource.key.name)
        self._api("POST", "conversations.invite",
                  json={"channel": cid, "users": self._uid(resource.key.subject)})
        resource.remote_id = cid
        return resource

    def update(self, resource: Resource) -> Resource:
        return resource  # membership has no tracked content

    def delete(self, resource: Resource) -> None:
        if resource.key.name in ("general", "random") or \
                resource.key.name.startswith("all-"):
            return
        cid = resource.remote_id or self._cid(resource.key.name)
        try:
            self._api("POST", "conversations.kick",
                      json={"channel": cid, "user": self._uid(resource.key.subject)})
        except AdapterError as e:
            if "not_in_channel" in str(e):
                return  # idempotent
            if "restricted_action" in str(e):
                # Workspace policy forbids removing members from
                # channels. Not a permission we can request -- an admin
                # setting, often unavailable on free plans. Surface it
                # rather than orphaning the run.
                raise AdapterError(
                    "slack: workspace policy forbids removing channel "
                    "members (restricted_action); offboarding cannot "
                    "revoke Slack access on this workspace",
                    transient=False,
                ) from e
            raise

    def preflight(self) -> str:
        return f"auth.test -> {self._api('POST', 'auth.test').get('team')}"


# -- Notion ---------------------------------------------------------------


class NotionAdapter(_Http):
    """Onboarding pages. Remember to add the integration to the parent
    page via ... -> Connections, or every call 404s with a valid token."""

    name = "notion"
    base_url = "https://api.notion.com/v1"

    def __init__(self, token: str, parent_page_id: str) -> None:
        super().__init__(token)
        self._client.headers["Notion-Version"] = "2022-06-28"
        self.parent = parent_page_id

    def read(self, subject: str) -> list[Resource]:
        data = self._req("POST", "/search", json={
            "query": marker(subject), "page_size": 100,
            "filter": {"value": "page", "property": "object"},
        })
        out = []
        for page in data.get("results", []):
            if page.get("archived"):
                continue
            title = _title(page)
            if marker(subject) not in title:
                continue
            out.append(Resource(
                key=ResourceKey("notion", "page", subject,
                                title.replace(marker(subject), "").strip()),
                content={"body": self._body(page["id"])},
                remote_id=page["id"],
            ))
        return out

    def verify(self, resource: Resource, *, present: bool = True) -> bool:
        """Verify by direct page fetch, not search.

        Notion's /search index is eventually consistent -- a page just
        created does not appear for several seconds, so search-based
        verification produces false negatives on create AND false
        positives on delete. Fetching the page by id is read-after-write
        consistent, so it is the only trustworthy check here.
        """
        pid = resource.remote_id
        if pid is None:
            return super().verify(resource, present=present)
        try:
            page = self._req("GET", f"/pages/{pid}")
        except AdapterError as exc:
            if "HTTP 404" in str(exc):
                return not present
            raise
        archived = bool(page.get("archived"))
        if not present:
            return archived
        if archived:
            return False
        if resource.content:
            return self._body(pid) == resource.content.get("body", "")
        return True

    def _body(self, page_id: str) -> str:
        data = self._req("GET", f"/blocks/{page_id}/children",
                         params={"page_size": 50})
        parts = []
        for b in data.get("results", []):
            rt = (b.get(b.get("type", ""), {}) or {}).get("rich_text", [])
            parts.append("".join(t.get("plain_text", "") for t in rt))
        return "\n".join(p for p in parts if p).strip()

    def _para(self, text: str) -> dict:
        return {"object": "block", "type": "paragraph", "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": text}}]}}

    def create(self, resource: Resource) -> Resource:
        title = f"{resource.key.name} {marker(resource.key.subject)}"
        body = resource.content.get("body", "")
        data = self._req("POST", "/pages", json={
            "parent": {"page_id": self.parent},
            "properties": {"title": {"title": [{"text": {"content": title}}]}},
            "children": [self._para(body)] if body else [],
        })
        resource.remote_id = data["id"]
        return resource

    def update(self, resource: Resource) -> Resource:
        pid = resource.remote_id or self._resolve(resource)
        if pid is None:
            raise AdapterError(f"notion: cannot update missing {resource.key}")
        existing = self._req("GET", f"/blocks/{pid}/children",
                             params={"page_size": 50})
        for b in existing.get("results", []):
            self._req("DELETE", f"/blocks/{b['id']}")
        body = resource.content.get("body", "")
        if body:
            self._req("PATCH", f"/blocks/{pid}/children",
                      json={"children": [self._para(body)]})
        resource.remote_id = pid
        return resource

    def _resolve(self, resource: Resource) -> str | None:
        hit = {r.key: r for r in self.read(resource.key.subject)}.get(resource.key)
        return hit.remote_id if hit else None

    def delete(self, resource: Resource) -> None:
        pid = resource.remote_id or self._resolve(resource)
        if pid is None:
            return  # already gone
        self._req("PATCH", f"/pages/{pid}", json={"archived": True})

    def preflight(self) -> str:
        data = self._req("GET", f"/pages/{self.parent}")
        return f"parent page reachable -> {data.get('id')}"


def _title(page: dict) -> str:
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in prop["title"])
    return ""


# -- Linear ---------------------------------------------------------------


class LinearAdapter(_Http):
    """Onboarding issues via GraphQL. Linear wants the raw key in
    Authorization, without 'Bearer'."""

    name = "linear"
    base_url = "https://api.linear.app"

    def __init__(self, token: str, team_id: str) -> None:
        super().__init__(token)
        self._client.headers["Authorization"] = token
        self.team_id = team_id

    def _gql(self, query: str, variables: dict) -> dict:
        data = self._req("POST", "/graphql",
                         json={"query": query, "variables": variables})
        if data.get("errors"):
            msg = str(data["errors"])
            raise AdapterError(f"linear: {msg[:200]}",
                               transient="RATE" in msg.upper())
        return data["data"]

    def read(self, subject: str) -> list[Resource]:
        q = """query($t:ID!,$m:String!){
          issues(filter:{team:{id:{eq:$t}},description:{contains:$m}},first:100){
            nodes{id title description assignee{email}}}}"""
        data = self._gql(q, {"t": self.team_id, "m": marker(subject)})
        out = []
        for n in data["issues"]["nodes"]:
            if marker(subject) not in (n.get("description") or ""):
                continue
            out.append(Resource(
                key=ResourceKey("linear", "issue", subject, n["title"]),
                content={"assignee": (n.get("assignee") or {}).get("email") or ""},
                remote_id=n["id"],
            ))
        return out

    def _user_id(self, email: str) -> str | None:
        q = """query($e:String!){users(filter:{email:{eq:$e}},first:1){
                 nodes{id}}}"""
        nodes = self._gql(q, {"e": email})["users"]["nodes"]
        return nodes[0]["id"] if nodes else None

    def create(self, resource: Resource) -> Resource:
        q = """mutation($t:String!,$ti:String!,$d:String!,$a:String){
          issueCreate(input:{teamId:$t,title:$ti,description:$d,assigneeId:$a}){
            success issue{id}}}"""
        data = self._gql(q, {
            "t": self.team_id, "ti": resource.key.name,
            "d": f"Onboarding task. {marker(resource.key.subject)}",
            "a": self._user_id(resource.content.get("assignee", "")),
        })["issueCreate"]
        if not data.get("success"):
            raise AdapterError("linear: issueCreate returned success=false")
        resource.remote_id = data["issue"]["id"]
        return resource

    def update(self, resource: Resource) -> Resource:
        iid = resource.remote_id or self._resolve(resource)
        if iid is None:
            raise AdapterError(f"linear: cannot update missing {resource.key}")
        q = """mutation($id:String!,$a:String){
          issueUpdate(id:$id,input:{assigneeId:$a}){success}}"""
        self._gql(q, {"id": iid,
                      "a": self._user_id(resource.content.get("assignee", ""))})
        resource.remote_id = iid
        return resource

    def _resolve(self, resource: Resource) -> str | None:
        hit = {r.key: r for r in self.read(resource.key.subject)}.get(resource.key)
        return hit.remote_id if hit else None

    def delete(self, resource: Resource) -> None:
        iid = resource.remote_id or self._resolve(resource)
        if iid is None:
            return
        self._gql("""mutation($id:String!){issueDelete(id:$id){success}}""",
                  {"id": iid})

    def preflight(self) -> str:
        q = """query($t:String!){team(id:$t){id name}}"""
        team = self._gql(q, {"t": self.team_id})["team"]
        return f"team reachable -> {team.get('name')}"


# -- Google Calendar ------------------------------------------------------


class GCalAdapter(_Http):
    """Onboarding events, with OAuth refresh.

    Preferred env: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
    GOOGLE_REFRESH_TOKEN -- the adapter mints and re-mints access tokens
    itself, so a long demo does not die at the 60-minute mark.

    Fallback: GOOGLE_OAUTH_TOKEN, a raw access token that expires in
    about an hour and cannot be renewed.
    """

    name = "gcal"
    base_url = "https://www.googleapis.com/calendar/v3"
    TOKEN_URL = "https://oauth2.googleapis.com/token"

    def __init__(
        self, *, access_token: str | None = None, client_id: str | None = None,
        client_secret: str | None = None, refresh_token: str | None = None,
        calendar_id: str = "primary",
    ) -> None:
        super().__init__(access_token or "")
        self.cal = calendar_id
        self._cid, self._secret, self._refresh = client_id, client_secret, refresh_token
        # A supplied access token is assumed good for 55 minutes; a
        # refreshable config starts expired so the first call mints one.
        self._expires_at = time.time() + 3300 if access_token else 0.0

    @property
    def can_refresh(self) -> bool:
        return bool(self._cid and self._secret and self._refresh)

    def _mint(self) -> None:
        resp = httpx.post(self.TOKEN_URL, timeout=TIMEOUT, data={
            "client_id": self._cid, "client_secret": self._secret,
            "refresh_token": self._refresh, "grant_type": "refresh_token",
        })
        if resp.status_code >= 400:
            raise AdapterError(
                f"gcal: token refresh failed HTTP {resp.status_code}: "
                f"{resp.text[:200]}",
                transient=resp.status_code >= 500,
            )
        data = resp.json()
        self._client.headers["Authorization"] = f"Bearer {data['access_token']}"
        # Refresh a minute early to avoid racing expiry mid-run.
        self._expires_at = time.time() + int(data.get("expires_in", 3600)) - 60

    def _ensure_token(self) -> None:
        if time.time() < self._expires_at:
            return
        if not self.can_refresh:
            raise AdapterError(
                "gcal: access token expired and no refresh credentials set. "
                "Set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / "
                "GOOGLE_REFRESH_TOKEN, or mint a fresh GOOGLE_OAUTH_TOKEN."
            )
        self._mint()

    def _req(self, method: str, path: str, **kw) -> dict:
        self._ensure_token()
        try:
            return super()._req(method, path, **kw)
        except AdapterError as e:
            # A 401 mid-run means the token died early; refresh once and
            # retry rather than failing the whole reconcile.
            if "HTTP 401" in str(e) and self.can_refresh:
                self._mint()
                return super()._req(method, path, **kw)
            raise

    def read(self, subject: str) -> list[Resource]:
        data = self._req("GET", f"/calendars/{self.cal}/events", params={
            "q": marker(subject), "maxResults": 250,
            "singleEvents": "true", "showDeleted": "false",
        })
        out = []
        for ev in data.get("items", []):
            if marker(subject) not in (ev.get("description") or ""):
                continue
            start = (ev.get("start") or {})
            out.append(Resource(
                key=ResourceKey("gcal", "event", subject, ev.get("summary", "")),
                content={"start": start.get("date") or start.get("dateTime", "")},
                remote_id=ev["id"],
            ))
        return out

    def _payload(self, resource: Resource) -> dict:
        start = resource.content.get("start") or "2026-09-21"
        return {
            "summary": resource.key.name,
            "description": f"Onboarding. {marker(resource.key.subject)}",
            "start": {"date": start}, "end": {"date": start},
            "attendees": [{"email": e}
                          for e in resource.meta.get("attendees", []) if e],
        }

    def create(self, resource: Resource) -> Resource:
        data = self._req("POST", f"/calendars/{self.cal}/events",
                         params={"sendUpdates": "all"},
                         json=self._payload(resource))
        resource.remote_id = data["id"]
        return resource

    def update(self, resource: Resource) -> Resource:
        eid = resource.remote_id or self._resolve(resource)
        if eid is None:
            raise AdapterError(f"gcal: cannot update missing {resource.key}")
        self._req("PATCH", f"/calendars/{self.cal}/events/{eid}",
                  json=self._payload(resource))
        resource.remote_id = eid
        return resource

    def _resolve(self, resource: Resource) -> str | None:
        hit = {r.key: r for r in self.read(resource.key.subject)}.get(resource.key)
        return hit.remote_id if hit else None

    def delete(self, resource: Resource) -> None:
        eid = resource.remote_id or self._resolve(resource)
        if eid is None:
            return
        try:
            self._req("DELETE", f"/calendars/{self.cal}/events/{eid}")
        except AdapterError as e:
            if "410" in str(e) or "404" in str(e):
                return  # already gone
            raise

    def preflight(self) -> str:
        self._req("GET", f"/calendars/{self.cal}/events", params={"maxResults": 1})
        mode = "refresh token" if self.can_refresh else "static token (expires ~1h)"
        return f"calendar reachable -> {mode}"


# -- wiring ---------------------------------------------------------------

REQUIRED = {
    "slack": ["SLACK_BOT_TOKEN"],
    "notion": ["NOTION_TOKEN", "NOTION_PARENT_PAGE_ID"],
    "linear": ["LINEAR_API_KEY", "LINEAR_TEAM_ID"],
    "gcal": ["GOOGLE_OAUTH_TOKEN | GOOGLE_CLIENT_ID+GOOGLE_CLIENT_SECRET+"
             "GOOGLE_REFRESH_TOKEN"],
}


def _gcal_ready() -> bool:
    return bool(os.environ.get("GOOGLE_OAUTH_TOKEN")) or all(
        os.environ.get(v) for v in
        ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")
    )


def missing_env() -> dict[str, list[str]]:
    out = {}
    for app, vars_ in REQUIRED.items():
        if app == "gcal":
            if not _gcal_ready():
                out[app] = vars_
            continue
        gaps = [v for v in vars_ if not os.environ.get(v)]
        if gaps:
            out[app] = gaps
    return out


def build_gcal() -> GCalAdapter:
    return GCalAdapter(
        access_token=os.environ.get("GOOGLE_OAUTH_TOKEN"),
        client_id=os.environ.get("GOOGLE_CLIENT_ID"),
        client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
        refresh_token=os.environ.get("GOOGLE_REFRESH_TOKEN"),
        calendar_id=os.environ.get("GOOGLE_CALENDAR_ID", "primary"),
    )


def build_live_adapters() -> dict:
    missing = missing_env()
    if missing:
        lines = "\n".join(f"  {a}: {', '.join(v)}" for a, v in missing.items())
        raise AdapterError(
            f"missing credentials for --live:\n{lines}\n\n"
            "Run `statelet doctor` for per-app checks, or omit --live to use "
            "the in-memory backend."
        )
    return {
        "slack": SlackAdapter(os.environ["SLACK_BOT_TOKEN"]),
        "notion": NotionAdapter(os.environ["NOTION_TOKEN"],
                                os.environ["NOTION_PARENT_PAGE_ID"]),
        "linear": LinearAdapter(os.environ["LINEAR_API_KEY"],
                                os.environ["LINEAR_TEAM_ID"]),
        "gcal": build_gcal(),
    }
