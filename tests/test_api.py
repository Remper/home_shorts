import unittest

from helpers import READ_TOKEN, REPORTS, ServerCase

from shortlinks import codes


class AuthTests(ServerCase):
    def test_missing_wrong_and_disabled_tokens_are_401(self):
        for token in (None, "nope", ""):
            status, headers, _ = self.request("GET", "/api/v1/links", token=token)
            self.assertEqual(status, 401, token)
            self.assertIn("Bearer", headers["www-authenticate"])
        status, _, _ = self.request("GET", "/api/v1/links", headers={"Authorization": "Basic abc"}, token=None)
        self.assertEqual(status, 401)

    def test_read_scope_can_read_but_not_write(self):
        self.create(REPORTS + "/x", code="SEEN")
        status, data = self.api("GET", "/api/v1/links", token=READ_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual([x["code"] for x in data["links"]], ["SEEN"])
        status, data = self.api("POST", "/api/v1/links", {"target": REPORTS + "/y"}, token=READ_TOKEN)
        self.assertEqual(status, 403)
        status, _ = self.api("DELETE", "/api/v1/links/SEEN", token=READ_TOKEN)
        self.assertEqual(status, 403)

    def test_origin_matches_case_insensitively(self):
        status, _, _ = self.request("GET", "/api/v1/links", headers={"Origin": "https://S.Example.net"})
        self.assertEqual(status, 200)

    def test_foreign_origin_is_refused(self):
        status, _, _ = self.request("GET", "/api/v1/links", headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("GET", "/api/v1/links", headers={"Origin": "https://s.example.net"})
        self.assertEqual(status, 200)


class CreateTests(ServerCase):
    def test_random_code_uses_unambiguous_alphabet(self):
        data = self.create(REPORTS + "/artifacts/art_1")
        self.assertTrue(data["created"])
        self.assertEqual(len(data["code"]), 5)
        self.assertTrue(set(data["code"]) <= set(codes.GENERATED_ALPHABET))
        self.assertEqual(data["createdBy"], "publisher")

    def test_create_is_idempotent_per_target(self):
        first = self.create(REPORTS + "/artifacts/art_1")
        status, second = self.api("POST", "/api/v1/links", {"target": REPORTS + "/artifacts/art_1"})
        self.assertEqual(status, 200)
        self.assertFalse(second["created"])
        self.assertEqual(first["code"], second["code"])

    def test_concurrent_creates_agree_on_one_code(self):
        import concurrent.futures

        target = REPORTS + "/artifacts/race"
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(
                lambda _: self.api("POST", "/api/v1/links", {"target": target}), range(24)))
        self.assertEqual({data["code"] for _, data in results if "code" in data}.__len__(), 1, results)
        self.assertEqual(sorted(status for status, _ in results).count(201), 1)

    def test_custom_code_is_normalised_and_idempotent(self):
        data = self.create(REPORTS + "/x", code="tools")
        self.assertEqual(data["code"], "TOOLS")
        status, again = self.api("POST", "/api/v1/links", {"target": REPORTS + "/x", "code": "Tools"})
        self.assertEqual((status, again["created"]), (200, False))

    def test_custom_code_conflicts(self):
        self.create(REPORTS + "/x", code="TAKEN")
        status, data = self.api("POST", "/api/v1/links", {"target": REPORTS + "/y", "code": "taken"})
        self.assertEqual(status, 409)
        # The pattern route's namespace.
        for code in ("A123", "z999"):
            status, data = self.api("POST", "/api/v1/links", {"target": REPORTS + "/y", "code": code})
            self.assertEqual(status, 409, (code, data))
        for code in ("a b", "-x", "x-", "a--b", "a/b", "é", "", 5, "api", "HEALTHZ"):
            status, data = self.api("POST", "/api/v1/links", {"target": REPORTS + "/y", "code": code})
            self.assertEqual(status, 400, (code, data))

    def test_deleted_code_is_never_reused(self):
        self.create(REPORTS + "/x", code="ONCE")
        self.api("DELETE", "/api/v1/links/ONCE")
        status, _ = self.api("POST", "/api/v1/links", {"target": REPORTS + "/x", "code": "ONCE"})
        self.assertEqual(status, 409)
        # A random code is minted instead for the same target.
        data = self.create(REPORTS + "/x")
        self.assertNotEqual(data["code"], "ONCE")

    def test_target_validation(self):
        bad = [
            "http://reports.example.net/x",           # not https
            "https://elsewhere.example/x",            # not on the allowlist
            "https://user:pw@reports.example.net/",   # credentials
            "https://reports.example.net/x?token=abc",
            "https://reports.example.net/x?a=1&Session=2",
            "https://reports.example.net/x y",
            "https://reports.example.net:99999/",
            "javascript:alert(1)",
            "",
            None,
            "https://reports.example.net/" + "a" * 2100,
        ]
        for target in bad:
            status, data = self.api("POST", "/api/v1/links", {"target": target})
            self.assertEqual(status, 400, (target, data))
        ok = self.create("https://REPORTS.example.net/review?project=p&artifact=a&view=focus")
        self.assertTrue(ok["created"])

    def test_bad_bodies(self):
        for body in (b"not json", b"[1]", b'"x"'):
            status, _ = self.api("POST", "/api/v1/links", body)
            self.assertEqual(status, 400, body)
        status, _ = self.api("POST", "/api/v1/links", b"x" * (64 * 1024 + 1))
        self.assertEqual(status, 413)


class ChangeTests(ServerCase):
    def test_get_update_delete_and_history(self):
        self.create(REPORTS + "/one", code="MOVE")
        status, data = self.api("PATCH", "/api/v1/links/move", {"target": REPORTS + "/two"})
        self.assertEqual((status, data["target"]), (200, REPORTS + "/two"))
        _, headers, _ = self.request("GET", "/MOVE", token=None)
        self.assertEqual(headers["location"], REPORTS + "/two")

        status, data = self.api("GET", "/api/v1/links/MOVE")
        self.assertEqual(status, 200)
        self.assertEqual([h["action"] for h in data["history"]], ["create", "update"])
        self.assertEqual(data["history"][1]["actor"], "publisher")

        status, data = self.api("DELETE", "/api/v1/links/MOVE")
        self.assertEqual((status, data["deleted"]), (200, True))
        status, _ = self.api("DELETE", "/api/v1/links/MOVE")
        self.assertEqual(status, 404)
        status, _ = self.api("PATCH", "/api/v1/links/MOVE", {"target": REPORTS + "/three"})
        self.assertEqual(status, 404)

        _, data = self.api("GET", "/api/v1/links")
        self.assertEqual(data["links"], [])
        _, data = self.api("GET", "/api/v1/links?include_deleted=1")
        self.assertEqual([x["code"] for x in data["links"]], ["MOVE"])

    def test_update_validates_target(self):
        self.create(REPORTS + "/one", code="KEEP")
        status, _ = self.api("PATCH", "/api/v1/links/KEEP", {"target": "https://elsewhere.example/"})
        self.assertEqual(status, 400)

    def test_unknown_code(self):
        status, _ = self.api("GET", "/api/v1/links/NOPE")
        self.assertEqual(status, 404)
        status, _ = self.api("GET", "/api/v1/links/a%2Fb")
        self.assertEqual(status, 404)

    def test_method_not_allowed(self):
        status, _ = self.api("PUT", "/api/v1/links/X", {})
        self.assertEqual(status, 405)
        status, _ = self.api("DELETE", "/api/v1/links")
        self.assertEqual(status, 405)


class RemoteCLITests(ServerCase):
    def test_create_print_url_and_get(self):
        import contextlib
        import io
        import os
        import tempfile

        from helpers import WRITE_TOKEN

        from shortlinks import cli

        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write(WRITE_TOKEN + "\n")
        self.addCleanup(os.unlink, f.name)
        url = f"http://127.0.0.1:{self.port}"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["create", REPORTS + "/cli", "--code", "cli-1", "--print-url",
                             "--url", url, "--token-file", f.name])
        self.assertEqual((code, out.getvalue()), (0, "https://s.example.net/CLI-1\n"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["get", "NOPE", "--url", url, "--token-file", f.name])
        self.assertEqual(code, 1)
        self.assertIn("404", err.getvalue())

        # Export through the API matches export from the database file.
        self.api("DELETE", "/api/v1/links/CLI-1")
        self.create(REPORTS + "/second")
        remote, local = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(remote):
            self.assertEqual(cli.main(["export", "--url", url, "--token-file", f.name]), 0)
        with contextlib.redirect_stdout(local):
            self.assertEqual(cli.main(["export", "--database", self.db]), 0)
        self.assertEqual(remote.getvalue(), local.getvalue())
        self.assertIn('"deletedAt": "20', local.getvalue())


if __name__ == "__main__":
    unittest.main()
