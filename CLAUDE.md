# shortlinks: notes for agents

A public repository. Keep it general: no host names, IP addresses, people,
artifact or project IDs, or paths from any particular deployment. Examples use
`example.net`. Deployment-specific values belong in the deploying system's
own (private) configuration, which imports `nixosModules.default`.

- Standard-library Python only (>= 3.11). A new runtime dependency is a design
  change, not a convenience.
- Gate: `PYTHONPATH=src python -m unittest discover -s tests -v` must pass;
  `nix flake check -L` (package build and NixOS VM test) runs in CI.
- Layout: `src/shortlinks/` holds `codes` (normalising and generating codes),
  `config` (JSON config, target rules, auth), `store` (SQLite with history and
  tombstones), `service` (the operations), `server` (HTTP), `mcp` (MCP over
  Streamable HTTP) and `cli`. `nix/` holds the package, module and VM test.
- Invariants worth keeping:
  - redirects are 302 with `Cache-Control: no-store`;
  - codes are case-insensitive and upper case;
  - a code is never reused;
  - a stored code never shadows a pattern route;
  - no secrets in config;
  - no per-redirect logging.
