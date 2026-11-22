"""Link operations shared by the REST API, the MCP endpoint and the CLI."""

from __future__ import annotations

import logging

from . import codes
from .config import Client, Config, InvalidTarget, reserved_or_routed
from .store import Conflict, Link, NotFound, Store

# Attempts at a random code before giving up. With 32**5 codes and a household's
# worth of links a collision is already rare; eight in a row means something is
# wrong (a pattern route that swallows most codes, say).
_GENERATE_ATTEMPTS = 8

# One audit line per change, whichever front end made it.
log = logging.getLogger("shortlinks")


class ServiceError(Exception):
    """An error a client caused, with the HTTP status that describes it."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Service:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store

    # --- redirect --------------------------------------------------------

    def resolve(self, raw_code: str) -> str | None:
        """Target for a code from a request path, or None for unknown."""
        code = codes.normalise(raw_code)
        if code is None:
            return None
        stored = self.store.get(code)
        if stored is not None:
            # A deleted code stays unknown; it never falls through to a
            # pattern route. create() refuses codes a route matches; a route
            # added later over old codes is reported at startup.
            return None if stored.deleted else stored.target
        for route in self.cfg.pattern_routes:
            target = route.resolve(code)
            if target is not None:
                return target
        return None

    def shadowed_codes(self) -> list[tuple[str, str]]:
        """(code, route name) for stored codes a pattern route also matches.

        create() never makes such a code, but a route added to the config
        later can cover old ones; the stored link (or its tombstone) wins.
        """
        found = []
        for link in self.store.list(include_deleted=True):
            route = self.cfg.matching_route(link.code)
            if route is not None:
                found.append((link.code, route.name))
        return found

    # --- API ---------------------------------------------------------------

    def link_json(self, link: Link, history: bool = False) -> dict:
        d = {
            "code": link.code,
            "shortUrl": self.cfg.short_url(link.code),
            "target": link.target,
            "createdBy": link.created_by,
            "createdAt": link.created_at,
            "updatedAt": link.updated_at,
            "deleted": link.deleted,
            "deletedAt": link.deleted_at,
        }
        if history:
            d["history"] = [
                {"action": h.action, "target": h.target, "actor": h.actor, "at": h.at}
                for h in self.store.history(link.code)
            ]
        return d

    def create(self, client: Client, target: object, code: object = None) -> tuple[Link, bool]:
        """Create a link. Returns (link, created).

        Without a custom code, idempotent per target: asking again for a
        target that already has a live link returns that link (created=False)
        instead of minting another code, so an agent that retries, or two
        agents publishing the same report at once, end up with one short link.
        With a custom code, idempotent per (code, target): a chosen code is a
        deliberate second name and is created even if the target has another.
        """
        self._require_write(client)
        target = self._target(target)
        if code is not None:
            if not isinstance(code, str):
                raise ServiceError(400, "code must be a string")
            try:
                code = codes.validate_custom(code)
            except codes.InvalidCode as e:
                raise ServiceError(400, str(e)) from None
            existing = self.store.get(code)
            if existing is not None and not existing.deleted and existing.target == target:
                return existing, False
            why = reserved_or_routed(self.cfg, code)
            if why:
                raise ServiceError(409, why)
            try:
                return self._created(self.store.insert(code, target, client.name)), True
            except Conflict as e:
                raise ServiceError(409, str(e)) from None

        def candidates():
            for _ in range(_GENERATE_ATTEMPTS):
                candidate = codes.generate(self.cfg.code_length)
                if not reserved_or_routed(self.cfg, candidate):
                    yield candidate

        try:
            link, created = self.store.insert_for_target(target, client.name, candidates())
        except Conflict:
            raise ServiceError(503, "could not find a free code; raise codeLength") from None
        if created:
            self._created(link)
        return link, created

    def get(self, client: Client, code: object) -> Link:
        link = self.store.get(self._code(code))
        if link is None:
            raise ServiceError(404, "no such link")
        return link

    def list(self, client: Client, include_deleted: bool = False) -> list[Link]:
        return self.store.list(include_deleted=include_deleted)

    def update(self, client: Client, code: object, target: object) -> Link:
        self._require_write(client)
        code = self._code(code)
        target = self._target(target)
        try:
            link = self.store.update(code, target, client.name)
        except NotFound:
            raise ServiceError(404, "no such link") from None
        log.info("update %s -> %s by %s", link.code, link.target, client.name)
        return link

    def delete(self, client: Client, code: object) -> Link:
        self._require_write(client)
        try:
            link = self.store.delete(self._code(code), client.name)
        except NotFound:
            raise ServiceError(404, "no such link") from None
        log.info("delete %s by %s", link.code, client.name)
        return link

    # --- helpers -----------------------------------------------------------

    @staticmethod
    def _created(link: Link) -> Link:
        log.info("create %s -> %s by %s", link.code, link.target, link.created_by)
        return link

    def _require_write(self, client: Client) -> None:
        if not client.can_write:
            raise ServiceError(403, f"client {client.name} has read-only access")

    def _target(self, target: object) -> str:
        try:
            return self.cfg.validate_target(target)
        except InvalidTarget as e:
            raise ServiceError(400, str(e)) from None

    @staticmethod
    def _code(code: object) -> str:
        normal = codes.normalise(code) if isinstance(code, str) else None
        if normal is None:
            raise ServiceError(404, "no such link")
        return normal
