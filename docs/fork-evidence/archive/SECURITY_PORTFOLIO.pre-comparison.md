# Independent Security Audit & Hardening – Video-Cutter

This repository-owner, AI-assisted security assessment examined OpenShorts, a Python/FastAPI and React video-processing application. It covered process execution, filesystem boundaries, uploads, external downloads, generated HTML, API exposure, dependency advisories, secrets handling, launch scripts, and resource consumption.

Eight application findings were confirmed using source-level dataflow and harmless local reproductions: path traversal, SSRF, stored XSS, unauthenticated network access, private metadata exposure, unbounded media transfers, uncontrolled generation counts, and conditional command injection through the Windows checkout path. Fixes introduce constrained paths and server-generated filenames, public-address checks with DNS pinning and redirect validation, context-aware output encoding, local-or-token access, media-only serving, and bounded work.

Thirteen security assertions fail against the original Git snapshot. The hardening includes automated regression tests and preserved scanner outputs. Dependency advisories are reported separately from demonstrated application exploits; the original runtime still needs a controlled dependency upgrade.

Evidence, commands, limitations, and local review findings are available in [the audit](SECURITY_AUDIT.md) and [testing guide](SECURITY_TESTING.md). This is a portfolio assessment, not third-party attestation, certification, or a claim of complete security. Docker, remote CI and the repository's external Claude/Codex review gates remain pending.
