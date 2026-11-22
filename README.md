# shortlinks

A small, self-hosted short-link service for a home or small network. Python
standard library only, one SQLite file, one process.

- `https://s.example.net/K7M2Q` → **302** to the stored target.
- **Pattern routes** redirect whole families of codes by rule, with no stored
  link each: for example every `A001`-style ID printed on a storage label goes
  to that compartment's page.
- **REST API** and an **MCP endpoint**, so scripts and AI agents can mint links
  (for example, a short link for every report they publish).
- **Codes fit QR alphanumeric mode.** Codes are upper-case letters, digits and
  `-`, matched case-insensitively. A link printed as `HTTPS://S.EXAMPLE.NET/A001`
  is therefore entirely in alphanumeric mode, which needs a smaller QR symbol
  than byte mode.

It deliberately does **not** count clicks, expire links, serve an admin UI,
or log redirects.

## Behaviour

| Request | Answer |
|---|---|
| `GET`/`HEAD /<code>`, stored link | `302`, `Location: <target>` |
| `GET`/`HEAD /<code>` matching a pattern route | `302` to the route's template with `{id}` = code |
| anything else (`/`, unknown or deleted code, `/A001/`, `/A001.html`) | `404`, body `not found` |
| `GET /healthz` | `200 ok` |

Every response carries `Cache-Control: no-store`, so a phone never replays an
old target after a link changes. The service also sends `Referrer-Policy: no-referrer`.
Redirects are always 302, never 301, because browsers cache a 301 indefinitely.

**Codes**
- Generated codes are 5 characters long by default.
- They are drawn from `23456789ABCDEFGHJKLMNPQRSTUVWXYZ`, which leaves out `0`/`O` and `1`/`I`.
- Custom codes may use letters, digits and single inner hyphens.
- A stored code may not match a pattern route.
- A deleted code is kept as a tombstone and never reused, because a code that was once printed must not start pointing somewhere else.

**Targets** must be:
- `https`;
- on `allowedTargetHosts`;
- free of user info;
- free of query parameters that look like credentials: any name containing a word such as `token`, `session`, `key`, `secret` or `signature`;
- free of a parameter-like fragment (`#access_token=...`).

A short link is unauthenticated and long-lived, so its target has to be
harmless to reveal.

## API

Every API and MCP request needs `Authorization: Bearer <token>`. Clients are
configured by the SHA-256 of their token, with scope `read` (the default) or `write`.

| Method and path | |
|---|---|
| `POST /api/v1/links` `{"target": ..., "code"?: ...}` | create: `201` new, `200` existing. Without `code`, **idempotent per target**, even under concurrent requests; with `code`, per code and target |
| `GET /api/v1/links[?include_deleted=1]` | list |
| `GET /api/v1/links/<code>` | one link with its change history |
| `PATCH /api/v1/links/<code>` `{"target": ...}` | change the target |
| `DELETE /api/v1/links/<code>` | delete (tombstone) |

```console
$ curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d '{"target":"https://reports.example.net/artifacts/abc"}' https://s.example.net/api/v1/links
{"code": "K7M2Q", "shortUrl": "https://s.example.net/K7M2Q", "created": true, ...}
```

Requests that carry an `Origin` header other than `publicBaseUrl` are refused.
This stops a web page from driving the API through a browser.

## MCP

`POST /mcp` is a stateless MCP server over Streamable HTTP (protocol
versions 2025-06-18 and 2025-11-25), answering in JSON.
It offers four tools:
- `shortlink_create`
- `shortlink_get`
- `shortlink_list`
- `shortlink_update`

Deletion is left out on purpose; it is available through the API and the CLI.

Register it with any client that speaks HTTP MCP with a bearer header, for example Claude Code:

```console
claude mcp add --transport http shortlinks https://s.example.net/mcp \
  --header "Authorization: Bearer $TOKEN"
```

## CLI

```console
shortlinks serve --config config.json
shortlinks mint-token --name publisher --out publisher.token   # prints the config entry, never the token
shortlinks create https://reports.example.net/artifacts/abc --print-url
shortlinks get K7M2Q | list [--all] | update K7M2Q URL | delete K7M2Q
shortlinks export --config config.json [--check links.json | --output links.json]
shortlinks export --url https://s.example.net --check links.json
shortlinks import --config config.json links.json          # every row checked like an API create
```

The remote commands use `--url` or `SHORTLINKS_URL`, which must be https unless it is loopback; they never follow redirects. For the token, they use
`SHORTLINKS_TOKEN`, or the file named by `--token-file` or `SHORTLINKS_TOKEN_FILE`.

`export` writes every link, deleted ones included, as sorted JSON. You can
commit that file to version control. `--check` exits 1 with a diff when the
live links have drifted from the committed copy. `export` opens the database
read-only. Run `import` as the service's user.

## Configuration

See [`examples/config.json`](examples/config.json). Only `database` and
`publicBaseUrl` are required. The file holds no secrets: a client is
identified by the hash of a 256-bit random token.

## NixOS

```nix
{
  inputs.shortlinks.url = "github:Remper/home_shorts";
  # ...
  modules = [
    shortlinks.nixosModules.default
    {
      services.shortlinks = {
        enable = true;
        port = 8090;
        publicBaseUrl = "https://s.example.net";
        allowedTargetHosts = [ "reports.example.net" ];
        clients.publisher.tokenSha256 = "<from shortlinks mint-token>";
      };
      services.caddy.virtualHosts."s.example.net".extraConfig = ''
        reverse_proxy http://127.0.0.1:8090
      '';
    }
  ];
}
```

The unit runs as a fixed system user and keeps its state in
`/var/lib/shortlinks` under systemd sandboxing. Back that directory up, or
commit `shortlinks export` output to git.

## Development

```console
PYTHONPATH=src python -m unittest discover -s tests -v   # unit and HTTP tests
nix flake check -L                                        # package build + NixOS VM test
```

## License

MIT
