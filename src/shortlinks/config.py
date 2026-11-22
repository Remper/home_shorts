"""Service configuration, loaded from one JSON file.

Example (all keys except database and publicBaseUrl are optional):

    {
      "listen": "127.0.0.1",
      "port": 8080,
      "database": "/var/lib/shortlinks/links.db",
      "publicBaseUrl": "https://s.example.net",
      "allowedTargetHosts": ["reports.example.net"],
      "deniedQueryParams": ["token", "session"],
      "codeLength": 5,
      "patternRoutes": [
        {"name": "inventory", "regex": "[A-Z][0-9]{3}",
         "target": "https://reports.example.net/artifacts/abc?path={id}.html"}
      ],
      "clients": {
        "publisher": {"tokenSha256": "<64 hex>", "scope": "write"}
      }
    }

The file holds no secrets: clients are identified by the SHA-256 of their
bearer token, which is safe to keep in version control when the token is 256
random bits (see `shortlinks mint-token`).
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import re
import urllib.parse
from collections.abc import Sequence

from . import codes

# Words that mark a query parameter as a credential or a one-time link. A
# short link is unauthenticated and long-lived, so its target must be harmless
# to disclose. A parameter is refused when any word of its name (split on
# anything but letters and digits, so `access_token`, `X-Amz-Signature` and
# `apiKey`-style names are covered) is on this list, or when its whole name is.
DEFAULT_DENIED_QUERY_PARAMS = (
    "apikey",
    "auth",
    "bootstrap",
    "credential",
    "credentials",
    "key",
    "password",
    "passwd",
    "secret",
    "session",
    "sessionid",
    "jsessionid",
    "sig",
    "signature",
    "token",
)

MAX_TARGET_LENGTH = 2048

SCOPES = ("read", "write")

_HEX64 = re.compile(r"[0-9a-f]{64}")


class ConfigError(ValueError):
    pass


class InvalidTarget(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class PatternRoute:
    """A rule that redirects every code matching `regex` to a template.

    `regex` is matched against the whole upper-cased code. `target` may
    contain `{id}`, replaced by the code. Used for families of codes that are
    printed in bulk (for example inventory labels) and should not need one
    stored link each.
    """

    name: str
    regex: re.Pattern[str]
    target: str

    def resolve(self, code: str) -> str | None:
        if self.regex.fullmatch(code):
            return self.target.replace("{id}", urllib.parse.quote(code, safe=""))
        return None


@dataclasses.dataclass(frozen=True)
class Client:
    name: str
    token_sha256: str
    scope: str

    @property
    def can_write(self) -> bool:
        return self.scope == "write"


@dataclasses.dataclass(frozen=True)
class Config:
    database: str
    public_base_url: str
    listen: str = "127.0.0.1"
    port: int = 8080
    allowed_target_hosts: tuple[str, ...] = ()
    denied_query_params: tuple[str, ...] = DEFAULT_DENIED_QUERY_PARAMS
    code_length: int = 5
    pattern_routes: tuple[PatternRoute, ...] = ()
    clients: tuple[Client, ...] = ()

    # --- loading -------------------------------------------------------

    @classmethod
    def load(cls, path: str) -> Config:
        with open(path, encoding="utf-8") as f:
            try:
                raw = json.load(f)
            except json.JSONDecodeError as e:
                raise ConfigError(f"{path}: {e}") from None
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> Config:
        if not isinstance(raw, dict):
            raise ConfigError("config must be a JSON object")
        known = {
            "listen", "port", "database", "publicBaseUrl", "allowedTargetHosts",
            "deniedQueryParams", "codeLength", "patternRoutes", "clients",
        }
        unknown = set(raw) - known
        if unknown:
            raise ConfigError(f"unknown config keys: {', '.join(sorted(unknown))}")
        for key in ("database", "publicBaseUrl"):
            if not isinstance(raw.get(key), str) or not raw[key]:
                raise ConfigError(f"{key} is required")

        # Scheme and host are case-insensitive; compare them in lower case.
        base = raw["publicBaseUrl"].rstrip("/").lower()
        parts = urllib.parse.urlsplit(base)
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.path:
            raise ConfigError("publicBaseUrl must be a scheme and host, such as https://s.example.net")

        port = raw.get("port", 8080)
        if not isinstance(port, int) or not 0 <= port <= 65535:
            raise ConfigError("port must be 0-65535")

        code_length = raw.get("codeLength", 5)
        if not isinstance(code_length, int) or not 4 <= code_length <= 16:
            raise ConfigError("codeLength must be 4-16")

        hosts = tuple(h.lower() for h in _str_list(raw, "allowedTargetHosts"))
        denied = (
            tuple(p.lower() for p in _str_list(raw, "deniedQueryParams"))
            if "deniedQueryParams" in raw
            else DEFAULT_DENIED_QUERY_PARAMS
        )

        routes = []
        names = set()
        for i, r in enumerate(raw.get("patternRoutes", [])):
            if not isinstance(r, dict) or set(r) != {"name", "regex", "target"}:
                raise ConfigError(f"patternRoutes[{i}] needs exactly name, regex and target")
            if r["name"] in names:
                raise ConfigError(f"duplicate pattern route {r['name']}")
            names.add(r["name"])
            try:
                rx = re.compile(r["regex"])
            except re.error as e:
                raise ConfigError(f"patternRoutes[{i}].regex: {e}") from None
            route = PatternRoute(r["name"], rx, r["target"])
            routes.append(route)

        clients = []
        hashes = set()
        raw_clients = raw.get("clients", {})
        if not isinstance(raw_clients, dict):
            raise ConfigError("clients must be an object keyed by client name")
        for name, c in sorted(raw_clients.items()):
            if not isinstance(c, dict) or set(c) - {"tokenSha256", "scope"}:
                raise ConfigError(f"clients.{name} takes tokenSha256 and scope")
            digest = c.get("tokenSha256", "")
            scope = c.get("scope", "read")
            if scope not in SCOPES:
                raise ConfigError(f"clients.{name}.scope must be one of {', '.join(SCOPES)}")
            if digest == "":
                continue  # an empty hash disables a client without removing it
            digest = digest.lower()
            if not _HEX64.fullmatch(digest):
                raise ConfigError(f"clients.{name}.tokenSha256 must be 64 hex characters")
            if digest in hashes:
                raise ConfigError(f"clients.{name} shares a token with another client")
            hashes.add(digest)
            clients.append(Client(name, digest, scope))

        cfg = cls(
            database=raw["database"],
            public_base_url=base,
            listen=raw.get("listen", "127.0.0.1"),
            port=port,
            allowed_target_hosts=hosts,
            denied_query_params=denied,
            code_length=code_length,
            pattern_routes=tuple(routes),
            clients=tuple(clients),
        )
        # Pattern route targets are fixed by the operator, but they are held
        # to the same rules as API-created targets: they are disclosed to
        # anyone who requests a matching code.
        for route in cfg.pattern_routes:
            try:
                cfg.validate_target(route.target.replace("{id}", "X"))
            except InvalidTarget as e:
                raise ConfigError(f"pattern route {route.name}: {e}") from None
        return cfg

    # --- behaviour -----------------------------------------------------

    def short_url(self, code: str) -> str:
        return f"{self.public_base_url}/{code}"

    def matching_route(self, code: str) -> PatternRoute | None:
        for route in self.pattern_routes:
            if route.regex.fullmatch(code):
                return route
        return None

    def validate_target(self, target: object) -> str:
        """Return the target if it is acceptable for a short link."""
        if not isinstance(target, str) or not target:
            raise InvalidTarget("target must be a non-empty string")
        if len(target) > MAX_TARGET_LENGTH:
            raise InvalidTarget(f"target is longer than {MAX_TARGET_LENGTH} characters")
        if not target.isascii() or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in target):
            raise InvalidTarget("target must be ASCII without spaces or control characters")
        try:
            parts = urllib.parse.urlsplit(target)
            host = parts.hostname
            parts.port  # noqa: B018 - raises ValueError on a malformed port
        except ValueError as e:
            raise InvalidTarget(f"target is not a valid URL: {e}") from None
        if parts.scheme != "https":
            raise InvalidTarget("target must be an https URL")
        if not host:
            raise InvalidTarget("target has no host")
        if parts.username is not None or parts.password is not None:
            raise InvalidTarget("target must not carry credentials")
        if self.allowed_target_hosts and host not in self.allowed_target_hosts:
            raise InvalidTarget(f"target host {host} is not on the allowlist")
        denied = set(self.denied_query_params)
        # Split on ';' as well as '&': some servers accept either.
        for pair in re.split(r"[&;]", parts.query):
            name = urllib.parse.unquote_plus(pair.partition("=")[0])
            if _credential_like(name, denied):
                raise InvalidTarget(f"target query parameter {name!r} looks like a credential")
        # A fragment never reaches the server, but OAuth-style flows put
        # tokens there (#access_token=...); refuse anything parameter-like.
        if "=" in parts.fragment:
            raise InvalidTarget("target fragment must not carry parameters")
        return target

    def authenticate(self, authorization: str | None) -> Client | None:
        """Map an Authorization header to a client, or None."""
        if not authorization:
            return None
        scheme, _, token = authorization.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        found = None
        # Compare against every client without stopping at a match, so the
        # time taken does not depend on which client (if any) matched.
        for client in self.clients:
            if hmac.compare_digest(digest, client.token_sha256):
                found = client
        return found


def _credential_like(name: str, denied: set[str]) -> bool:
    lowered = name.lower()
    if lowered in denied:
        return True
    # Words of the name: split on non-alphanumerics and on camelCase humps.
    words = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+", name)
    return any(w.lower() in denied for w in words)


def _str_list(raw: dict, key: str) -> Sequence[str]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"{key} must be a list of non-empty strings")
    return value


def validate_stored_code(cfg: Config, code: object) -> str:
    """Check a code read from an export: canonical, allowed, not routed."""
    if not isinstance(code, str) or codes.normalise(code) != code:
        raise ConfigError(f"code {code!r} is not in canonical (upper-case) form")
    why = reserved_or_routed(cfg, code)
    if why:
        raise ConfigError(why)
    return code


def reserved_or_routed(cfg: Config, code: str) -> str | None:
    """Why a code cannot be stored, or None if it can."""
    if code in codes.RESERVED:
        return f"code {code} is reserved"
    route = cfg.matching_route(code)
    if route is not None:
        return f"code {code} belongs to pattern route {route.name}"
    return None


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
