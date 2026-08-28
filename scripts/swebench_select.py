#!/usr/bin/env python
"""Pick the cheapest SWE-bench instances to heal (token-frugal slice).

The Corrector's cost is dominated by *prompt* tokens, and the biggest driver is
the size of the file it has to read and rewrite.  So we rank instances by a
cost proxy — golden-patch size + problem-statement length + a penalty for the
heavyweight repos — and keep the smallest, single-file ones.

Output is a JSON the runner consumes::

    python scripts/swebench_select.py --limit 8 --out slice.json
    python scripts/run_swebench.py --instances-file slice.json --sanity

``datasets`` is imported lazily so this module (and its pure helpers) import
without it installed.  Install with ``pip install datasets``.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from typing import Any

# Repos whose files/builds are large or slow — avoid for a cheap slice.
HEAVY_REPOS: frozenset[str] = frozenset({
    "django/django", "sympy/sympy", "matplotlib/matplotlib", "sphinx-doc/sphinx",
    "scikit-learn/scikit-learn", "astropy/astropy", "pydata/xarray",
})

# Repos excluded outright (hard skip, not a cost penalty): their instances are
# not reliably evaluable with this test-entry runner.
#  * pytest-dev/pytest — pytest tests itself; a fresh clone mounted over
#    ``/testbed`` hides the build-generated ``src/_pytest/_version.py`` →
#    ``ModuleNotFoundError`` even on a correct patch.
#  * psf/requests — many FAIL_TO_PASS hit httpbin over the network, so they are
#    flaky / not reproducible offline.
EXCLUDED_REPOS: frozenset[str] = frozenset({
    "pytest-dev/pytest",
    "psf/requests",
})


def parse_fail_to_pass(value: Any) -> list[str]:
    """Normalise a SWE-bench ``FAIL_TO_PASS`` field to a list of node ids.

    The field ships either as a real list or as a stringified one (JSON, or a
    Python repr with single quotes).  Tolerant of all three.
    """
    if isinstance(value, list):
        return [str(v) for v in value]
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        try:
            parsed = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return []
    return [str(v) for v in parsed] if isinstance(parsed, list) else []


def patch_n_files(patch: str) -> int:
    """Count the files a unified diff touches (its ``diff --git`` headers)."""
    return patch.count("diff --git ")


def patch_n_lines(patch: str) -> int:
    """Line count of a unified diff."""
    return len(patch.splitlines())


def cost_proxy(instance: dict[str, Any]) -> float:
    """Lower = cheaper to heal. Patch size + statement length + heavy-repo penalty."""
    patch = instance.get("patch", "")
    statement = instance.get("problem_statement", "")
    penalty = 60.0 if instance.get("repo") in HEAVY_REPOS else 0.0
    return patch_n_lines(patch) + len(statement) / 80.0 + penalty


def select(
    instances: list[dict[str, Any]],
    *,
    limit: int,
    max_files: int = 1,
    max_patch_lines: int = 30,
) -> list[dict[str, Any]]:
    """Filter to small, single-file instances and return the *limit* cheapest."""
    cands = [
        x for x in instances
        if x.get("repo") not in EXCLUDED_REPOS
        and patch_n_files(x.get("patch", "")) <= max_files
        and patch_n_lines(x.get("patch", "")) <= max_patch_lines
        and parse_fail_to_pass(x.get("FAIL_TO_PASS"))
    ]
    cands.sort(key=cost_proxy)
    return cands[:limit]


def _load_dataset(name: str, split: str) -> list[dict[str, Any]]:
    """Load a SWE-bench split into plain dicts (lazy ``datasets`` import)."""
    try:
        from datasets import load_dataset
    except ImportError as exc:  # pragma: no cover - environment guard
        raise SystemExit(
            "The 'datasets' package is required. Install with: pip install datasets"
        ) from exc
    return [dict(row) for row in load_dataset(name, split=split)]


def main() -> int:
    """Select the cheapest instances and print/serialise the slice."""
    ap = argparse.ArgumentParser(description="Select cheap SWE-bench instances to heal.")
    ap.add_argument("--dataset", default="princeton-nlp/SWE-bench_Lite")
    ap.add_argument("--split", default="test")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--max-files", type=int, default=1)
    ap.add_argument("--max-patch-lines", type=int, default=30)
    ap.add_argument("--out", default=None, help="write the slice as JSON for the runner")
    args = ap.parse_args()

    chosen = select(
        _load_dataset(args.dataset, args.split),
        limit=args.limit,
        max_files=args.max_files,
        max_patch_lines=args.max_patch_lines,
    )

    print(f"{'instance_id':<34} {'repo':<22} {'lines':>5}  first FAIL_TO_PASS")
    print("-" * 96)
    for x in chosen:
        ftp = parse_fail_to_pass(x.get("FAIL_TO_PASS"))
        print(f"{x['instance_id']:<34} {x['repo']:<22} {patch_n_lines(x['patch']):>5}  {ftp[0]}")

    if args.out:
        payload = {
            "dataset": args.dataset,
            "split": args.split,
            "instance_ids": [x["instance_id"] for x in chosen],
        }
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        print(f"\nWrote {len(chosen)} instance id(s) to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
