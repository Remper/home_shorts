# NixOS VM test: the module, the unit's hardening and the service together.
self:
{ pkgs, ... }:
{
  name = "shortlinks";

  nodes.machine = { ... }: {
    imports = [ self.nixosModules.default ];
    services.shortlinks = {
      enable = true;
      port = 8080;
      publicBaseUrl = "https://s.example.net";
      allowedTargetHosts = [ "reports.example.net" ];
      patternRoutes = [{
        name = "inventory";
        regex = "[A-Z][0-9]{3}";
        target = "https://reports.example.net/artifacts/art_test?path={id}.html";
      }];
      # sha256("test-token")
      clients.vm = {
        tokenSha256 = "4c5dc9b7708905f77f5e5d16316b5dfb425e68cb326dcd55a860e90a7707031e";
        scope = "write";
      };
    };
    environment.systemPackages = [ pkgs.curl ];
  };

  testScript = { nodes, ... }: ''
    url = "http://127.0.0.1:8080"
    auth = "-H 'Authorization: Bearer test-token' -H 'Content-Type: application/json'"

    machine.wait_for_unit("shortlinks.service")
    machine.wait_for_open_port(8080)
    machine.succeed(f"curl -sf {url}/healthz")

    out = machine.succeed(f"curl -s -o /dev/null -w '%{{http_code}} %{{redirect_url}}' {url}/a001")
    assert out == "302 https://reports.example.net/artifacts/art_test?path=A001.html", out
    out = machine.succeed(f"curl -s -o /dev/null -w '%{{http_code}}' {url}/A0011")
    assert out == "404", out

    machine.succeed(
        f"curl -sf {auth} -d '{{\"target\":\"https://reports.example.net/x\",\"code\":\"vm\"}}' "
        f"{url}/api/v1/links"
    )
    out = machine.succeed(f"curl -s -X POST {auth} -d '{{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}}' {url}/mcp")
    assert "shortlink_create" in out, out

    # Links survive a restart, and the host-side export sees them.
    machine.systemctl("restart shortlinks.service")
    machine.wait_for_open_port(8080)
    out = machine.succeed(f"curl -s -o /dev/null -w '%{{redirect_url}}' {url}/VM")
    assert out == "https://reports.example.net/x", out
    machine.succeed("shortlinks export --config ${nodes.machine.services.shortlinks.configFile} | grep -q '\"VM\"'")

    # The hardening is in effect, and the state directory is private.
    print(machine.succeed("systemd-analyze security shortlinks.service | tail -1"))
    machine.succeed("stat -c %a /var/lib/shortlinks | grep -qx 750")
  '';
}
