{
  description = "Local Claude and Codex Desktop bridge";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/2efa67fd26b6df417c33e4603185c701f260dd83";
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
    };
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
    };
  };

  outputs = { self, nixpkgs, pyproject-nix, uv2nix, pyproject-build-systems }:
    let
      system = "x86_64-linux";
      inherit (nixpkgs) lib;
      pkgs = nixpkgs.legacyPackages.${system};
      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
      pythonSet = (pkgs.callPackage pyproject-nix.build.packages {
        python = pkgs.python312;
      }).overrideScope (lib.composeManyExtensions [
        pyproject-build-systems.overlays.wheel
        (workspace.mkPyprojectOverlay { sourcePreference = "wheel"; })
        (final: prev: {
          claudex = prev.claudex.overrideAttrs (old: {
            nativeBuildInputs = (old.nativeBuildInputs or [ ])
              ++ final.resolveBuildSystem { setuptools = [ ]; };
          });
          claude-agent-sdk = prev.claude-agent-sdk.overrideAttrs (old: {
            # The SDK wheel carries a native Claude Code executable.
            buildInputs = (old.buildInputs or [ ]) ++ [ pkgs.stdenv.cc.cc.lib pkgs.zlib ];
          });
        })
      ]);
    in
    {
      packages.${system}.default = pythonSet.mkVirtualEnv "claudex" workspace.deps.default;

      checks.${system}.package = pkgs.runCommand "claudex-package-check" {
        nativeBuildInputs = [ self.packages.${system}.default ];
      } ''
        claudex --help
        claudex-desktop --help
        python -c 'from importlib.resources import files; assert files("patcher").joinpath("restore.cjs").is_file()'
        cli=$(python -c 'from importlib.resources import files; print(files("claude_agent_sdk").joinpath("_bundled", "claude"))')
        HOME="$TMPDIR" "$cli" --version
        touch "$out"
      '';
    };
}
