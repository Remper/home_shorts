# NixOS module: services.shortlinks.
#
# Runs the server as a hardened systemd unit on a loopback port. TLS and the
# public host name belong to a reverse proxy on the same machine, which this
# module does not configure; for Caddy, for example:
#
#   services.caddy.virtualHosts."s.example.net".extraConfig = ''
#     reverse_proxy http://127.0.0.1:${toString config.services.shortlinks.port}
#   '';
self:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.shortlinks;
  inherit (lib) mkOption types;

  stateDir = "/var/lib/shortlinks";

  settings = {
    inherit (cfg) listen port publicBaseUrl allowedTargetHosts codeLength;
    database = "${stateDir}/links.db";
    patternRoutes = map (r: { inherit (r) name regex target; }) cfg.patternRoutes;
    clients = lib.mapAttrs (_: c: { inherit (c) tokenSha256 scope; }) cfg.clients;
  } // lib.optionalAttrs (cfg.deniedQueryParams != null) {
    inherit (cfg) deniedQueryParams;
  };

  configFile = pkgs.writeText "shortlinks.json" (builtins.toJSON settings);

  routeModule = {
    options = {
      name = mkOption { type = types.str; description = "Name, used in error messages."; };
      regex = mkOption {
        type = types.str;
        description = "Python regex matched against the whole upper-cased code.";
        example = "[A-Z][0-9]{3}";
      };
      target = mkOption {
        type = types.str;
        description = "Target URL; `{id}` is replaced by the code.";
        example = "https://reports.example.net/artifacts/ID?path={id}.html";
      };
    };
  };

  clientModule = {
    options = {
      tokenSha256 = mkOption {
        type = types.str;
        description = ''
          SHA-256 (hex) of the client's bearer token, from
          `shortlinks mint-token`. Safe to commit: the token is 256 random
          bits. An empty string disables the client.
        '';
      };
      scope = mkOption {
        type = types.enum [ "read" "write" ];
        default = "read";
        description = "`read` may list and look up links; `write` may also change them.";
      };
    };
  };
in
{
  options.services.shortlinks = {
    enable = lib.mkEnableOption "the shortlinks redirect service";

    package = mkOption {
      type = types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      defaultText = lib.literalMD "the package from this flake";
      description = "The shortlinks package.";
    };

    listen = mkOption {
      type = types.str;
      default = "127.0.0.1";
      description = ''
        Bind address. The server speaks plain HTTP and expects a
        TLS-terminating reverse proxy on the same host; keep it on loopback.
      '';
    };

    port = mkOption {
      type = types.port;
      default = 8080;
      description = "Port the reverse proxy forwards to.";
    };

    publicBaseUrl = mkOption {
      type = types.str;
      example = "https://s.example.net";
      description = ''
        Scheme and host the service is reached at. Used to build short URLs
        in API answers and as the only Origin the API accepts from browsers.
      '';
    };

    allowedTargetHosts = mkOption {
      type = types.listOf types.str;
      default = [ ];
      example = [ "reports.example.net" ];
      description = ''
        Hosts a link may point at. Empty allows any https host, which turns
        the service into an open redirector for anyone holding a token; set
        it.
      '';
    };

    deniedQueryParams = mkOption {
      type = types.nullOr (types.listOf types.str);
      default = null;
      description = ''
        Query parameter names a target may not carry, because they look like
        credentials. null keeps the built-in list (token, session, key, ...).
      '';
    };

    codeLength = mkOption {
      type = types.ints.between 4 16;
      default = 5;
      description = "Length of generated codes.";
    };

    patternRoutes = mkOption {
      type = types.listOf (types.submodule routeModule);
      default = [ ];
      description = ''
        Families of codes redirected by rule instead of one stored link each,
        such as IDs printed on labels in bulk. A stored link may not use a
        code a route matches.
      '';
    };

    clients = mkOption {
      type = types.attrsOf (types.submodule clientModule);
      default = { };
      description = "API and MCP clients, keyed by name. The name is recorded on each change.";
    };

    configFile = mkOption {
      type = types.path;
      readOnly = true;
      default = configFile;
      defaultText = lib.literalMD "the generated JSON config";
      description = ''
        The generated config, for `shortlinks export --config` on the host.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    # A fixed user rather than DynamicUser: the service owns state, and a uid
    # that can change underneath persistent files is a known way to lose
    # access to them.
    users.users.shortlinks = {
      isSystemUser = true;
      group = "shortlinks";
      description = "shortlinks redirect service";
    };
    users.groups.shortlinks = { };

    # The CLI, for export, import and token minting on the host.
    environment.systemPackages = [ cfg.package ];

    systemd.services.shortlinks = {
      description = "shortlinks redirect service";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ];
      restartTriggers = [ configFile ];

      serviceConfig = {
        ExecStart = "${lib.getExe cfg.package} serve --config ${configFile}";
        User = "shortlinks";
        Group = "shortlinks";
        Restart = "on-failure";
        RestartSec = 5;
        StateDirectory = "shortlinks";
        StateDirectoryMode = "0750";
        UMask = "0027";

        # The process needs one listening socket and one directory.
        NoNewPrivileges = true;
        PrivateTmp = true;
        PrivateDevices = true;
        PrivateUsers = true;
        ProtectHome = true;
        ProtectSystem = "strict";
        ProtectHostname = true;
        ProtectClock = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectKernelLogs = true;
        ProtectControlGroups = true;
        ProtectProc = "invisible";
        ProcSubset = "pid";
        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" ];
        RestrictNamespaces = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;
        MemoryDenyWriteExecute = true;
        RemoveIPC = true;
        SystemCallArchitectures = "native";
        SystemCallFilter = [ "@system-service" "~@privileged @resources" ];
        # A blocked syscall returns EPERM instead of killing the process with
        # SIGSYS, so a filter that is too tight shows up as an error message
        # rather than a restart loop.
        SystemCallErrorNumber = "EPERM";
        CapabilityBoundingSet = "";
        AmbientCapabilities = "";
        MemoryMax = "256M";
      };
    };
  };
}
