# Independent Security Research, Development & Hardening of an Open-Source Fork

**Project:** [OpenShorts fork maintained by Themegaindex](https://github.com/Themegaindex/openshorts-fork), derived from [mutonby/openshorts](https://github.com/mutonby/openshorts). **Evidence date:** November 5, 2026.

This portfolio documents substantial fork development, source-based security research, harmless local reproductions, regression-driven hardening and a comparison with current upstream. The assessment and local hardening were AI-assisted. “Independent” describes a self-directed repository-owner project; it does not mean independent third-party certification or an externally commissioned audit.

## Project and contribution boundaries

OpenShorts already provided the video-clipping platform, React/FastAPI application, YouTube Studio, UGC generation, voice dubbing, S3 integration and social publishing. Those original capabilities are credited to upstream. The fork extends and revises that platform rather than claiming to have created it from scratch.

The public fork snapshot is [`98afa9e`](https://github.com/Themegaindex/openshorts-fork/commit/98afa9ea5e9a842eb358834fe0cbd251d335573b). Current upstream was inspected at [`8945140`](https://github.com/mutonby/openshorts/commit/894514049a3c6210a7cd65676344f44371b3717a). The inherited fork snapshot `0ffe730` has exactly the same Git tree as upstream `8f4bc54`; different commit IDs must not be mistaken for newly authored work.

From that identical inherited tree to the published fork, Git records **90 Themegaindex-attributed commits: 79 non-merge commits and 11 merges**, covering 93 files and adding 15 root Python modules. Seventeen additional fork-side commits are inherited work with patch-equivalent upstream counterparts. These metrics establish repository history, not exclusive manual authorship, work hours or a quality score.

Evidence-backed development includes:

- Multiple output formats, persisted render configuration and configurable watermarks.
- Deterministic edit-decision rendering, caption-safe effects, karaoke presets and removable presentation layers.
- Window scoring and shortlist/detail analysis, word-aligned cuts and structured model responses.
- Long Video and Smart/Auto workflows, timeline planning and review gates.
- Stabilized speaker/layout tracking, sequential frame sampling and exact delivery dimensions.
- Persistent job recovery, cancellation, clip-state coordination, Windows process containment and timing calibration.
- Shared YouTube sessions with opt-in browser cookies, Whisper GPU/CPU fallback and NVENC/CPU fallback.
- Regression tests, lightweight test dependencies, CI configuration and bounded issue/review workflows.

The [contribution report](FORK_CONTRIBUTIONS.md) maps each area to actual files and commits. No new GPU-performance benchmark or end-to-end provider result is claimed by this portfolio.

## Observable upstream adoption

[Upstream PR #50](https://github.com/mutonby/openshorts/pull/50), authored by Themegaindex, is **closed and unmerged**. It would be inaccurate to describe it as a merged PR.

However, two subsequent upstream commits explicitly document adapted adoption from that PR:

- [`40151e1`](https://github.com/mutonby/openshorts/commit/40151e1708100327f647bdd190adcf4808838a60) incorporates karaoke subtitles, deterministic Auto-Edit, emoji hooks, retries and related tests; its metadata credits Themegaindex as co-author.
- [`b3e4464`](https://github.com/mutonby/openshorts/commit/b3e44643cc1fd0cf3822f202726d451d2e0ef1e4) names PR #50 as the source of the two-pass clip-selection pipeline.

The initial fork and upstream adoption commits also contain an identical `edit_builder.py` Git blob. This provides concrete code evidence in addition to maintainers’ commit descriptions. It does not imply that upstream adopted every fork feature or that its current files remain identical.

## Security research and implemented hardening

The local audit traced untrusted filenames, URLs, uploads, model responses, HTML metadata, process arguments, resource parameters and dependency versions through the application. It confirmed **eight application findings: four High, three Medium and one conditional Low; none Critical**.

| Finding | Local correction | Representative evidence |
| --- | --- | --- |
| Path traversal / external file selection | Constrained storage paths, identifier validation, generated filenames and exclusive file creation | Synthetic marker-file and traversal regression cases |
| SSRF in scraping and media downloads | Public-address checks, pinned connection IP with original Host/SNI, redirect revalidation and YouTube entry restrictions | Mock redirect to loopback rejected before the internal request |
| Stored XSS in gallery output | Context-aware HTML encoding, URL-scheme restrictions and safe JSON-LD serialization | Synthetic metadata tested against the actual renderer |
| Unauthenticated network exposure | Local-or-token access, explicit hosts/origins and loopback port bindings | Network-free ASGI requests, including mounted URL prefixes |
| Private metadata exposed through media serving | Media-only allowlist and hidden-file restrictions | Artificial private JSON changes from 200 to 404 |
| Unbounded transfers | Streaming limits, request-body bounds, temporary files and timeouts | Byte-limit rejection and preservation of an existing destination |
| Uncontrolled generation counts | Bounded options/scripts/segments and shared concurrency/admission controls | Invalid counts rejected before worker/provider calls |
| Windows launcher path injection | Quoted `start /D` working directory | Original/new launch commands exercised with harmless echo markers |

Several risky flows were inherited from upstream’s older code, while the launcher and additional state paths were fork contributions. Their provenance is recorded rather than attributed wholesale to the fork author. These are verified fixes in the tested local flows, not claims of novel zero-days or CVE assignments. Upstream has independently addressed related problems.

The hardening remains **uncommitted and unpublished**. It is present in this working copy and is not represented as already available at the public fork commit.

## Completed local verification

The following results were actually obtained during the preceding local audit and are preserved in [security-evidence](security-evidence/README.md):

| Check | Recorded outcome |
| --- | --- |
| Full Python suite | **632 passed, 0 failed, 8 skipped** |
| New security suite, included in the total | **65 passed, 0 failed, 1 skipped** |
| Selected assertions on the original Git snapshot | **13 expected failures**, confirming the old behavior |
| Frontend lint and production build | Passed |
| Hardened npm dependency scan | Zero reported vulnerable packages; confirmed again during this comparison |
| Fresh lightweight Python test/tool environment | Zero reported dependency advisories in the recorded scans |
| Bandit | 65 Low and 2 Medium alerts; gate remains nonzero and is explicitly triaged |
| Secret-signature scan | No candidates within its documented scope; not proof of universal secret absence |
| Local read-only code reviews | Three review lanes, two rounds; intermediate regressions repaired |

All 69 files covered by the earlier audit manifest matched before this documentation update. This extension did not rerun the full application suite or reuse historical PR claims as new test results. It performed current GitHub/Git provenance checks, targeted isolated helper probes, dependency lookups and document/evidence validation. Application hardening and existing test files were preserved.

## Current upstream comparison and open work

Current upstream is a substantially different application with additional cloud, identity, OAuth/MCP, billing and media-sharing architecture. Commit divergence alone cannot establish which security corrections are missing. Relevant patches were compared by their actual behavior.

The comparison identifies incomplete protection or missing equivalents in the local fork:

- **NAT64 IP policy:** the local helper accepts two tested NAT64 forms embedding private IPv4 addresses, whereas the upstream predicate rejects them. This is a confirmed policy gap; internal reachability requires a translating network and was not tested. It remains an open qualification to the SSRF hardening.
- **Outbound coverage:** upstream guards connections inside yt-dlp/probes and checks URLs passed to external downloaders. The local pinned-HTTPX helper does not establish equivalent protection for every SDK, CDN or FFmpeg path.
- **Secret minimization:** centralized credential-URL scrubbing and reduced worker environments are not fully matched. A synthetic credential string remains unchanged in the local log-entry helper; no real credential was used or exposed by the probe.
- **Metadata integrity:** a deterministic local interleaving shows the worker’s JSON helper exposes an empty target while writing; the inspected upstream atomic helper keeps the previous JSON readable. API-side atomic writes already exist, but do not cover that worker path.
- **Multi-user controls:** per-owner media capabilities and durable quotas are absent from the fork’s single-operator model. Upstream cloud-only OAuth/billing fixes are not automatically applicable core patches.

These differences are documented backlog items, not additional completed fixes or invented counts of remotely exploited vulnerabilities. [FORK_CONTRIBUTIONS.md](FORK_CONTRIBUTIONS.md) contains the commit-by-commit triage and [targeted-probes.json](fork-evidence/targeted-probes.json) records the harmless observations.

A new upstream npm-lockfile scan reports 15 affected packages; the hardened local lockfile reports zero. The dependency trees differ, and advisory counts do not rank overall application security. The existing local video runtime still has the earlier **163 advisory entries across 18 packages** and needs a controlled upgrade. New constraints do not update that environment automatically.

Docker builds, container smoke tests, real GPU/provider integration, remote CI and the repository’s external Claude/Codex gates remain pending. Browser-stored API keys, native media-parser isolation, cumulative disk/cost quotas and deliberate public-gallery behavior remain operational considerations. Public deployment requires authenticated HTTPS ingress and a suitable isolation model; local tests do not establish production readiness.

## Evidence, preservation and licensing

- [Detailed development and upstream comparison](FORK_CONTRIBUTIONS.md)
- [Security audit and comparison addendum](SECURITY_AUDIT.md)
- [Threat model](THREAT_MODEL.md) and [security-test reproduction](SECURITY_TESTING.md)
- [Original test/scanner evidence](security-evidence/README.md) and [new comparison evidence](fork-evidence/README.md)

The original MIT license and **Copyright (c) 2024 OpenShorts** are unchanged. Current upstream separately licenses its `cloud/` directory; that code was not automatically imported or presented as original fork work. The earlier portfolio/audit texts are retained unchanged in the comparison archive. Nothing was automatically published, pushed, merged or deployed.
