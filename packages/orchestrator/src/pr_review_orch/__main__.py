"""CLI entry point: `python -m pr_review_orch` (used by the GitHub Action)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .graph import run_pr_review


def main() -> None:
    parser = argparse.ArgumentParser(prog="pr-review")
    parser.add_argument("--repo-path", default=os.environ.get("REPO_PATH", "."))
    parser.add_argument("--pr-number", type=int,
                        default=int(os.environ.get("PR_NUMBER", "0") or 0))
    parser.add_argument("--dry-run", action="store_true",
                        default=os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes"))
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    if not args.pr_number:
        parser.error("--pr-number (or PR_NUMBER env) is required")

    result = run_pr_review(args.repo_path, args.pr_number, dry_run=args.dry_run)

    verdict = result.get("verdict", {})
    print(json.dumps({
        "approve": verdict.get("approve"),
        "summary": verdict.get("summary"),
        "posted": result.get("posted"),
        "cleanup_report": result.get("cleanup_report"),
        "metrics": result.get("metrics"),
    }, indent=2))

    # Non-zero exit when the PR is rejected lets the Action mark the check red.
    sys.exit(0 if verdict.get("approve") else 1)


if __name__ == "__main__":
    main()
