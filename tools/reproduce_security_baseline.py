"""Run the original 13 security assertions against the audited Git snapshot.

Only tracked Python sources are copied to a newly created temporary directory.
The live .env, cookies, media and working tree are never copied or changed.
Expected exit: 0 here, after pytest reports exactly 13 failures on the old code.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

BASELINE = "98afa9ea5e9a842eb358834fe0cbd251d335573b"
CASES = [
    "test_publish_rejects_thumbnail_path_escape", "test_thumbnail_rejects_traversal_session",
    "test_status_rejects_path_job_id", "test_scraper_rejects_redirect_to_loopback",
    "test_process_rejects_non_youtube_url_before_probe", "test_actor_options_are_bounded",
    "test_video_page_encodes_untrusted_metadata", "test_api_rejects_remote_peer",
    "test_api_rejects_foreign_browser_origin", "test_private_metadata_is_not_served",
    "test_thumbnail_analyze_enforces_upload_limit",
]


def main():
    repo = Path(__file__).resolve().parents[1]
    files = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", BASELINE], cwd=repo, text=True).splitlines()
    with tempfile.TemporaryDirectory(prefix="openshorts-security-baseline-") as directory:
        root = Path(directory)
        for filename in files:
            if filename.endswith(".py") and "/" not in filename:
                content = subprocess.check_output(["git", "show", f"{BASELINE}:{filename}"], cwd=repo)
                (root / filename).write_bytes(content)
        (root / "tests").mkdir()
        (root / "tests" / "test_security.py").write_bytes((repo / "tests" / "test_security.py").read_bytes())
        conftest = subprocess.check_output(["git", "show", f"{BASELINE}:tests/conftest.py"], cwd=repo)
        (root / "tests" / "conftest.py").write_bytes(conftest)
        environment = os.environ.copy()
        # Do not inherit paid-service credentials into this synthetic PoC run.
        for key in list(environment):
            if any(part in key.upper() for part in ("API_KEY", "COOKIES", "AWS_", "OPENSHORTS_")):
                environment.pop(key)
        environment["PYTHON_DOTENV_DISABLED"] = "1"
        command = [sys.executable, "-X", "utf8", "-m", "pytest", "tests/test_security.py", "-q", "--tb=short", "-k", " or ".join(CASES), "--junitxml=baseline.xml"]
        completed = subprocess.run(command, cwd=root, env=environment, timeout=120)
        suites = ET.parse(root / "baseline.xml").getroot()
        suite = suites.find("testsuite")
        actual = {key: int(suite.get(key, "0")) for key in ("tests", "failures", "errors", "skipped")}
        print(f"Audited baseline {BASELINE}: {actual}")
        return 0 if completed.returncode == 1 and actual == {"tests": 13, "failures": 13, "errors": 0, "skipped": 0} else 1


if __name__ == "__main__":
    raise SystemExit(main())
