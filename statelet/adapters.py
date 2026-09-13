"""The adapter contract, and an in-memory implementation of it.

Five methods. `verify` is the one that matters: most agents treat a 2xx
as proof of success, but APIs accept writes and silently do nothing
(missing scope, permissions that fail open, eventual consistency,
Slack's `200 {"ok": false}`). `verify` re-reads and checks both presence
and content fingerprint.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from statelet.core import Resource


class AdapterError(RuntimeError):
    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


@runtime_checkable
class Adapter(Protocol):
    name: str

    def read(self, subject: str) -> list[Resource]: ...
    def create(self, resource: Resource) -> Resource: ...
    def update(self, resource: Resource) -> Resource: ...
    def delete(self, resource: Resource) -> None: ...
    def verify(self, resource: Resource, *, present: bool = True) -> bool: ...


class BaseAdapter:
    name = "base"

    def read(self, subject: str) -> list[Resource]:
        raise NotImplementedError

    def create(self, resource: Resource) -> Resource:
        raise NotImplementedError

    def update(self, resource: Resource) -> Resource:
        raise NotImplementedError

    def delete(self, resource: Resource) -> None:
        raise NotImplementedError

    def verify(self, resource: Resource, *, present: bool = True) -> bool:
        """Re-read and confirm the resource is in the intended state.

        For present=True, identity must exist AND (when the resource
        declares tracked content) the fingerprint must match -- so a
        page that exists with the wrong body fails verification.
        """
        found = {r.key: r for r in self.read(resource.key.subject)}
        if not present:
            return resource.key not in found
        hit = found.get(resource.key)
        if hit is None:
            return False
        if resource.content:
            return hit.fingerprint == resource.fingerprint
        return True


class FakeAdapter(BaseAdapter):
    """In-memory app. Not a mock -- it holds state and enforces the same
    idempotency rules the live adapters must, which is why the chaos
    suite can run deterministically."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._store: dict[str, Resource] = {}
        self._n = 0
        self.write_calls = 0

    def read(self, subject: str) -> list[Resource]:
        # Copies, not references. Observed state must be a snapshot: an
        # UPDATE's `prior` is captured from here and used to roll back,
        # so handing out live objects lets the update mutate the very
        # record needed to undo it.
        return [
            Resource(key=r.key, content=dict(r.content),
                     meta=dict(r.meta), remote_id=r.remote_id)
            for r in self._store.values()
            if r.key.subject == subject
        ]

    def create(self, resource: Resource) -> Resource:
        self.write_calls += 1
        k = str(resource.key)
        if k in self._store:
            return self._store[k]          # idempotent
        self._n += 1
        self._store[k] = Resource(
            key=resource.key, content=dict(resource.content),
            meta=dict(resource.meta), remote_id=f"{self.name}-{self._n}",
        )
        return self._store[k]

    def update(self, resource: Resource) -> Resource:
        self.write_calls += 1
        k = str(resource.key)
        if k not in self._store:
            raise AdapterError(f"{self.name}: cannot update missing {k}")
        self._store[k].content = dict(resource.content)
        return self._store[k]

    def delete(self, resource: Resource) -> None:
        self.write_calls += 1
        self._store.pop(str(resource.key), None)   # idempotent

    # -- test/demo helpers ---------------------------------------------

    def tamper_delete(self, key_str: str) -> bool:
        """Simulate a human deleting something out-of-band."""
        return self._store.pop(key_str, None) is not None

    def tamper_content(self, key_str: str, content: dict) -> bool:
        """Simulate a human editing content out-of-band (e.g. emptying
        a page body). Identity survives, fingerprint changes."""
        r = self._store.get(key_str)
        if r is None:
            return False
        r.content = dict(content)
        return True

    def snapshot(self) -> list[str]:
        return sorted(self._store)

    def count(self) -> int:
        return len(self._store)
