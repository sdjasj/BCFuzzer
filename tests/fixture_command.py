#!/usr/bin/env python3
"""Harmless local fixture used by runner tests and the example manifest."""

import json
import os
import sys
from pathlib import Path


def main() -> int:
    mode = sys.argv[1]
    if mode == "interaction":
        covered = int(sys.argv[2])
        output = Path(os.environ["BCFUZZER_COVERAGE_DIR"])
        output.mkdir(parents=True, exist_ok=True)
        (output / "raw.coverage").write_text("x" * covered, encoding="utf-8")
        (output / "summary.json").write_text(
            json.dumps({"lines_covered": covered, "line_coverage_percent": covered / 20 * 100}),
            encoding="utf-8",
        )
    print(f"fixture:{mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
