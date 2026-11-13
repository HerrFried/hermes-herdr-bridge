# Testing

All commands below assume the repository root. Tests are designed for macOS. They create private temporary files, child processes and local sockets; do not run them in an occupied legacy Herdr pane or against someone else's gateway.

## Default suite

Set `TMPDIR` to an existing private, short path suitable for macOS Unix sockets. If Hermes is installed in its default location, this is one option:

```sh
export TMPDIR="$HOME/.hermes/cache/scratch"
export PYTHONDONTWRITEBYTECODE=1
python3 -W always::ResourceWarning -m unittest discover -s tests -v
python3 -m unittest -v test_bridge
```

Optional integration tests are skipped unless explicitly enabled. Some tests run real PTYs, manipulate signals and create short-lived child process groups; they should be run on a disposable development environment. The fork-churn test is intentionally resource-intensive for several seconds.

## Optional Hermes runtime contracts

These tests require a compatible Hermes checkout at `~/.hermes/hermes-agent` and a Python interpreter with Hermes' dependencies. Set `HERMES_PYTHON` to that interpreter's absolute path; do not alter your installed Hermes just to run the test.

```sh
HERMES_BRIDGE_RUNTIME_TEST=1 "$HERMES_PYTHON" \
  -m unittest discover -s tests -p test_hermes_runtime.py -v
```

One negative test deliberately makes a required middleware contract unavailable and expects plugin registration to fail. Interface tests alone cannot prove compatibility after upgrades.

## Optional private gateway and herdr-core

Requires a compatible, clean tracked Hermes source checkout, its generated normal launchers, the Hermes Python dependencies and `hc`. Set `HERMES_SOURCE` to the checkout if it is not in the default location, `HC_BINARY` to `hc` if it is not in `~/.local/bin/hc`, and `HERMES_PYTHON` to the Hermes Python interpreter.

```sh
HERMES_REAL_GATEWAY_TEST=1 HERMES_TEST_FULL_BOOTSTRAP=1 \
  HERMES_TEST_PEER_CONFIG=1 "$HERMES_PYTHON" \
  -m unittest discover -s tests -p test_real_gateway_hc.py -v
```

The harness creates a temporary home, a private gateway and core, and a fake local model. It snapshots tracked Hermes source, copies and rewrites launchers, and uses the supplied interpreter. It does not use the live profile, gateway, app UI or real credentials. It requires the particular launcher/bootstrap shape the harness knows; failures on a different installation are not necessarily bridge failures. For an environment-variable opt-in instead of the profile file, replace `HERMES_TEST_PEER_CONFIG=1` with `HERMES_TEST_PEER_IDENTITY=1`. Omit both for the limited mode.

## Optional legacy backend harness

```sh
HERMES_REAL_LEGACY_PROBE_TEST=1 HERMES_HERDR_PEER_IDENTITY=1 \
  "$HERMES_PYTHON" -m unittest discover -s tests -p test_legacy_gateway_live.py -v
```

This uses a real private Hermes gateway and a **fake** legacy Herdr backend. It does not prove the real legacy UI. An additional interactive probe is present in `legacy_gateway_probe.py` for an explicitly authorized empty legacy pane. Read the file and the installed Herdr CLI guidance before running it; it uses a private home and fake model, and requires Hermes source at its documented default location.

## Before calling the bridge verified

Test the actual installed launcher and real Herdr.app and legacy TUI separately. Observe startup, working, human-input blocked, resume, completion and exit cleanup; test two viewers of a shared session and a switch in one. Exercise actual app launch paths, not only shell aliases. Do not claim automatic compression, cross-platform operation, upgrade safety, multiprocess-gateway behavior or release-on-exit based solely on the private harness.
