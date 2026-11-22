{ lib, python3Packages }:

let
  # The version lives in one place, the Python package.
  version = builtins.head (builtins.match ''.*__version__ = "([^"]+)".*''
    (builtins.readFile ../src/shortlinks/__init__.py));
in
python3Packages.buildPythonApplication {
  pname = "shortlinks";
  inherit version;
  pyproject = true;

  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../pyproject.toml
      ../README.md
      ../LICENSE
      ../src
      ../tests
    ];
  };

  build-system = [ python3Packages.hatchling ];

  # Standard library only: no runtime Python dependencies.
  dependencies = [ ];

  # The test suite runs in the build, so a broken package never reaches a
  # deploy. Tests start real servers on loopback ports, which the build
  # sandbox allows.
  checkPhase = ''
    runHook preCheck
    PYTHONPATH=src''${PYTHONPATH:+:$PYTHONPATH} python -m unittest discover -s tests -v
    runHook postCheck
  '';
  pythonImportsCheck = [ "shortlinks" ];

  meta = {
    description = "Small self-hosted short-link redirect service with a REST API and an MCP endpoint";
    license = lib.licenses.mit;
    mainProgram = "shortlinks";
    platforms = lib.platforms.unix;
  };
}
