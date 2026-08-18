"""Shared dataclasses and JSON persistence for the BCFuzzer engine.

The dataclasses mirror the paper's terminology: an item is a (key, value,
type) triple drawn from the declarative per-target catalog; a mutation op is
the replayable edit applied to one controlled node's config; a placement is
one node's assigned config + workload for a round; a bug report is the
oracle's output, deduplicated by signature.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------
# Domain dataclasses
# --------------------------------------------------------------------------

@dataclass
class ItemSpec:
    """A config item the fuzzer knows how to mutate (paper: item=(key,value,type)).

    `path` is the dotted path inside the config file.  `file` names which
    config file of the node dir the item lives in (per-target default when
    empty).  `dangerous_legal` lists legal-but-extreme values that the
    live-admission sanitizers would normally clamp away; the campaign passes
    these through untouched (see `exempt_key_set` in mutator.py).
    """

    path: str
    kind: str                 # int | float | bool | string | enum | list | nested
    default: Any = None
    bounds: tuple[Any, Any] | None = None
    enum: list[Any] | None = None
    dangerous_legal: list[Any] | None = None
    nested_members: list[str] | None = None
    bug_tags: list[str] = field(default_factory=list)
    cross_constraint: str | None = None
    file: str = ""

    def id(self) -> str:
        return f"{self.file or 'default'}:{self.path}"


@dataclass
class MutationOp:
    """One replayable, rollback-able mutation operation (design §3.1)."""

    op_id: str
    item_path: str
    rule: str                # flip | boundary | scale | enum_switch | empty | ...
    old_value: Any = None
    new_value: Any = None
    file: str = ""
    exempt_sanitize: bool = False

    def describe(self) -> str:
        return f"{self.rule}({self.item_path}: {self.old_value!r} -> {self.new_value!r})"


@dataclass
class Seed:
    """A workload seed (transaction corpus T or message corpus M) submitted
    during a round, per design §3.3/§3.4.  `role` names the kind of node the
    seed must be submitted to (normal / controlled / proposer / leader /
    engine); `preconditions` are checked by the scheduler before placement."""

    seed_id: str
    corpus: str              # "T" | "M"
    role: str = "normal"
    target: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    preconditions: dict[str, Any] = field(default_factory=dict)
    bug_tags: list[str] = field(default_factory=list)


@dataclass
class Placement:
    """One controlled node's assigned config + workload for one round."""

    node_id: str
    config_hash: str
    config_file: str | None = None
    ops: list[MutationOp] = field(default_factory=list)
    seed_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BugReport:
    """BCB oracle output: one failure observation, deduplicated by signature."""

    bug_id: str
    target: str
    category: str            # peer | progress | transaction | capacity
    signal: str
    round_id: int
    severity: str = "warning"  # warning | critical
    observed: dict[str, Any] = field(default_factory=dict)
    signature: str = ""
    minimized_ops: list[MutationOp] = field(default_factory=list)
    repro: str | None = None
    verified: bool | None = None


# --------------------------------------------------------------------------
# Persistence (plain JSON under <output>/state/)
# --------------------------------------------------------------------------

def _json_default(value: Any) -> Any:
    if isinstance(value, (ItemSpec, MutationOp, Seed, Placement, BugReport)):
        data = asdict(value)
        data["_kind"] = type(value).__name__
        return data
    if isinstance(value, (Path,)):
        return str(value)
    return str(value)  # last resort: persistence must never crash on odd values


def _json_hook(data: dict[str, Any]) -> Any:
    kind = data.pop("_kind", None)
    if kind == "ItemSpec":
        return ItemSpec(**data)
    if kind == "MutationOp":
        return MutationOp(**data)
    if kind == "Seed":
        return Seed(**data)
    if kind == "Placement":
        return Placement(**data)
    if kind == "BugReport":
        return BugReport(**data)
    return data


def save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=_json_default),
                   encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"), object_hook=_json_hook)


def stable_hash(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
