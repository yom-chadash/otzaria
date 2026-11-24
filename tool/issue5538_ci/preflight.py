"""Record a fork runner without starting application or release workflows."""

import argparse
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys


def git(*arguments):
    return subprocess.check_output(
        ["git", *arguments], text=True, encoding="utf-8"
    ).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    repository = os.environ.get("GITHUB_REPOSITORY")
    expected = os.environ.get("GITHUB_SHA", "")
    if repository != "yom-chadash/otzaria":
        raise SystemExit("This workflow is restricted to the requested fork.")
    if not re.fullmatch(r"[0-9a-f]{40}", expected):
        raise SystemExit("Missing immutable workflow source commit.")
    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain=v1", "--untracked-files=all")
    if commit != expected or status:
        raise SystemExit("Preflight source is changed or dirty.")
    result = {
        "schemaVersion": 1,
        "purpose": "CI environment preflight; no PDF measurements",
        "status": "passed",
        "repository": repository,
        "source": {"commit": commit, "tree": git("rev-parse", "HEAD^{tree}"), "dirty": False},
        "workflow": {
            key: os.environ.get(key)
            for key in ("GITHUB_WORKFLOW_REF", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB")
        },
        "runner": {
            "system": platform.system(),
            "platform": platform.platform(),
            "architecture": platform.machine(),
            "logicalProcessors": os.cpu_count(),
            "imageOS": os.environ.get("ImageOS"),
            "imageVersion": os.environ.get("ImageVersion"),
            "environment": os.environ.get("RUNNER_ENVIRONMENT"),
        },
        "python": sys.version,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Verified {repository}@{commit} on {platform.system()}; no measurement was run.")


if __name__ == "__main__":
    main()
