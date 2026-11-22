import contextlib
import io
import json
import os
import sqlite3
import stat
import tempfile
import unittest

from helpers import WRITE_TOKEN, base_config

from shortlinks import cli
from shortlinks.config import Config, ConfigError, InvalidTarget, token_sha256
from shortlinks.store import Conflict, Store


class ConfigTests(unittest.TestCase):
    def test_minimal(self):
        cfg = Config.from_dict({"database": "x.db", "publicBaseUrl": "https://s.example.net/"})
        self.assertEqual(cfg.short_url("AB"), "https://s.example.net/AB")
        self.assertEqual(cfg.clients, ())

    def test_rejects(self):
        cases = [
            {"publicBaseUrl": "https://s.example.net"},
            {"database": "x", "publicBaseUrl": "s.example.net"},
            {"database": "x", "publicBaseUrl": "https://s.example.net/sub"},
            {"database": "x", "publicBaseUrl": "https://s", "typo": 1},
            {"database": "x", "publicBaseUrl": "https://s", "clients": {"a": {"tokenSha256": "abc"}}},
            {"database": "x", "publicBaseUrl": "https://s", "clients": {"a": {"tokenSha256": "a" * 64, "scope": "admin"}}},
            {"database": "x", "publicBaseUrl": "https://s",
             "clients": {"a": {"tokenSha256": "a" * 64}, "b": {"tokenSha256": "a" * 64}}},
            {"database": "x", "publicBaseUrl": "https://s",
             "patternRoutes": [{"name": "p", "regex": "(", "target": "https://h/{id}"}]},
            {"database": "x", "publicBaseUrl": "https://s", "allowedTargetHosts": ["h"],
             "patternRoutes": [{"name": "p", "regex": "A", "target": "https://other/{id}"}]},
            {"database": "x", "publicBaseUrl": "https://s",
             "patternRoutes": [{"name": "p", "regex": "A", "target": "https://h/?token={id}"}]},
            {"database": "x", "publicBaseUrl": "https://s", "codeLength": 2},
        ]
        for raw in cases:
            with self.assertRaises(ConfigError, msg=raw):
                Config.from_dict(raw)

    def test_scope_defaults_to_read_and_origin_is_lower_case(self):
        cfg = Config.from_dict({"database": "x", "publicBaseUrl": "https://S.Example.NET",
                                "clients": {"a": {"tokenSha256": "a" * 64}}})
        self.assertEqual(cfg.clients[0].scope, "read")
        self.assertEqual(cfg.public_base_url, "https://s.example.net")

    def test_credential_like_query_names(self):
        cfg = Config.from_dict(base_config("x.db"))
        for q in ("access_token=1", "auth_token=1", "X-Amz-Signature=1", "apiKey=1", "a=1;token=2",
                  "JSESSIONID=1", "sessionid=1", "client_secret=1", "tok%65n=1"):
            with self.assertRaises(InvalidTarget, msg=q):
                cfg.validate_target("https://reports.example.net/x?" + q)
        with self.assertRaises(InvalidTarget):
            cfg.validate_target("https://reports.example.net/x#access_token=abc")
        for q in ("project=p&artifact=a&view=focus&path=A001.html", "monkey=1", "keyboard=2"):
            cfg.validate_target("https://reports.example.net/x?" + q)
        cfg.validate_target("https://reports.example.net/x#section-2")

    def test_authenticate(self):
        cfg = Config.from_dict(base_config("x.db"))
        self.assertEqual(cfg.authenticate(f"Bearer {WRITE_TOKEN}").name, "publisher")
        self.assertEqual(cfg.authenticate(f"bearer   {WRITE_TOKEN}").name, "publisher")
        self.assertIsNone(cfg.authenticate("Bearer "))
        self.assertIsNone(cfg.authenticate(None))
        self.assertIsNone(cfg.authenticate(f"Basic {WRITE_TOKEN}"))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "l.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_schema_version_and_reopen(self):
        s = Store(self.path)
        s.insert("AB", "https://h/x", "t")
        s.close()
        s = Store(self.path)
        self.assertEqual(s.resolve("AB"), "https://h/x")
        s.close()
        db = sqlite3.connect(self.path)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
        db.execute("PRAGMA user_version=99")
        db.commit()
        db.close()
        with self.assertRaises(RuntimeError):
            Store(self.path)

    def test_readonly_never_creates_or_writes(self):
        with self.assertRaises(sqlite3.OperationalError):
            Store(self.path, readonly=True)
        self.assertFalse(os.path.exists(self.path))
        s = Store(self.path)
        s.insert("AB", "https://h/x", "t")
        s.close()
        ro = Store(self.path, readonly=True)
        self.assertEqual(ro.resolve("AB"), "https://h/x")
        with self.assertRaises(sqlite3.OperationalError):
            ro.insert("CD", "https://h/y", "t")
        ro.close()

    def test_insert_conflict_rolls_back(self):
        s = Store(self.path)
        s.insert("AB", "https://h/x", "t")
        with self.assertRaises(Conflict):
            s.insert("AB", "https://h/y", "t")
        self.assertEqual(len(s.history("AB")), 1)
        s.close()


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "l.db")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_mint_token(self):
        path = os.path.join(self.tmp.name, "tok")
        code, out, _ = self.run_cli("mint-token", "--name", "pub", "--out", path)
        self.assertEqual(code, 0)
        with open(path) as f:
            token = f.read().strip()
        self.assertEqual(len(token), 64)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertNotIn(token, out)
        self.assertEqual(json.loads(out), {"pub": {"tokenSha256": token_sha256(token), "scope": "write"}})
        code, _, err = self.run_cli("mint-token", "--name", "pub", "--out", path)
        self.assertEqual(code, 1)
        self.assertIn("refusing", err)

    def write_config(self, db):
        path = os.path.join(self.tmp.name, "config.json")
        with open(path, "w") as f:
            json.dump(base_config(db), f)
        return path

    def test_export_check_and_import(self):
        target_x = "https://reports.example.net/x"
        s = Store(self.db)
        s.insert("AB", target_x, "t")
        s.insert("CD", "https://reports.example.net/y", "t")
        s.delete("CD", "t")
        s.close()

        snapshot = os.path.join(self.tmp.name, "links.json")
        code, _, _ = self.run_cli("export", "--database", self.db, "--output", snapshot)
        self.assertEqual(code, 0)
        with open(snapshot) as f:
            doc = json.load(f)
        self.assertEqual([x["code"] for x in doc["links"]], ["AB", "CD"])
        self.assertIsNotNone(doc["links"][1]["deletedAt"])

        code, out, _ = self.run_cli("export", "--database", self.db, "--check", snapshot)
        self.assertEqual((code, out), (0, ""))

        s = Store(self.db)
        s.update("AB", "https://reports.example.net/z", "t")
        s.close()
        code, out, err = self.run_cli("export", "--database", self.db, "--check", snapshot)
        self.assertEqual(code, 1)
        self.assertIn("https://reports.example.net/z", out)

        fresh = os.path.join(self.tmp.name, "fresh.db")
        cfg = self.write_config(fresh)
        code, _, err = self.run_cli("import", "--config", cfg, snapshot)
        self.assertEqual(code, 0, err)
        self.assertIn("2 of 2", err)
        code, _, err = self.run_cli("import", "--config", cfg, snapshot)
        self.assertIn("0 of 2", err)
        code, _, _ = self.run_cli("export", "--database", fresh, "--check", snapshot)
        self.assertEqual(code, 0)

    def test_import_applies_the_api_rules(self):
        fresh = os.path.join(self.tmp.name, "fresh.db")
        cfg = self.write_config(fresh)
        row = {"code": "OK", "target": "https://reports.example.net/x", "createdBy": "t",
               "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z", "deletedAt": None}
        bad_rows = [
            {**row, "target": "https://reports.example.net/x\r\nSet-Cookie: a=b"},
            {**row, "target": "http://evil.example/"},
            {**row, "target": "https://reports.example.net/?token=1"},
            {**row, "code": "A123"},          # a pattern route's code
            {**row, "code": "lower"},         # not canonical
            {**row, "code": "API"},           # reserved
            {**row, "createdBy": None},
            {k: v for k, v in row.items() if k != "target"},
            "not an object",
        ]
        for bad in bad_rows:
            path = os.path.join(self.tmp.name, "bad.json")
            with open(path, "w") as f:
                json.dump({"format": 1, "links": [row, bad]}, f)
            code, _, err = self.run_cli("import", "--config", cfg, path)
            self.assertEqual(code, 1, (bad, err))
        with open(path, "w") as f:
            json.dump({"format": 1, "links": [row, row]}, f)
        code, _, err = self.run_cli("import", "--config", cfg, path)
        self.assertIn("twice", err)
        with open(path, "w") as f:
            f.write("[" * 100000)
        code, _, err = self.run_cli("import", "--config", cfg, path)
        self.assertEqual(code, 1)
        # Nothing was written by any refused import.
        self.assertEqual(Store(fresh).list(include_deleted=True), [])

    def test_remote_refuses_plain_http_off_loopback(self):
        os.environ["SHORTLINKS_TOKEN"] = "x"
        try:
            code, _, err = self.run_cli("list", "--url", "http://s.example.net")
        finally:
            del os.environ["SHORTLINKS_TOKEN"]
        self.assertEqual(code, 2)
        self.assertIn("https", err)

    def test_export_of_missing_database_fails_cleanly(self):
        code, _, err = self.run_cli("export", "--database", os.path.join(self.tmp.name, "none.db"))
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "none.db")))

    def test_remote_needs_url_and_token(self):
        env = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("SHORTLINKS_")}
        try:
            code, _, err = self.run_cli("list")
            self.assertEqual(code, 2)
            self.assertIn("SHORTLINKS_URL", err)
        finally:
            os.environ.update(env)


if __name__ == "__main__":
    unittest.main()
