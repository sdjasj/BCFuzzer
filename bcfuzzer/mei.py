"""Mutation-Effective Index (design §3.1).

Tracks, per item, which (rule, value) mutations were explored and whether the
resulting configuration survived admission:

  - valid[item]   — admitted values (>=1 admitted  => item is `inconsistent`)
  - invalid[item] — rejected values with counts  (>=10 rejected, never
                    admitted => item is `consistent`)
  - otherwise the item stays `unexplored`.

`record_admission` is the ONLY write path: the campaign probes the running
network and feeds the verdict back here, so MEI state and observed behavior
cannot drift apart (the upstream prototype's stale-set bug, fixed per the
revised design).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .common import ItemSpec, load_json, save_json

CONSISTENT_THRESHOLD = 10      # rejected times with zero admission
INCONSISTENT_THRESHOLD = 1     # one admission is enough


@dataclass
class MeiState:
    valid: dict[str, list[tuple[str, Any]]] = field(default_factory=dict)
    invalid: dict[str, dict[str, int]] = field(default_factory=dict)
    explored: dict[str, set[Any]] = field(default_factory=dict)

    # -- the only write path ------------------------------------------------

    def record_admission(self, item: ItemSpec, rule: str, value: Any,
                         admitted: bool) -> None:
        item_id = item.path
        key = f"{rule}={self._norm(value)}"
        if admitted:
            # valid keeps the exact (rule, value) pair so the fuzzing phase
            # can REPLAY the admitted mutation — storing the value alone
            # (the old format) lost the rule and made the exploit branch
            # re-wrap every op under rule="dangerous"
            pairs = self.valid.setdefault(item_id, [])
            if not any(r == rule and self._norm(v) == self._norm(value)
                       for r, v in pairs):
                pairs.append((rule, value))
        else:
            counts = self.invalid.setdefault(item_id, {})
            counts[key] = counts.get(key, 0) + 1
        self.explored.setdefault(item_id, set()).add((rule, self._norm(value)))

    # -- classification ------------------------------------------------------

    def status(self, item: ItemSpec) -> str:
        item_id = item.path
        if self.valid.get(item_id):
            return "inconsistent"
        if len(self.invalid.get(item_id, {})) >= CONSISTENT_THRESHOLD:
            return "consistent"
        return "unexplored"

    def status_counts(self, target: str,
                      catalog: list[ItemSpec]) -> dict[str, int]:
        counts = {"consistent": 0, "inconsistent": 0, "unexplored": 0}
        for item in catalog:
            counts[self.status(item)] += 1
        return counts

    def rejected_count(self, item: ItemSpec) -> int:
        return len(self.invalid.get(item.path, {}))

    def valid_pairs(self, item: ItemSpec) -> list[tuple[str, Any]]:
        return list(self.valid.get(item.path, []))

    def is_explored(self, item: ItemSpec, rule: str, value: Any) -> bool:
        return (rule, self._norm(value)) in self.explored.get(item.path, set())

    # -- persistence ----------------------------------------------------------

    def save(self, path: Path) -> None:
        save_json(path, {
            "valid": {k: [[r, v] for r, v in pairs]
                      for k, pairs in self.valid.items()},
            "invalid": self.invalid,
            "explored": {k: sorted(v) for k, v in self.explored.items()},
        })

    @classmethod
    def load(cls, path: Path) -> "MeiState":
        data = load_json(path)
        if not data:
            return cls()
        valid: dict[str, list[tuple[str, Any]]] = {}
        for k, entries in data.get("valid", {}).items():
            pairs = []
            for entry in entries:
                # old format stored bare values — the rule is lost, so the
                # replay guarantee is too; drop rather than fabricate a rule
                if isinstance(entry, list) and len(entry) == 2:
                    pairs.append((entry[0], entry[1]))
            if pairs:
                valid[k] = pairs
        return cls(
            valid=valid,
            invalid=data.get("invalid", {}),
            explored={k: set(map(tuple, v))
                      for k, v in data.get("explored", {}).items()},
        )

    @staticmethod
    def _norm(value: Any) -> Any:
        if isinstance(value, (list, dict)):
            return repr(value)
        return value


def summarize(mei: MeiState, catalog: list[ItemSpec]) -> str:
    counts = mei.status_counts("", catalog)
    return (f"consistent={counts['consistent']} "
            f"inconsistent={counts['inconsistent']} "
            f"unexplored={counts['unexplored']}")
