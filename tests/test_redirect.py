import statistics
import time
import unittest

from helpers import INVENTORY_TARGET, REPORTS, ServerCase


class RedirectTests(ServerCase):
    def test_stored_link_redirects_with_302_and_no_store(self):
        link = self.create(REPORTS + "/artifacts/art_one", code="HOME")
        status, headers, body = self.request("GET", "/HOME", token=None)
        self.assertEqual(status, 302)
        self.assertEqual(headers["location"], REPORTS + "/artifacts/art_one")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["referrer-policy"], "no-referrer")
        self.assertEqual(link["shortUrl"], "https://s.example.net/HOME")

    def test_head_redirects_too(self):
        self.create(REPORTS + "/x", code="HEADY")
        status, headers, body = self.request("HEAD", "/HEADY", token=None)
        self.assertEqual(status, 302)
        self.assertEqual(headers["location"], REPORTS + "/x")
        self.assertEqual(body, b"")

    def test_codes_are_case_insensitive(self):
        self.create(REPORTS + "/x", code="Garage-1")
        for path in ("/GARAGE-1", "/garage-1", "/GaRaGe-1"):
            status, headers, _ = self.request("GET", path, token=None)
            self.assertEqual((status, headers.get("location")), (302, REPORTS + "/x"), path)

    def test_pattern_route_redirects_every_well_formed_id(self):
        for path, code in (("/A001", "A001"), ("/a001", "A001"), ("/Z999", "Z999")):
            status, headers, _ = self.request("GET", path, token=None)
            self.assertEqual(status, 302, path)
            self.assertEqual(headers["location"], INVENTORY_TARGET.replace("{id}", code))

    def test_unknown_and_malformed_paths_are_404_with_fixed_body(self):
        for path in ("/", "/ZZZ", "/A001/", "/A001.html", "/A0011", "/A%30%30%31",
                     "/-A1", "/A--1", "/" + "A" * 33, "/A001?x=1/..", "/caf%C3%A9"):
            status, headers, body = self.request("GET", path, token=None)
            if path == "/A001?x=1/..":
                # A query string is ignored; the path is a valid ID.
                self.assertEqual(status, 302)
                continue
            self.assertEqual((status, body), (404, b"not found\n"), path)
            self.assertEqual(headers["cache-control"], "no-store")

    def test_stored_link_wins_over_nothing_and_deleted_link_is_gone(self):
        self.create(REPORTS + "/x", code="GONE")
        self.api("DELETE", "/api/v1/links/GONE")
        status, _, body = self.request("GET", "/GONE", token=None)
        self.assertEqual((status, body), (404, b"not found\n"))

    def test_redirect_needs_no_token_and_ignores_a_bad_one(self):
        self.create(REPORTS + "/x", code="OPEN")
        status, _, _ = self.request("GET", "/OPEN", token="nonsense")
        self.assertEqual(status, 302)

    def test_other_methods_on_codes_are_405(self):
        status, headers, _ = self.request("POST", "/A001", body={}, token=None)
        self.assertEqual(status, 405)
        self.assertEqual(headers["allow"], "GET, HEAD")

    def test_stdlib_errors_carry_the_common_headers(self):
        status, headers, body = self.request("OPTIONS", "/A001", token=None)
        self.assertEqual(status, 501)
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["content-type"], "application/json")
        status, headers, _ = self.request("GET", "/" + "A" * 70000, token=None)
        self.assertIn(status, (414, 400, 431))

    def test_healthz(self):
        status, _, body = self.request("GET", "/healthz", token=None)
        self.assertEqual((status, body), (200, b"ok\n"))

    def test_redirect_is_fast(self):
        # A smoke bound, far looser than a deployment's target, so the test
        # does not flake on a busy CI machine.
        self.create(REPORTS + "/x", code="FAST")
        times = []
        for _ in range(20):
            t = time.perf_counter()
            status, _, _ = self.request("GET", "/FAST", token=None)
            times.append(time.perf_counter() - t)
            self.assertEqual(status, 302)
        self.assertLess(statistics.median(times), 0.05)


if __name__ == "__main__":
    unittest.main()
