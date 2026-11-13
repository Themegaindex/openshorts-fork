"""Harmless targeted comparison with the pinned, reviewed upstream snapshot.

No application import, provider requests, real secrets or user media. Only the
named, reviewed pure/JSON helper definitions are executed in isolated globals.
NAT64 reachability is not tested; acceptance by an IP predicate is not an exploit.
Requires the pinned upstream Git objects (see docs/FORK_CONTRIBUTIONS.md).
"""
import ast
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from typing import Optional
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
UPSTREAM = "894514049a3c6210a7cd65676344f44371b3717a"
sys.path.insert(0, str(REPO))
import security


def definition(source, name, namespace):
    tree = ast.parse(source)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<reviewed-isolated-definition>", "exec"), namespace)
    return namespace[name]


def upstream_source(path):
    return subprocess.check_output(["git", "show", f"{UPSTREAM}:{path}"], cwd=REPO, text=True, encoding="utf-8")


def main():
    namespace = {"ipaddress": ipaddress, "_NAT64": ipaddress.ip_network("64:ff9b::/96")}
    public = definition(upstream_source("security_utils.py"), "_ip_is_public", namespace)
    ip_results = []
    for address in ("127.0.0.1", "100.64.0.1", "::ffff:127.0.0.1", "64:ff9b::7f00:1", "64:ff9b::a00:1", "93.184.216.34"):
        host = f"[{address}]" if ":" in address else address
        try:
            security._public_target(f"http://{host}/synthetic")
            accepted = True
        except ValueError:
            accepted = False
        ip_results.append({"address": address, "fork_public_target_accepts": accepted, "upstream_ip_predicate_accepts": public(address)})

    unsafe = type("UnsafeURLError", (ValueError,), {})
    from urllib.parse import urlparse
    namespace = {"socket": socket, "urlparse": urlparse, "ipaddress": ipaddress, "UnsafeURLError": unsafe, "_ip_is_public": public}
    upstream_assert = definition(upstream_source("security_utils.py"), "assert_public_url", namespace)
    errors = {}
    with patch("socket.getaddrinfo", side_effect=UnicodeError("synthetic invalid IDNA label")):
        for label, call in (("fork", lambda: security._public_target("http://synthetic.example/")), ("upstream", lambda: upstream_assert("http://synthetic.example/"))):
            try:
                call()
            except Exception as exc:
                errors[label] = {"type": type(exc).__name__, "is_validation_error": isinstance(exc, ValueError)}

    atomic = []
    with tempfile.TemporaryDirectory(prefix="openshorts-atomic-probe-") as directory:
        target = Path(directory) / "synthetic_metadata.json"
        for label, source, name in (("fork", (REPO / "main.py").read_text(encoding="utf-8"), "_save_json_file"), ("upstream", upstream_source("jsonfile.py"), "write_json_atomic")):
            target.write_text('{"version":"old"}', encoding="utf-8")
            observed = {}

            def dump(payload, handle, **kwargs):
                try:
                    json.loads(target.read_text(encoding="utf-8"))
                    observed["reader_sees_valid_json"] = True
                except json.JSONDecodeError:
                    observed["reader_sees_valid_json"] = False
                return json.dump(payload, handle, **kwargs)

            write = definition(source, name, {"json": SimpleNamespace(dump=dump), "os": os, "tempfile": tempfile})
            write(str(target), {"version": "new"})
            atomic.append({"implementation": label, "reader_sees_valid_json": observed["reader_sees_valid_json"], "final_json_valid": json.loads(target.read_text()) == {"version": "new"}})

    message = "synthetic error https://synthetic-user:synthetic-password@proxy.example/path"
    make_log = definition((REPO / "app.py").read_text(encoding="utf-8"), "_make_log_entry", {"Optional": Optional, "_now_ts": lambda: 1.0, "_isoformat": lambda ts: "synthetic"})
    scrub = definition(upstream_source("app.py"), "_scrub_secrets", {"_CREDENTIAL_URL_RE": re.compile(r"(\w+://)[^:/@\s]+:[^@/\s]+@")})
    logs = {"fork_log_entry_retains_synthetic_password": "synthetic-password" in make_log(message)["message"], "upstream_scrubber_removes_synthetic_password": "synthetic-password" not in scrub(message)}
    result = {
        "upstream_sha": UPSTREAM, "python_version": sys.version.split()[0], "network_requests": 0, "real_secrets_used": False,
        "method": "Reviewed definitions isolated with AST; DNS mocked; own temporary JSON only; neither app nor upstream workers launched.",
        "ip_policy": ip_results, "malformed_dns_error": errors, "metadata_atomicity": atomic, "credential_url_logs": logs,
        "limitations": ["NAT64 internal reachability depends on a translating network and was not tested.", "No new end-to-end remotely exploitable vulnerability or severity assigned by these comparison probes.", "Metadata observation is deterministic interleaving at json.dump, not a stress or performance benchmark."],
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
