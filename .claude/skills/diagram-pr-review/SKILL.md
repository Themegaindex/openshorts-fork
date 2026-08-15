---
name: diagram-pr-review
description: Run the repository's post-CI pull-request gate with exactly three parallel reviewers, validate findings, apply at most two fix rounds, and then request Codex code and security review.
allowed-tools: Read, Write, Edit, Glob, Grep, Agent, Bash(gh pr diff:*), Bash(gh pr view:*), Bash(gh pr comment:*), Bash(git:*), Bash(python:*), Bash(pytest:*), Bash(pip:*), Bash(npm:*), Bash(npx:*)
---

# Post-CI review gate

The prompt supplies the repository, PR number, head branch, head SHA, and the
backend/frontend dependency setup outcomes. CI is already green for that SHA.

1. Read `CLAUDE.md`, `AGENTS.md`, the PR title/body/comments, and the complete diff.
2. Skip closed, draft, empty, or already-passed PRs. A pass is current only when `<!-- claude-diagram-review:pass sha=HEAD_SHA -->` matches the supplied full SHA.
3. Launch exactly three independent reviewers in parallel and give each the PR intent and diff:
   - **Correctness reviewer:** logic, regressions, edge cases, error handling, and missing regression tests.
   - **Security reviewer:** trust boundaries, injection, secrets, permissions, unsafe files/URLs/subprocesses, races, cleanup, and resource exhaustion.
   - **Repository reviewer:** `CLAUDE.md`/`AGENTS.md` compliance, API compatibility, CI coverage, and maintainability defects that can cause concrete failures.
4. Require every proposed finding to identify changed file/line evidence, a concrete failure path, severity, and a minimal fix. Ignore style, speculation, linter findings, and pre-existing defects.
5. Validate every candidate against the actual changed code. Deduplicate overlapping findings. Keep only confirmed, high-confidence defects introduced by the PR. Use the dependency setup outcomes when judging which local validations were available; if one failed, retry that install once and do not turn an infrastructure-only failure into a PR finding or fix round.
6. If no confirmed defects remain:
   - post one concise German PR comment containing `<!-- claude-diagram-review:pass sha=HEAD_SHA -->`, replacing `HEAD_SHA` with the supplied full commit SHA;
   - state that the three reviewer lanes passed;
   - include `@codex review` and `@codex security review` on separate lines;
   - make no code changes and stop.
7. If confirmed defects remain, count unique `<!-- claude-review-fix-round:N -->` markers in PR comments.
8. If two rounds already exist, post a German blocked summary with `<!-- claude-review-fix-limit -->`, list the confirmed defects with file/line evidence, and stop without editing.
9. Otherwise apply only the validated minimal fixes, add regression tests, and run the focused reproduction plus every affected validation command from `CLAUDE.md`.
10. Re-check the diff, leave it ready for the action to commit, and post one concise German PR comment with the next round marker, fixed findings, and exact successful commands.

Make at most one fix commit per invocation. Never merge. Every changed SHA must return through CI before the three reviewers run again.
