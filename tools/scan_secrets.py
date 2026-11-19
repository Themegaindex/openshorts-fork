"""Signature scan of tracked content; emit locations and rule names, never values.

This is intentionally limited: a zero result is not proof of secret absence.
Ignored .env/cookies/runtime media are not opened. --history includes Git blobs.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess


RULES = {
    "private-key": rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
    "aws-access-id": rb"(?:AKIA|ASIA)[A-Z0-9]{16}",
    "github-token": rb"(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{50,255})",
    "google-api-key": rb"AIza[A-Za-z0-9_-]{35}",
    "openai-style-key": rb"sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{40,255}",
    "slack-token": rb"xox[baprs]-[A-Za-z0-9-]{20,255}",
}


def scan(content, path, revision):
    if b"\x00" in content:
        return [], False
    findings = []
    for rule, pattern in RULES.items():
        for match in re.finditer(pattern, content):
            findings.append({"path": path, "line": content.count(b"\n", 0, match.start()) + 1, "rule": rule, "revision": revision})
    return findings, True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    findings = []
    current_text = 0
    historical_text = 0
    # NUL delimiters support filenames containing spaces and Unicode.
    files = subprocess.check_output(["git", "ls-files", "-z"], cwd=repo).decode("utf-8").split("\0")
    # Include audit deliverables before they are staged, while leaving unrelated
    # personal untracked files (notably .claude/settings.local.json) untouched.
    audit_files = ["SECURITY.md", "security.py", "security_http.py", "security-constraints.txt", "requirements-security.txt", "tests/test_security.py", "tools/scan_secrets.py", "tools/reproduce_security_baseline.py"]
    audit_files += [str(path.relative_to(repo)).replace("\\", "/") for path in (repo / "docs").rglob("*") if path.is_file()]
    files = sorted(set(filter(None, files)) | {filename for filename in audit_files if (repo / filename).is_file()})
    for filename in filter(None, files):
        path = repo / filename
        if path.is_file():
            found, text = scan(path.read_bytes(), filename, "working-tree")
            findings.extend(found)
            current_text += int(text)
    if args.history:
        objects = subprocess.check_output(["git", "-c", "core.quotepath=false", "rev-list", "--objects", "--all"], cwd=repo).decode("utf-8").splitlines()
        paths = {line.split(" ", 1)[0]: line.partition(" ")[2] for line in objects}
        metadata = subprocess.run(["git", "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"], input="\n".join(paths) + "\n", cwd=repo, capture_output=True, text=True, check=True).stdout
        blobs = [line.split()[0] for line in metadata.splitlines() if line.split()[1] == "blob"]
        with subprocess.Popen(["git", "cat-file", "--batch"], cwd=repo, stdin=subprocess.PIPE, stdout=subprocess.PIPE) as process:
            for oid in blobs:
                process.stdin.write((oid + "\n").encode("ascii"))
                process.stdin.flush()
                header = process.stdout.readline().split()
                content = process.stdout.read(int(header[2]))
                process.stdout.read(1)
                found, text = scan(content, paths[oid], oid)
                findings.extend(found)
                historical_text += int(text)
            process.stdin.close()
            if process.wait() != 0:
                raise RuntimeError("Git blob scan failed")
    print(json.dumps({"current_text_files_including_audit_deliverables": current_text, "historical_text_blobs": historical_text, "rules": list(RULES), "findings": findings, "limitation": "Signature scan only; ignored/personal untracked files and unknown secret formats are excluded."}, indent=2))
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
