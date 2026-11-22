"""Test fixtures: a real server on a loopback ephemeral port."""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest

from shortlinks.config import Config, token_sha256
from shortlinks.server import make_server
from shortlinks.store import Store

WRITE_TOKEN = "w" * 64
READ_TOKEN = "r" * 64
REPORTS = "https://reports.example.net"
INVENTORY_TARGET = REPORTS + "/artifacts/art_test?path={id}.html"


def base_config(db: str, **overrides) -> dict:
    raw = {
        "database": db,
        "publicBaseUrl": "https://s.example.net",
        "port": 0,
        "allowedTargetHosts": ["reports.example.net", "docs.example.net"],
        "patternRoutes": [
            {"name": "inventory", "regex": "[A-Z][0-9]{3}", "target": INVENTORY_TARGET},
        ],
        "clients": {
            "publisher": {"tokenSha256": token_sha256(WRITE_TOKEN), "scope": "write"},
            "viewer": {"tokenSha256": token_sha256(READ_TOKEN), "scope": "read"},
            "disabled": {"tokenSha256": ""},
        },
    }
    raw.update(overrides)
    return raw


class ServerCase(unittest.TestCase):
    """Starts one server per test against a fresh database."""

    config_overrides: dict = {}

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "links.db")
        self.cfg = Config.from_dict(base_config(self.db, **self.config_overrides))
        self.store = Store(self.db)
        self.server = make_server(self.cfg, self.store, host="127.0.0.1", port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.tmp.cleanup()

    def request(self, method: str, path: str, body: object = None, token: str | None = WRITE_TOKEN,
                headers: dict | None = None) -> tuple[int, dict, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse()
        raw = resp.read()
        result = (resp.status, {k.lower(): v for k, v in resp.getheaders()}, raw)
        conn.close()
        return result

    def api(self, method: str, path: str, body: object = None, token: str | None = WRITE_TOKEN):
        status, headers, raw = self.request(method, path, body, token)
        return status, json.loads(raw) if raw else None

    def create(self, target: str, code: str | None = None) -> dict:
        body = {"target": target}
        if code:
            body["code"] = code
        status, data = self.api("POST", "/api/v1/links", body)
        self.assertIn(status, (200, 201), data)
        return data
