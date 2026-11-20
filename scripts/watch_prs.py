"""Record pull requests and changed heads for an active reviewer."""

import argparse
import json
import subprocess
import time
from datetime import UTC, datetime

FIELDS = "number,title,state,isDraft,headRefOid,baseRefName,reviewDecision,mergeStateStatus,url"


def emit(message: str) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    print(f"{now} {message}", flush=True)


def pull_requests(repo: str) -> list[dict]:
    result = subprocess.run(
        ["gh", "pr", "list", "--repo", repo, "--state", "all", "--limit", "100", "--json", FIELDS],
        check=True, capture_output=True, text=True, timeout=45,
    )
    return json.loads(result.stdout)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="s-k-28/rebound")
    parser.add_argument("--interval", type=int, choices=(900, 1800), default=900)
    parser.add_argument("--hours", type=float, default=6)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not 0 < args.hours <= 24:
        parser.error("--hours must be greater than zero and at most 24.")

    deadline = time.monotonic() + args.hours * 3600
    seen: dict[int, tuple] = {}
    while True:
        try:
            rows = pull_requests(args.repo)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            emit(f"PR check failed ({type(exc).__name__}); open reviews are unchanged.")
            if args.once:
                raise SystemExit(1) from exc
        else:
            changed = 0
            for row in sorted(rows, key=lambda candidate: candidate["number"]):
                number = row["number"]
                current = tuple(row[key] for key in (
                    "headRefOid", "state", "isDraft", "baseRefName", "reviewDecision", "mergeStateStatus",
                ))
                if current == seen.get(number):
                    continue
                seen[number] = current
                changed += 1
                status = "draft" if row["isDraft"] else row["state"].lower()
                title = " ".join(row["title"].split())
                emit(f"#{number} {status} {row['headRefOid'][:8]} → {row['baseRefName']} "
                     f"{row['reviewDecision'] or 'UNREVIEWED'} {row['mergeStateStatus']} | {title} | {row['url']}")
            emit(f"Checked {len(rows)} PRs, {changed} new or changed.")

        remaining = deadline - time.monotonic()
        if args.once or remaining <= 0:
            break
        time.sleep(min(args.interval, remaining))
    emit("PR monitoring finished.")


if __name__ == "__main__":
    main()
