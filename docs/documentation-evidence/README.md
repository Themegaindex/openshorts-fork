# Documentation follow-up evidence · 2026-11-06

These records cover the README, Node/Vite prerequisite, upstream-remote, and supplemental secret-scan corrections. Earlier [audit evidence](../security-evidence/README.md) and [fork evidence](../fork-evidence/README.md) are historical snapshots and are preserved unchanged.

Machine-readable results and command logs in this directory describe only checks actually executed. Temporary clone paths and machine-specific absolute paths are replaced by placeholders. No private environment, cookies, user media, or personal untracked configuration is copied into the review workspace.

Full backend/ML installation, GPU/provider workflows, Docker builds/runtime, non-Windows execution, and remote CI/external review gates remain outside the completed checks. A local documentation review does not close the existing application/dependency risks. See [DOCUMENTATION_REVIEW.md](../DOCUMENTATION_REVIEW.md).

- [Execution summary](execution.json): actual commands, exit codes, timings, and omitted checks. Individual `.txt` logs retain the corresponding output.
- [Integrity checks](integrity.json): protected source/test contents, historical raw evidence, original license, and old manifest differences.
- [Relative links and anchors](links.json), [PowerShell syntax](powershell-syntax.json), and [official/public sources](sources.json).
- [History signature scan](secret-history-scan.txt) and [supplemental scan](supplemental-secret-scan.txt). The [supplemental script](supplemental-secret-scan.ps1) is extracted verbatim from the documented PowerShell block.
- [Full staged whitespace check](diff-check.txt): exit 2 only for four original formatting locations in three immutable historical raw logs. The [check excluding those raw evidence directories](authored-diff-check.txt) covers the remaining candidate; logs were not edited to silence diagnostics.
- [Prepared index manifest](publication-index.json): Git blob IDs and SHA-256 of the staged publication files, excluding the manifest itself. This records canonical Git bytes rather than machine-specific working-tree line endings; it is neither a commit nor an external signature.

The historical raw records use original bytes protected by `.gitattributes`; source files remain subject to normal Git line-ending rules. Older manifests describe the original working tree, not every later normalized Git blob. The integrity report enumerates intentional documentation changes and the one source-file EOL-only difference.
