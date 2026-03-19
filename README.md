<p align="center"><strong>Codex HSOG</strong> is an HS Offenburg fork of the Rust-based Codex CLI.
<p align="center">
  <img src="https://github.com/openai/codex/blob/main/.github/codex-cli-splash.png" alt="Codex CLI splash" width="80%" />
</p>
</br>
This fork is intended for student distribution via native launcher bundles and keeps a local patch in the Rust implementation so the CLI can still work with gateways that provide Completions or Chat Completions behavior where upstream Codex expects Responses.</p>

---

## Fork note

This repository is not the stock upstream Codex distribution.

- It is a fork of the Rust Codex CLI.
- It contains an HSOG-specific patch so the Rust version can continue to process gateway traffic that only exposes Completions or Chat Completions semantics.
- The Node/`npm` variant is not the patched student distribution.
- Student-facing releases are published as `codex-hsog` so they can coexist with an existing upstream `codex` install.

## Using Student Releases

Students should download the platform-specific bundle from the releases of this fork:

- <https://github.com/grabow/codex-hsog/releases/latest>

Each bundle already contains:

- the native `codex-hsog` binary
- a launcher script
- a short `README.txt`

Choose the bundle that matches the student machine:

- macOS Apple Silicon: `codex-hsog-macos-aarch64.tar.gz`
- macOS Intel: `codex-hsog-macos-x86_64.tar.gz`
- Linux x86_64: `codex-hsog-linux-x86_64.tar.gz`
- Linux ARM64: `codex-hsog-linux-aarch64.tar.gz`
- Windows x86_64: `codex-hsog-windows-x86_64.zip`

Windows ARM64 is currently not provided.

The launcher is the supported entry point for students. It injects the HSOG provider configuration on the command line and prompts for `HSOG_API_KEY` on first use, so no manual `config.toml` editing is required.

### Launcher usage

macOS / Linux:

```shell
./start-codex-hsog.sh
```

Windows PowerShell:

```powershell
.\start-codex-hsog.ps1
```

You can pass normal Codex arguments through the launcher, for example:

```shell
./start-codex-hsog.sh --help
```

## Docs

- [**Contributing**](./docs/contributing.md)

This repository is licensed under the [Apache-2.0 License](LICENSE).
