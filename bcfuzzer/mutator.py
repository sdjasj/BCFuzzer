"""Type-aware, replayable config mutation (paper: type-aware mutation rules).

Three config formats are handled by the same editor interface:
  - TOML (geth.toml) and INI (fisco config.ini / config.genesis) are edited
    line-by-line via config_mutators.parse_lines/rewrite, the same mechanism
    the live-admission sanitizers use — so mutations compose with them.
  - YAML (chainmaker chainmaker.yml, aptos node.yaml) is edited structurally
    with a dotted-path setter that understands lists and nested blocks.

Every edit is recorded as a MutationOp carrying the old value, so a round's
config can be rolled back exactly.  Values drawn from an item's
`dangerous_legal` list are marked exempt_sanitize; the campaign passes that
set to the target's sanitizer so legal-but-extreme values (the paper's
Table 1 triggers) are not clamped away.
"""

from __future__ import annotations

import json
import random
import shutil
from pathlib import Path
from typing import Any

import yaml

from config_mutators import parse_lines, rewrite

from .common import ItemSpec, MutationOp, stable_hash

# --------------------------------------------------------------------------
# Mutation rules per item kind
# --------------------------------------------------------------------------

RULES_FOR_KIND: dict[str, list[str]] = {
    "bool": ["flip", "set_true", "set_false"],
    "int": ["min", "max", "zero", "neg_one", "add_one", "sub_one",
            "scale_10", "scale_10th", "dangerous"],
    "float": ["min", "max", "zero", "neg_one", "scale_10", "scale_10th"],
    "string": ["empty", "max_len", "format_mismatch"],
    "enum": ["switch", "invalid_member", "dangerous"],
    "list": ["append_elem", "remove_elem", "reorder", "duplicate", "empty_list"],
    "nested": ["delete_member", "replace_member", "switch_variant", "dangerous"],
}


def pick_rule(item: ItemSpec, rng: random.Random) -> str:
    return rng.choice(RULES_FOR_KIND[item.kind])


