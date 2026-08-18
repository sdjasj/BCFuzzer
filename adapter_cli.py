#!/usr/bin/env python3
"""BCFuzzer adapter CLI for target configuration and interaction execution."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from targets import ADAPTERS, WORKSPACE_ROOT, apply_case, build_manifest, get_adapter, run_interaction


def parse_target_list(value: str | None) -> list[str]:
    if not value or value == "all":
        return sorted(ADAPTERS)
    targets = [item.strip() for item in value.split(",") if item.strip()]
    unknown = sorted(set(targets) - set(ADAPTERS))
    if unknown:
        raise SystemExit(f"unknown target(s): {', '.join(unknown)}")
    return targets


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)

    subparsers.add_parser("list-targets", help="list supported target adapters")

    manifest_parser = subparsers.add_parser("make-manifest", help="generate a campaign manifest")
    manifest_parser.add_argument("--targets", default="all", help="comma-separated target names or all")
    manifest_parser.add_argument("--workspace-root", type=Path, default=WORKSPACE_ROOT)
    manifest_parser.add_argument("--repeats", type=int, default=1)
    manifest_parser.add_argument("--seed", type=int, default=20270802)
    manifest_parser.add_argument("--timeout-seconds", type=int, default=180)
    manifest_parser.add_argument("--execution-mode", choices=("unit", "live"), default="unit")
    manifest_parser.add_argument("--output", type=Path, required=True)

    check_parser = subparsers.add_parser("check-target", help="validate one target root")
    check_parser.add_argument("--target", required=True, choices=sorted(ADAPTERS))
    check_parser.add_argument("--target-root", type=Path, required=True)

    apply_parser = subparsers.add_parser("apply-config", help="generate a valid config variant")
    apply_parser.add_argument("--target", required=True, choices=sorted(ADAPTERS))
    apply_parser.add_argument("--target-root", type=Path, required=True)
    apply_parser.add_argument("--case", required=True)
    apply_parser.add_argument("--run-dir", type=Path, required=True)
    apply_parser.add_argument("--seed", type=int, required=True)

    run_parser = subparsers.add_parser("run-interaction", help="run a transaction/message interaction")
    run_parser.add_argument("--target", required=True, choices=sorted(ADAPTERS))
    run_parser.add_argument("--target-root", type=Path, required=True)
    run_parser.add_argument("--interaction", required=True)
    run_parser.add_argument("--execution-mode", choices=("unit", "live"), default="unit")
    run_parser.add_argument("--run-dir", type=Path, required=True)
    run_parser.add_argument("--coverage-dir", type=Path, required=True)
    run_parser.add_argument("--seed", type=int, required=True)

    args = parser.parse_args(argv)

    if args.action == "list-targets":
        for target in sorted(ADAPTERS):
            print(target)
        return 0

    if args.action == "make-manifest":
        manifest = build_manifest(
            parse_target_list(args.targets),
            args.workspace_root.resolve(),
            args.repeats,
            args.seed,
            args.timeout_seconds,
            args.execution_mode,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(args.output.resolve())
        return 0

    if args.action == "check-target":
        adapter = get_adapter(args.target, args.target_root)
        if not adapter.target_root.is_dir():
            print(f"target root does not exist: {adapter.target_root}", file=sys.stderr)
            return 1
        print(f"{args.target}: {adapter.target_root}")
        return 0

    if args.action == "apply-config":
        metadata = apply_case(args.target, args.target_root, args.case, args.run_dir, args.seed)
        print(json.dumps(metadata, sort_keys=True))
        return 0

    if args.action == "run-interaction":
        result = run_interaction(args.target, args.target_root, args.interaction,
                                 args.run_dir, args.coverage_dir, args.seed,
                                 args.execution_mode)
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("status") in {"success", "not_executed"} else 1

    raise AssertionError(args.action)


if __name__ == "__main__":
    raise SystemExit(main())
