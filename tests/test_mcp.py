import json
import unittest

from helpers import READ_TOKEN, REPORTS, ServerCase

from shortlinks.mcp import PROTOCOL_VERSIONS


class MCPTests(ServerCase):
    def rpc(self, method, params=None, req_id=1, token=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if req_id is not None:
            msg["id"] = req_id
        if params is not None:
            msg["params"] = params
        kwargs = {} if token is None else {"token": token}
        status, headers, raw = self.request(
            "POST", "/mcp", msg, headers={"Accept": "application/json, text/event-stream"}, **kwargs
        )
        return status, headers, json.loads(raw) if raw else None

    def call(self, name, arguments, token=None):
        status, _, data = self.rpc("tools/call", {"name": name, "arguments": arguments}, token=token)
        self.assertEqual(status, 200)
        return data["result"]

    def test_handshake(self):
        status, headers, data = self.rpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        })
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(data["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", data["result"]["capabilities"])
        self.assertNotIn("mcp-session-id", headers)

        status, _, data = self.rpc("initialize", {"protocolVersion": "1999-01-01"})
        self.assertEqual(data["result"]["protocolVersion"], PROTOCOL_VERSIONS[0])

        status, _, data = self.rpc("notifications/initialized", req_id=None)
        self.assertEqual((status, data), (202, None))
        status, _, data = self.rpc("ping")
        self.assertEqual(data["result"], {})

    def test_tools_list(self):
        _, _, data = self.rpc("tools/list")
        names = [t["name"] for t in data["result"]["tools"]]
        self.assertEqual(names, ["shortlink_create", "shortlink_get", "shortlink_list", "shortlink_update"])
        for tool in data["result"]["tools"]:
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_create_get_list_update(self):
        result = self.call("shortlink_create", {"target": REPORTS + "/artifacts/art_9"})
        self.assertFalse(result["isError"])
        code = result["structuredContent"]["code"]
        self.assertTrue(result["structuredContent"]["shortUrl"].endswith("/" + code))
        self.assertEqual(json.loads(result["content"][0]["text"])["code"], code)

        again = self.call("shortlink_create", {"target": REPORTS + "/artifacts/art_9"})
        self.assertEqual(again["structuredContent"]["code"], code)
        self.assertFalse(again["structuredContent"]["created"])

        got = self.call("shortlink_get", {"code": code.lower()})
        self.assertEqual(got["structuredContent"]["history"][0]["action"], "create")

        listed = self.call("shortlink_list", {})
        self.assertEqual([x["code"] for x in listed["structuredContent"]["links"]], [code])

        updated = self.call("shortlink_update", {"code": code, "target": REPORTS + "/artifacts/art_10"})
        self.assertEqual(updated["structuredContent"]["target"], REPORTS + "/artifacts/art_10")

    def test_tool_errors_are_results(self):
        result = self.call("shortlink_create", {"target": "https://elsewhere.example/"})
        self.assertTrue(result["isError"])
        self.assertIn("allowlist", result["content"][0]["text"])
        result = self.call("shortlink_create", {"target": REPORTS + "/x"}, token=READ_TOKEN)
        self.assertTrue(result["isError"])
        result = self.call("shortlink_get", {"code": "NOPE"})
        self.assertTrue(result["isError"])

    def test_protocol_errors(self):
        _, _, data = self.rpc("tools/call", {"name": "shortlink_delete", "arguments": {}})
        self.assertEqual(data["error"]["code"], -32602)
        _, _, data = self.rpc("resources/list")
        self.assertEqual(data["error"]["code"], -32601)
        status, _, raw = self.request("POST", "/mcp", b"{nope")
        self.assertEqual((status, json.loads(raw)["error"]["code"]), (400, -32700))
        status, _, raw = self.request("POST", "/mcp", [{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
        self.assertEqual((status, json.loads(raw)["error"]["code"]), (400, -32600))
        status, _, raw = self.request("POST", "/mcp", {"jsonrpc": "2.0", "id": 1})
        self.assertEqual(status, 400)
        status, _, raw = self.request("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "result": {}})
        self.assertEqual((status, raw), (202, b""))
        status, _, raw = self.request("POST", "/mcp", b"[" * 60000)
        self.assertEqual(status, 400)
        status, _, _ = self.request("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                    headers={"MCP-Protocol-Version": "2024-01-01"})
        self.assertEqual(status, 400)
        status, _, _ = self.request("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                    headers={"MCP-Protocol-Version": "2025-06-18"})
        self.assertEqual(status, 200)
        result = self.call("shortlink_list", {"include_deleted": "false"})
        self.assertTrue(result["isError"])

    def test_auth_and_methods(self):
        status, _, _ = self.request("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, token=None)
        self.assertEqual(status, 401)
        status, headers, _ = self.request("GET", "/mcp")
        self.assertEqual((status, headers["allow"]), (405, "POST"))
        status, _, _ = self.request("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                    headers={"Origin": "http://localhost:1234"})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