def generate_value(item: ItemSpec, rule: str, current: Any,
                   rng: random.Random) -> Any:
    """Compute the new value for (item, rule) deterministically from the rng."""
    kind = item.kind
    if kind == "bool":
        if rule in ("set_true", "set_false"):
            return rule == "set_true"
        return not bool(current)
    if kind in ("int", "float"):
        low, high = item.bounds or (0, 10**9)
        if rule == "dangerous":
            pool = item.dangerous_legal or []
            return rng.choice(pool) if pool else current
        if rule == "min":
            return low
        if rule == "max":
            return high
        if rule == "zero":
            return 0
        if rule == "neg_one":
            return -1
        n = int(current) if isinstance(current, (int, float)) else int(item.default or 0)
        if rule == "add_one":
            return n + 1
        if rule == "sub_one":
            return n - 1
        if rule == "scale_10":
            return n * 10
        if rule == "scale_10th":
            return max(0, n // 10)
        return current
    if kind == "string":
        if rule == "empty":
            return ""
        if rule == "max_len":
            return "x" * 4096
        if rule == "format_mismatch":
            return "not-a-valid-format!"
        return current
    if kind == "enum":
        members = item.enum or []
        if rule == "dangerous":
            pool = item.dangerous_legal or members
            return rng.choice(pool)
        if rule == "invalid_member":
            return "invalid_member_xyz"
        return rng.choice([m for m in members if m != current] or members)
    if kind == "list":
        return rule  # the list ops are handled by the editor; value is the rule
    if kind == "nested":
        variants = [v for v in (item.enum or []) if isinstance(v, dict)]
        if rule == "dangerous":
            pool = item.dangerous_legal or variants
            return dict(rng.choice(pool)) if pool else current
        if rule == "switch_variant":
            return dict(rng.choice(variants)) if variants else current
        if rule == "replace_member":
            member = rng.choice(item.nested_members or ["type"])
            value: Any = ""
            if member == "type":
                value = "process"
            elif member == "server_address":
                value = "/ip4/127.0.0.1/tcp/1"
            return {"member": member, "value": value}
        return {"member": rng.choice(item.nested_members or ["type"])}
    return current


# --------------------------------------------------------------------------
# Structured YAML editing
# --------------------------------------------------------------------------

def _set_dotted_path(root: Any, dotted: str, value: Any, delete: bool = False) -> Any:
    """Set (or delete) `value` at the dotted path, creating containers on the
    way.  List indexes may appear as path components (e.g. `net.seeds.0`)."""
    parts = dotted.split(".")
    if not parts:
        return root
    current = root
    for index, part in enumerate(parts[:-1]):
        if isinstance(current, list):
            current = current[int(part)]
            continue
        next_part = parts[index + 1]
        try:
            int(next_part)
            next_is_index = True
        except ValueError:
            next_is_index = False
        if not isinstance(current, dict):
            raise TypeError(f"cannot descend into {type(current).__name__} at {'.'.join(parts[:index + 1])}")
        if part not in current or current[part] is None:
            current[part] = [] if next_is_index else {}
        current = current[part]
    leaf = parts[-1]
    if delete:
        if isinstance(current, list):
            current.pop(int(leaf))
        elif isinstance(current, dict):
            current.pop(leaf, None)
    elif isinstance(current, list):
        current[int(leaf)] = value
    else:
        current[leaf] = value
    return root


def _apply_list_op(root: Any, dotted: str, rule: str, rng: random.Random) -> Any:
    parts = dotted.split(".")
    index = int(parts[-1]) if parts[-1].isdigit() else None
    if index is not None:
        parts = parts[:-1]
    parent = root
    for part in parts:
        if isinstance(parent, list):
            parent = parent[int(part)]
        elif isinstance(parent, dict):
            parent = parent.get(part)
        else:
            return root
    if not isinstance(parent, list):
        return root
    if rule == "append_elem":
        elem = parent[-1] if parent else "/ip4/127.0.0.1/tcp/11301/p2p/QmPlaceholderSeed"
        if isinstance(elem, str):
            elem = elem + "x"
        parent.append(elem)
    elif rule == "remove_elem":
        if parent:
            parent.pop(rng.randrange(len(parent)))
    elif rule == "reorder":
        rng.shuffle(parent)
    elif rule == "duplicate":
        if parent:
            parent.append(parent[-1])
    elif rule == "empty_list":
        parent.clear()
    return root


# --------------------------------------------------------------------------
# Config editor (formats + snapshot/rollback)
# --------------------------------------------------------------------------

def _format_leaf(value: Any, fmt: str) -> str:
    if fmt == "toml":
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            return json.dumps(value)
        return str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class ConfigEditor:
    """Per-round editor over one node's config files with rollback."""

    def __init__(self, node_dir: Path) -> None:
        self.node_dir = Path(node_dir)
        self._backups: dict[Path, bytes | None] = {}

    # -- snapshots ---------------------------------------------------------

    def snapshot(self, path: Path) -> None:
        if path not in self._backups:
            self._backups[path] = path.read_bytes() if path.is_file() else None

    def rollback(self) -> None:
        for path, original in self._backups.items():
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)
        self._backups.clear()

    # -- read --------------------------------------------------------------

    def read_value(self, path: Path, item: ItemSpec) -> Any:
        text = path.read_text(encoding="utf-8", errors="replace")
        fmt = self._format_of(path)
        if fmt == "yaml":
            data = yaml.safe_load(text) or {}
            return _get_dotted(data, item.path)
        section, key = self._split_path(item)
        for entry in parse_lines(text):
            if entry["section"] == section and entry["key"] == key:
                return entry["value"]
        return None

    # -- write -------------------------------------------------------------

    def write_value(self, path: Path, item: ItemSpec, value: Any) -> None:
        self.snapshot(path)
        fmt = self._format_of(path)
        if fmt == "yaml":
            self._write_yaml(path, item.path, value)
        else:
            self._write_line(path, item, value, fmt)

    def apply_list_op(self, path: Path, item: ItemSpec, rule: str,
                      rng: random.Random) -> None:
        self.snapshot(path)
        fmt = self._format_of(path)
        if fmt != "yaml":
            raise NotImplementedError(f"list ops on {fmt} not supported")
        data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace")) or {}
        _apply_list_op(data, item.path, rule, rng)
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    def apply_nested_op(self, path: Path, item: ItemSpec, rule: str,
                        value: Any) -> None:
        self.snapshot(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace")) or {}
        if rule == "delete_member":
            _set_dotted_path(data, item.path, None, delete=True)
        elif rule == "replace_member":
            member = value.get("member") if isinstance(value, dict) else None
            if not member:
                return
            _set_dotted_path(data, f"{item.path}.{member}", value.get("value"))
        else:  # switch_variant / dangerous: whole nested object
            _set_dotted_path(data, item.path, value)
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _format_of(path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix in (".yml", ".yaml"):
            return "yaml"
        if suffix == ".toml":
            return "toml"
        return "ini"

    @staticmethod
    def _split_path(item: ItemSpec) -> tuple[str, str]:
        parts = item.path.split(".")
        return ".".join(parts[:-1]), parts[-1]

    def _write_line(self, path: Path, item: ItemSpec, value: Any, fmt: str) -> None:
        section, key = self._split_path(item)
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        entries = parse_lines(text)
        target = None
        for entry in entries:
            if entry["section"] == section and entry["key"] == key:
                target = entry
                break
        new_line = f"{target['indent'] if target else ''}{key} = {_format_leaf(value, fmt)}"
        if target is not None:
            lines[target["line"]] = new_line
        else:
            insert_at = len(lines)
            for i, entry in enumerate(entries):
                if entry["section"] == section:
                    insert_at = entry["line"] + 1
            header = f"[{section}]"
            if not any(l.strip() == header for l in lines):
                lines.append("")
                lines.append(header)
                insert_at = len(lines)
            lines.insert(insert_at, new_line)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_yaml(self, path: Path, dotted: str, value: Any) -> None:
        data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace")) or {}
        _set_dotted_path(data, dotted, value)
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _get_dotted(root: Any, dotted: str) -> Any:
    current = root
    for part in dotted.split("."):
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


# --------------------------------------------------------------------------
# Mutation entry point
# --------------------------------------------------------------------------

def mutate_one(editor: ConfigEditor, config_path: Path, item: ItemSpec,
               rule: str, rng: random.Random, op_index: int,
               force_value: Any = None) -> MutationOp:
    """Apply one mutation op; returns the replayable op record.

    `force_value` lets the scheduler hand over a pre-chosen (rule, value) so
    the on-disk edit matches the MEI accounting exactly."""
    current = editor.read_value(config_path, item)
    if item.kind == "list":
        editor.apply_list_op(config_path, item, rule, rng)
        value: Any = rule
    elif item.kind == "nested":
        value = generate_value(item, rule, current, rng)
        if force_value is not None:
            value = force_value
        editor.apply_nested_op(config_path, item, rule, value)
    else:
        value = generate_value(item, rule, current, rng)
        if force_value is not None:
            value = force_value
        editor.write_value(config_path, item, value)
    exempt = bool(item.dangerous_legal and value in item.dangerous_legal)
    op_id = stable_hash(f"{config_path.name}:{item.path}:{rule}:{value}:{op_index}")[:12]
    return MutationOp(
        op_id=op_id,
        item_path=item.path,
        rule=rule,
        old_value=current,
        new_value=value,
        file=config_path.name,
        exempt_sanitize=exempt,
    )


def exempt_key_set(ops: list[MutationOp]) -> set[tuple[str, str]]:
    """(file, item path) pairs the campaign's sanitizer step must not clamp."""
    return {(op.file, op.item_path) for op in ops if op.exempt_sanitize}


def copy_config_tree(src_dir: Path, dst_dir: Path) -> None:
    """Copy the whole config directory of a node as the mutation base."""
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    shutil.copytree(src_dir, dst_dir)
