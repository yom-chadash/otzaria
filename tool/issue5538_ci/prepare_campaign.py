"""Fetch full fork source commits and reject an incomparable campaign upfront."""

import argparse
import os
from pathlib import Path
import subprocess

from run_campaign import ROOT, pair, read_plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("GITHUB_REPOSITORY") != "yom-chadash/otzaria":
        raise ValueError("This campaign belongs only to the explicitly authorized fork.")
    plan = read_plan(args.plan)
    for role in ("baseline", "candidate"):
        subprocess.run(["git", "fetch", "--no-tags", "https://github.com/yom-chadash/otzaria.git", plan[role]],
                       cwd=ROOT, check=True)
    identities = pair.verify_sources(ROOT, {role: plan[role] for role in ("baseline", "candidate")})
    orchestrator_commit = pair.git(ROOT, "rev-parse", "HEAD")
    pair.verify_checkout(ROOT, orchestrator_commit)
    if (pair.source_identity(ROOT, orchestrator_commit)["harnessObjects"] !=
            identities["sources"]["baseline"]["harnessObjects"]):
        raise ValueError("The orchestrator must use the same verified harness as the measured sources.")
    conditions = len(plan["cases"]) * len(plan["cacheStates"]) * len(plan["scenarios"])
    launches = conditions * 2 * 2 * plan["launchesPerBlock"]
    print(f"Verified {conditions} conditions, {launches} measured process launches, "
          f"{plan['repetitions']} measured repetitions per launch. "
          "Two source checkouts are retained for incremental build reuse.")


if __name__ == "__main__":
    main()
