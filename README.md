# Hermes–Herdr Bridge

An experimental local bridge that reports Hermes Agent session states to Herdr.app/herdr-core and the legacy Herdr TUI. It combines a same-terminal launcher with a Hermes plugin; it does not modify Hermes core or its generated launcher.

**Experimental, not production-ready.** Private Hermes gateway APIs and macOS process inspection can change. A working installation on one machine does not guarantee compatibility after an upgrade or across multiple gateways. Review [limitations](#known-limitations) before use.

## What it does

- Reports session activity such as `idle`, `working`, `blocked` and `done` to Herdr.app; the legacy TUI has a separate report/release path.
- Can opt into macOS per-viewer identity so viewers of the same session share its state and a viewer can follow a session switch.
- Fails closed when ownership is uncertain: status can clear or become `unknown` rather than being guessed.
- Uses private local sockets for bridge metadata; it does not intentionally send prompts or answers over the bridge. It does not add cloud services or telemetry.

## Compatibility

The peer-identity mode has been exercised on macOS with a real Hermes gateway and private herdr-core, using a fake model; it is **macOS-only** and depends on `libproc`. A shell-launched live pilot was tested against Hermes Agent v0.23.1 (source revision `3b4215785882`), herdr-core `0.1.21`, and legacy Herdr `0.9.5`. These are observations, not a supported-version guarantee. The legacy live result was user-observed, not independently traced to the bridge.

Other OSes and versions are not validated. With peer identity disabled, a more limited breadcrumb-based mode is available, but its initial binding and behavior across session changes have not been proven safe in all cases. It clears reporting on a session change rather than following it. Python 3.9 or newer, an installed Hermes Agent and the relevant Herdr installation are required. No third-party Python package is needed by the bridge itself; the optional integration tests use the installed Hermes environment and its test fixtures.

## How it works

1. Start Hermes through `bin/hermes` in the same Herdr terminal. It looks for a `hermes` executable on `PATH`, or an explicit absolute `HERMES_HERDR_REAL`, without modifying it. It rejects itself and recognizable copies of this bridge; detection is signature-based and cannot guarantee detection of modified wrappers.
2. A Hermes directory plugin receives session and human-input hooks in the gateway and publishes bounded snapshots to a private profile-local socket.
3. The launcher correlates the session to its terminal and sends OSC 7501 to herdr-core and/or reports to the legacy Herdr pane API.

The plugin uses tool-execution middleware to bind clarification events to a turn using private Hermes context APIs. It is not a purely observational plugin. Verify compatibility after every Hermes update and do not rely on reported status for security or irreversible automation.

## Manual pilot installation

These instructions make changes to a Hermes profile. They are opt-in; review them first, and do not run them against an occupied plugin directory. The recipe pins `HERMES_HOME` to the explicit value if supplied, otherwise the default profile at `~/.hermes`; a named or sticky profile requires explicitly selecting its home for installation and launches. An enable operation may cause Hermes to reload plugins in a running gateway; protect active work. No administrator privileges are required.

From the repository root:

```sh
(
  set -eu
  home="${HERMES_HOME:-$HOME/.hermes}"
  export HERMES_HOME="$home"
  plugin="$home/plugins/hermes_herdr_bridge"
  if [ -e "$plugin" ] || [ -L "$plugin" ]; then
    printf '%s\n' 'Bridge directory already exists; inspect it before proceeding.' >&2
    exit 1
  fi
  umask 077
  mkdir -p "$home/plugins"
  mkdir "$plugin"
  for f in __init__.py observer.py relay.py state.py introspect.py peer.py; do
    cp "$f" "$plugin/$f"
  done
  printf '%s\n' '{"enabled": true}' > "$plugin/peer-identity.json"
  cp plugin.yaml "$plugin/plugin.yaml"
  hermes plugins doctor "$plugin" --ci
  hermes plugins enable hermes_herdr_bridge --no-allow-tool-override
)
```

If a step fails after the directory is created, inspect that directory before cleaning up or retrying; the recipe will not overwrite it.

The private `peer-identity.json` explicitly enables experimental identity for this profile. Omit it to use the limited breadcrumb mode. An explicit `HERMES_HERDR_PEER_IDENTITY=1` or `0` overrides the file in each process: the gateway and launcher must agree. The file and all parent directories must not be symlinks, and the file must be owned by the current user and not group/world-writable.

The recipe validates and enables the plugin. Read the enable output. If Hermes reports a live reload, hooks may already be active; otherwise the gateway must be reloaded or restarted according to Hermes' instructions. Restarting a gateway can interrupt active sessions; do it only from a separate terminal after saving work. The bridge has not been validated against all plugin coexistence scenarios.

To launch through the wrapper without changing global settings, use the absolute path to this repository's `bin/hermes` in a Herdr terminal. For ordinary interactive shell launches, you can create a shell alias pointing `hermes` at that path. Ensure the real Hermes executable is still on `PATH`, or set `HERMES_HERDR_REAL` to its absolute path. Do not point it back to the wrapper. For a shell alias example from a repository path that contains no single quote:

```sh
alias hermes="'$(pwd -P)/bin/hermes'"
```

This alias is scoped to the current shell; persist it yourself only if desired. An app-created direct `argv: ["hermes"]` launch can bypass shell aliases and run the vendor executable, resulting in no reported status. Configure the app's launch command to point to the wrapper explicitly if the app supports that; this route is not yet fully verified. Test with a disposable pane first.

### Disable or remove

- To disable peer identity for future scans, remove the profile opt-in file or set `HERMES_HERDR_PEER_IDENTITY=0` consistently for both gateway and launcher. A process with an explicit environment override ignores the file. Existing terminals may need to be relaunched.
- Disable the plugin with `hermes plugins disable hermes_herdr_bridge`; check whether Hermes reports a live reload or a restart requirement.
- After verifying that the directory belongs to this bridge and is no longer in use, remove only `plugins/hermes_herdr_bridge` from the selected Hermes profile. Remove any alias you added. Do not remove another Herdr integration or its settings.

## Tests

On macOS, with a short private temporary directory on the same machine:

```sh
export TMPDIR="$HOME/.hermes/cache/scratch"
export PYTHONDONTWRITEBYTECODE=1
python3 -m unittest discover -s tests -v
python3 -m unittest -v test_bridge
```

The scratch directory must already exist. Seven integration tests are opt-in and skip in the default suite; the count can change with the test suite. The test suite includes real PTY and fork-churn probes that create and clean up their own child processes. Tests are intended for macOS; do not assume the Linux/Windows peer-identity path works.

The optional real-gateway tests require a compatible local Hermes source checkout, Hermes Python environment and `hc` binary. They use a private Hermes home, a fake loopback model and private backend; they do not validate your live gateway or the actual app UI. See [TESTING.md](TESTING.md) for flags and limitations.

## Known limitations

- Only a narrow set of interactive CLI arguments is wrapped. `--profile`, `--tui`, one-shot and management commands pass through without bridge reporting. The legacy Herdr TUI is separate from Hermes' own `--tui` mode.
- Direct app launches can bypass a shell alias. Shell-typed launches and app launch menus must be tested independently.
- Brief status dropouts have been observed with shared viewers. Updates can be delayed, coalesced or dropped; no fixed switch-latency bound is guaranteed.
- Heavy process churn, unreadable identities or a persistent zombie can leave status `unknown`. A directly delivered SIGINT to the wrapper alone is not forwarded; other signal and job-control paths remain incompletely verified.
- Manual compression followed by a turn has been tested; automatic compression and all human-input paths have not. During a live manual compression, the displayed state remained `done` until a subsequent turn.
- A live legacy lifecycle sequence was reported by a user, but independent evidence of the exact status source and release on exit remains outstanding.
- Multiple gateway processes, long suspension, platform upgrades, and all private API changes are not covered. Unsupported or unrecognized inputs may fail closed, but no security or compatibility guarantee is made.

## Project status and contributions

This is a pre-release prototype. Please include Hermes, herdr-core/TUI, macOS and Python versions and a sanitized reproduction when filing a bug. Never include prompts, session transcripts, tokens, socket contents or credential files. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

Related upstream discussion: https://github.com/herdrdev/herdr-app/issues/358. A separate Linux workaround is discussed there; this project does not claim to be the only implementation.

## License

An MIT license is included as a proposed distribution license; see [LICENSE](LICENSE). Confirm ownership, employer rights and third-party provenance before publishing or redistributing.
