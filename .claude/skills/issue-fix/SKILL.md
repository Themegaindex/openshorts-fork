---
name: issue-fix
description: Reproduce a trusted GitHub issue, establish the baseline, implement a minimal fix with regression coverage, validate it, and prepare the change for a pull request.
allowed-tools: Read, Write, Edit, Glob, Grep, Agent, Bash(gh issue view:*), Bash(gh issue comment:*), Bash(git:*), Bash(python:*), Bash(pytest:*), Bash(pip:*), Bash(npm:*), Bash(npx:*)
---

# Fix a GitHub issue

The prompt supplies the repository and issue number.

1. Read `CLAUDE.md`, `AGENTS.md`, and the complete issue with `gh issue view`.
2. Stop without editing when the request is ambiguous, unsafe, unrelated to this repository, or cannot be verified. Explain the blocker in a concise German issue comment.
3. Establish and record the baseline before editing:
   - run the narrowest relevant existing test;
   - run `python -m pytest tests/ -q` for backend changes;
   - run `npm --prefix dashboard run lint` and `npm --prefix dashboard run build` for frontend changes.
4. Reproduce the reported defect. Prefer a failing regression test. Never invent a reproduction or treat an unrelated pre-existing failure as proof.
5. Implement the smallest complete fix. Add or update regression coverage.
6. Re-run the reproduction, the relevant focused tests, and the full affected validation commands. Do not weaken tests or suppress failures.
7. Review the diff for secrets, unsafe input handling, command/path injection, permission errors, races, and resource leaks.
8. Leave the working tree ready for the Claude GitHub Action to commit. Do not merge and do not edit workflow authentication or repository settings.
9. Post one concise German issue comment containing:
   - baseline result;
   - reproduction evidence;
   - changed behavior and tests;
   - exact validation commands and results;
   - any remaining limitation.
