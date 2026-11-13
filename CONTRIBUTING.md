# Contributing

This project is experimental. Small, independently testable changes are preferred.

## Before changing behavior

- Preserve the user's existing Hermes launcher, profiles, arguments, working directory, terminal behavior and exit status.
- Never use another person's active pane or restart their gateway or Herdr server as a test fixture. Use private temporary homes, fake models and test-owned processes.
- Treat uncertain viewer/session identity and incomplete lifecycle information as unknown; do not manufacture a state.
- Keep the two Herdr reporting backends separate. Changes need coverage for shared viewers, session switches and failure/cleanup paths when applicable.
- Do not commit secrets, credentials, transcripts, personal paths, local state, or downloaded dependencies.

## Verification

Run the default macOS suite and `test_bridge` as described in [TESTING.md](TESTING.md). Add regression tests for changes and run the relevant opt-in integration tests when the required Hermes checkout and dependencies are available. Report what you actually ran and what remains unverified; CI does not substitute for live UI testing.

For pull requests, describe the problem, the observable behavior change, tests and platform/compatibility implications. Changes involving private Hermes APIs should explain how incompatible versions fail and which upgrade tests were run.

## Privacy and reports

Strip private paths, hostnames, terminal history, prompts, session IDs, keys and tokens from logs and reproductions. See [SECURITY.md](SECURITY.md) before posting a security issue.
