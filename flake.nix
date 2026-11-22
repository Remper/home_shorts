{
  description = "shortlinks: a small self-hosted short-link redirect service";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs = { self, nixpkgs }:
    let
      lib = nixpkgs.lib;
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAllSystems = f: lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAllSystems (pkgs: rec {
        shortlinks = pkgs.callPackage ./nix/package.nix { };
        default = shortlinks;
      });

      nixosModules.default = import ./nix/module.nix self;

      checks = forAllSystems (pkgs:
        {
          # Building the package runs the unit tests.
          package = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
        }
        // lib.optionalAttrs pkgs.stdenv.isLinux {
          # Boots a VM with the module and drives the service end to end.
          nixos = pkgs.testers.runNixOSTest (import ./nix/test.nix self);
        });

      formatter = forAllSystems (pkgs: pkgs.nixfmt-rfc-style);
    };
}
