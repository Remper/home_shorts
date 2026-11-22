"""Command line: run the server, manage links remotely, mint tokens, export.

  shortlinks serve --config FILE
  shortlinks create TARGET [--code CODE] [--print-url]
  shortlinks get CODE | list [--all] | update CODE TARGET | delete CODE
  shortlinks mint-token --name NAME [--scope write|read] --out FILE
  shortlinks token-hash < TOKEN
  shortlinks export (--config FILE | --database FILE) [--check FILE | --output FILE]
  shortlinks import (--config FILE | --database FILE) FILE

Remote commands read the server from --url or SHORTLINKS_URL and the token
from SHORTLINKS_TOKEN, or from the file named by --token-file or
SHORTLINKS_TOKEN_FILE.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import signal
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .config import Config, ConfigError, InvalidTarget, token_sha256, validate_stored_code
from .store import Link, Store

EXPORT_FORMAT = 1


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except CLIError as e:
        print(f"shortlinks: {e}", file=sys.stderr)
        return e.code


class CLIError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


# --- serve -------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import make_server

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    cfg = _config(args.config)
    store = Store(cfg.database)
    server = make_server(cfg, store)
    for code, route in server.RequestHandlerClass.service.shadowed_codes():
        logging.getLogger("shortlinks").warning(
            "stored code %s also matches pattern route %s; the stored link (or its tombstone) wins",
            code, route,
        )
    host, port = server.server_address[:2]
    logging.getLogger("shortlinks").info(
        "shortlinks %s listening on %s:%s for %s (%d clients, %d pattern routes)",
        __version__, host, port, cfg.public_base_url, len(cfg.clients), len(cfg.pattern_routes),
    )
    signal.signal(signal.SIGTERM, _interrupt)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()
    return 0


def _interrupt(signum, frame) -> None:
    # systemd stops the unit with SIGTERM; leave serve_forever() the same way
    # as on Ctrl-C so the database is closed cleanly.
    raise KeyboardInterrupt


# --- remote ------------------------------------------------------------------


def _request(args: argparse.Namespace, method: str, path: str, body: dict | None = None) -> dict:
    base = args.url or os.environ.get("SHORTLINKS_URL")
    if not base:
        raise CLIError("no server: pass --url or set SHORTLINKS_URL", 2)
    token = os.environ.get("SHORTLINKS_TOKEN")
    token_file = args.token_file or os.environ.get("SHORTLINKS_TOKEN_FILE")
    if token_file:
        with open(token_file, encoding="utf-8") as f:
            token = f.read().strip()
    if not token:
        raise CLIError("no token: set SHORTLINKS_TOKEN or SHORTLINKS_TOKEN_FILE, or pass --token-file", 2)
    parts = urllib.parse.urlsplit(base)
    if parts.scheme != "https" and parts.hostname not in ("127.0.0.1", "::1", "localhost"):
        raise CLIError(f"refusing to send a token to {base}: use https (plain http only for loopback)", 2)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"shortlinks-cli/{__version__}",
        },
    )
    try:
        with _NO_REDIRECTS.open(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            message = json.load(e).get("error", e.reason)
        except (json.JSONDecodeError, AttributeError):
            message = e.reason
        raise CLIError(f"{e.code}: {message}") from None
    except urllib.error.URLError as e:
        raise CLIError(f"cannot reach {base}: {e.reason}") from None


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    # urllib would follow a redirect and resend the Authorization header to
    # wherever it points. The API never redirects; treat one as an error.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"unexpected redirect to {newurl}", headers, fp)


_NO_REDIRECTS = urllib.request.build_opener(_RefuseRedirects)


def _code_path(code: str) -> str:
    return "/api/v1/links/" + urllib.parse.quote(code, safe="")


def _print(data: dict) -> None:
    print(json.dumps(data, indent=2))


def cmd_create(args: argparse.Namespace) -> int:
    body = {"target": args.target}
    if args.code:
        body["code"] = args.code
    data = _request(args, "POST", "/api/v1/links", body)
    print(data["shortUrl"]) if args.print_url else _print(data)
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    _print(_request(args, "GET", _code_path(args.code)))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    query = "?include_deleted=1" if args.all else ""
    _print(_request(args, "GET", "/api/v1/links" + query))
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    _print(_request(args, "PATCH", _code_path(args.code), {"target": args.target}))
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    _print(_request(args, "DELETE", _code_path(args.code)))
    return 0


# --- tokens ------------------------------------------------------------------


def cmd_mint_token(args: argparse.Namespace) -> int:
    token = secrets.token_hex(32)
    try:
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise CLIError(f"{args.out} exists; refusing to overwrite a token") from None
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    # The token itself is never printed: it goes only to the 0600 file.
    print(json.dumps({args.name: {"tokenSha256": token_sha256(token), "scope": args.scope}}, indent=2))
    print(f"token written to {args.out}", file=sys.stderr)
    return 0


def cmd_token_hash(args: argparse.Namespace) -> int:
    token = sys.stdin.read().strip()
    if not token:
        raise CLIError("no token on stdin", 2)
    print(token_sha256(token))
    return 0


# --- export / import -----------------------------------------------------------


def _open_store(args: argparse.Namespace, readonly: bool = False) -> Store:
    path = args.database or _config(args.config).database
    if readonly and not os.path.exists(path):
        raise CLIError(f"{path} does not exist")
    try:
        return Store(path, readonly=readonly)
    except (sqlite3.Error, RuntimeError) as e:
        raise CLIError(f"{path}: {e}") from None


def export_document(links: list[Link]) -> str:
    rows = sorted((link.to_export() for link in links), key=lambda d: d["code"])
    return json.dumps({"format": EXPORT_FORMAT, "links": rows}, indent=2, sort_keys=True) + "\n"


def _remote_links(args: argparse.Namespace) -> list[Link]:
    data = _request(args, "GET", "/api/v1/links?include_deleted=1")
    return [Link.from_export(d) for d in data["links"]]


def cmd_export(args: argparse.Namespace) -> int:
    if args.url:
        links = _remote_links(args)
    else:
        store = _open_store(args, readonly=True)
        try:
            links = store.list(include_deleted=True)
        finally:
            store.close()
    doc = export_document(links)
    if args.check:
        try:
            with open(args.check, encoding="utf-8") as f:
                expected = f.read()
        except FileNotFoundError:
            expected = ""
        if expected != doc:
            import difflib

            sys.stdout.writelines(difflib.unified_diff(
                expected.splitlines(keepends=True), doc.splitlines(keepends=True),
                fromfile=args.check, tofile="live",
            ))
            print(f"shortlinks: live links differ from {args.check}", file=sys.stderr)
            return 1
        return 0
    if args.output:
        tmp = args.output + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(doc)
        os.replace(tmp, args.output)
    else:
        sys.stdout.write(doc)
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    """Restore links from an export, checked as strictly as the API would.

    The export is meant to live in version control, so whoever can change that
    file must not be able to smuggle in a target the API would refuse (another
    host, a credential, a header-splitting CR/LF) or a code that hides a
    pattern route.
    """
    cfg = _config(args.config)
    try:
        with open(args.file, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError, RecursionError) as e:
        raise CLIError(f"{args.file}: {e}") from None
    if not isinstance(doc, dict) or doc.get("format") != EXPORT_FORMAT or not isinstance(doc.get("links"), list):
        raise CLIError(f"{args.file}: not a format {EXPORT_FORMAT} shortlinks export")
    links = []
    seen = set()
    for i, d in enumerate(doc["links"]):
        try:
            link = Link.from_export(d)
            validate_stored_code(cfg, link.code)
            cfg.validate_target(link.target)
            for field in (link.created_by, link.created_at, link.updated_at):
                if not isinstance(field, str) or not field:
                    raise ConfigError("createdBy, createdAt and updatedAt must be non-empty strings")
            if link.deleted_at is not None and not isinstance(link.deleted_at, str):
                raise ConfigError("deletedAt must be a string or null")
        except (KeyError, TypeError) as e:
            raise CLIError(f"{args.file}: links[{i}] is missing or has a bad field: {e}") from None
        except (ConfigError, InvalidTarget) as e:
            raise CLIError(f"{args.file}: links[{i}]: {e}") from None
        if link.code in seen:
            raise CLIError(f"{args.file}: code {link.code} appears twice")
        seen.add(link.code)
        links.append(link)
    store = Store(cfg.database)
    try:
        changed = store.restore(links, actor="import")
    finally:
        store.close()
    print(f"{changed} of {len(links)} links inserted or changed", file=sys.stderr)
    return 0


# --- plumbing ----------------------------------------------------------------


def _config(path: str) -> Config:
    try:
        return Config.load(path)
    except (OSError, ConfigError) as e:
        raise CLIError(f"config: {e}", 2) from None


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="shortlinks", description="Short-link redirect service.")
    p.add_argument("--version", action="version", version=f"shortlinks {__version__}")
    sub = p.add_subparsers(metavar="COMMAND")

    s = sub.add_parser("serve", help="run the HTTP server")
    s.add_argument("--config", required=True)
    s.set_defaults(func=cmd_serve)

    def remote(name: str, help: str, func) -> argparse.ArgumentParser:  # noqa: A002
        r = sub.add_parser(name, help=help)
        r.add_argument("--url", help="server base URL (default: $SHORTLINKS_URL)")
        r.add_argument("--token-file", help="file holding the bearer token")
        r.set_defaults(func=func)
        return r

    r = remote("create", "create a short link (or return the existing one)", cmd_create)
    r.add_argument("target")
    r.add_argument("--code", help="custom code")
    r.add_argument("--print-url", action="store_true", help="print only the short URL")
    r = remote("get", "show a link and its history", cmd_get)
    r.add_argument("code")
    r = remote("list", "list links", cmd_list)
    r.add_argument("--all", action="store_true", help="include deleted codes")
    r = remote("update", "change a link's target", cmd_update)
    r.add_argument("code")
    r.add_argument("target")
    r = remote("delete", "delete a link (its code is never reused)", cmd_delete)
    r.add_argument("code")

    t = sub.add_parser("mint-token", help="make a client token and print its config entry")
    t.add_argument("--name", required=True)
    t.add_argument("--scope", choices=("write", "read"), default="write")
    t.add_argument("--out", required=True, help="file to write the token to (mode 0600)")
    t.set_defaults(func=cmd_mint_token)

    h = sub.add_parser("token-hash", help="print the SHA-256 of a token read from stdin")
    h.set_defaults(func=cmd_token_hash)

    e = sub.add_parser("export", help="write all links, deleted ones included, as sorted JSON")
    src = e.add_mutually_exclusive_group(required=True)
    src.add_argument("--config", help="read the database named in this server config")
    src.add_argument("--database")
    src.add_argument("--url", help="read through the API instead (needs a token)")
    e.add_argument("--token-file", help="file holding the bearer token, with --url")
    out = e.add_mutually_exclusive_group()
    out.add_argument("--check", metavar="FILE", help="exit 1 with a diff if FILE differs")
    out.add_argument("--output", metavar="FILE")
    e.set_defaults(func=cmd_export)

    i = sub.add_parser("import", help="restore links from an export (run as the service's user)")
    i.add_argument("--config", required=True,
                   help="server config: names the database, and its rules check every row")
    i.add_argument("file")
    i.set_defaults(func=cmd_import)
    return p


if __name__ == "__main__":
    sys.exit(main())
