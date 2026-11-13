# Security

This is experimental software, not a security boundary. It runs a plugin in the Hermes process and accesses local process and socket metadata. It uses private Hermes APIs and tool-execution middleware for clarification correlation. Review the source and use only in environments where you accept those risks.

The bridge aims to keep prompt and answer content out of its transport, but this has not undergone a comprehensive external security audit. Its profile-local runtime records include the Hermes home, working directory, terminal device, process identifier, process start time and a random nonce; session identifiers and states are sent over local sockets. Peer identity inspects descendant TCP endpoint metadata. Runtime directories and files are checked for ownership and restrictive permissions, but crashes can leave stale artifacts and same-user races are not ruled out. Diagnostic logs or terminal output from Hermes and Herdr can contain sensitive material: inspect and redact them before sharing.

Please do not post exploitable vulnerabilities, secrets, private terminal output, prompts, session identifiers or credential files in public issues. 

If a credential was exposed, revoke or rotate it using the provider's process; deleting it from a repository alone does not make it safe.
