---
name: fix-ci
description: Diagnose a failed GitHub CI run for a same-repository pull request, apply one minimal verified fix, and stop after two automated CI-fix rounds.
allowed-tools: Read, Write, Edit, Glob, Grep, Agent, Bash(gh run view:*), Bash(gh pr view:*), Bash(gh pr comment:*), Bash(git:*), Bash(python:*), Bash(pytest:*), Bash(pip:*), Bash(npm:*), Bash(npx:*)
---

# Fix a failed CI run

The prompt supplies the repository, PR number, CI run ID, head branch, and head SHA.

1. Read `CLAUDE.md` and `AGENTS.md`.
2. Read existing PR comments. Count unique `<!-- claude-ci-fix-round:N -->` markers.
3. If two rounds already exist, post a German blocked comment with `<!-- claude-ci-fix-limit -->` and stop without editing.
4. Inspect only the failed jobs with `gh run view RUN_ID --log-failed`.
5. Separate infrastructure/network failures and pre-existing failures from failures introduced by the PR. Do not change code for an infrastructure-only failure; comment with the evidence and stop.
6. Reproduce the relevant failure locally. Implement the smallest fix and add a regression test when practical.
7. Run the focused reproduction and the full affected validation commands from `CLAUDE.md`.
8. Review the diff for unsafe or unrelated changes. Leave the working tree ready for the action to commit. Never merge.
9. Post one concise German PR comment with the next round marker, root cause, files changed, and exact successful commands. Example for the first round: `<!-- claude-ci-fix-round:1 -->`.

Make at most one fix commit per invocation. The pushed result must return through the full CI workflow.
