"""Configuration mutation strategies of the four comparison baselines.

Each strategy takes the generated config text (TOML / YAML / INI — the
formats produced by the target adapters) and returns a mutated copy, in the
spirit of the tool it models:

- ECFuzz:     type-specific value mutations on many options at once
              (zero, negative, extreme, decrement, right-shift, scaling,
              boolean flip, empty string).
- ConfTest:   syntactic and semantic constraint violations (malformed
              values, zero/negative/extreme for numeric options).
- ConfErr:    human editing errors (key spelling typos, structural damage:
              duplicated lines, dropped quotes, truncated values).
- ConfDiag:   exactly one option changed per mutant: empty value,
              same-type (+1), different-type (number->string), spelling,
              case change.
"""

from __future__ import annotations

import math
import random
import re

KEY_LINE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z_][A-Za-z0-9_.\-]*)[ \t]*(?P<sep>=|:)[ \t]*(?P<value>.*)$"
)


def parse_lines(text: str) -> list[dict]:
    """Return [{line, indent, key, sep, value, section}] for key-value lines."""
    out = []
    section = ""
    for i, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        m = KEY_LINE.match(line)
        if m:
            if m.group("sep") == ":" and not m.group("value"):
                section = m.group("key")
            out.append({
                "line": i, "indent": m.group("indent"), "key": m.group("key"),
                "sep": m.group("sep"), "value": m.group("value").strip(),
                "section": section,
            })
    return out


def rewrite(text: str, items: list[dict]) -> str:
    lines = text.splitlines()
    for it in items:
        if it["line"] < len(lines):
            lines[it["line"]] = (
                f"{it['indent']}{it['key']} {it['sep']} {it['value']}"
            )
    return "\n".join(lines) + "\n"


def value_kind(v: str) -> str:
    v = v.strip()
    if not v:
        return "empty"
    if re.fullmatch(r"-?\d+", v):
        return "int"
    if re.fullmatch(r"-?\d+\.\d+", v):
        return "float"
    if v.lower() in ("true", "false"):
        return "bool"
    return "string"


def pick_options(items: list[dict], rng: random.Random, n: int) -> list[dict]:
    numeric = [it for it in items if value_kind(it["value"]) in ("int", "float")]
    pool = numeric if numeric else items
    return rng.sample(pool, min(n, len(pool))) if pool else []


def ecfuzz(text: str, seed: int) -> str:
    rng = random.Random(seed)
    items = [it for it in parse_lines(text) if it["value"]]
    if not items:
        return text
    out = []
    for it in pick_options(items, rng, max(1, len(items) // 3)):
        kind = value_kind(it["value"])
        v = it["value"]
        if kind == "int":
            n = int(v)
            op = rng.randrange(9)
            vals = [0, -1, 2**31 - 1, n - 1, n >> 1, n * 10, n * 100, n + 1, -n]
            it["value"] = str(vals[op])
        elif kind == "float":
            n = float(v)
            it["value"] = str(rng.choice([0.0, -1.0, n * 10.0, n / 10.0, 1e9]))
        elif kind == "bool":
            it["value"] = "false" if v.lower() == "true" else "true"
        else:
            it["value"] = rng.choice(['""', "''", "0", "true"])
        out.append(it)
    return rewrite(text, out)


def conftest(text: str, seed: int) -> str:
    rng = random.Random(seed)
    items = [it for it in parse_lines(text) if it["value"]]
    if not items:
        return text
    out = []
    for it in pick_options(items, rng, max(1, len(items) // 4)):
        kind = value_kind(it["value"])
        if kind == "int":
            it["value"] = str(rng.choice([0, -1, -(2**31), 2**63 - 1]))
        elif kind == "float":
            it["value"] = str(rng.choice([0.0, -1.0, 1e308]))
        elif kind == "bool":
            it["value"] = "maybe"
        else:
            it["value"] = rng.choice(["", "abc", "1.2.3", '"unterminated'])
        out.append(it)
    return rewrite(text, out)


def conferr(text: str, seed: int) -> str:
    rng = random.Random(seed)
    lines = text.splitlines()
    items = parse_lines(text)
    if not items:
        return text
    mutated = []
    for _ in range(max(1, len(items) // 5)):
        it = rng.choice(items)
        kind = rng.randrange(4)
        if kind == 0:  # key spelling: swap two adjacent chars
            k = it["key"]
            if len(k) >= 2:
                p = rng.randrange(len(k) - 1)
                it["key"] = k[:p] + k[p + 1] + k[p] + k[p + 2:]
        elif kind == 1:  # key spelling: delete one char
            k = it["key"]
            if len(k) >= 2:
                p = rng.randrange(len(k))
                it["key"] = k[:p] + k[p + 1:]
        elif kind == 2:  # structural: duplicate the line
            dup = dict(it)
            dup["line"] = it["line"] + 1
            mutated.append(dup)
        else:  # structural: drop quotes / truncate
            v = it["value"]
            if v.startswith('"') and v.endswith('"'):
                it["value"] = v[1:-1]
            else:
                it["value"] = v[: max(0, len(v) // 2)]
        mutated.append(it)
    for dup in [m for m in mutated if m["line"] != items[0]["line"] or True]:
        pass
    # rebuild with inserted duplicates
    new_lines = []
    for i, line in enumerate(lines):
        new_lines.append(line)
        for dup in mutated:
            if dup["line"] == i and dup.get("is_dup"):
                new_lines.append(f"{dup['indent']}{dup['key']} {dup['sep']} {dup['value']}")
    return rewrite("\n".join(new_lines) + "\n", [m for m in mutated if not m.get("is_dup")])


def confdiag(text: str, seed: int) -> str:
    rng = random.Random(seed)
    items = [it for it in parse_lines(text) if it["value"]]
    if not items:
        return text
    it = rng.choice(items)
    kind = value_kind(it["value"])
    op = rng.randrange(5)
    if op == 0:  # empty value
        it["value"] = ""
    elif op == 1:  # same-type: +1 / toggle
        if kind == "int":
            it["value"] = str(int(it["value"]) + 1)
        elif kind == "bool":
            it["value"] = "false" if it["value"].lower() == "true" else "true"
        elif kind == "string":
            it["value"] = it["value"] + "x"
        else:
            it["value"] = str(float(it["value"]) + 1.0)
    elif op == 2:  # different-type: number -> string
        it["value"] = '"' + it["value"].strip('"') + '"' if kind != "string" else str(rng.randrange(1000))
    elif op == 3:  # spelling: key typo
        k = it["key"]
        if len(k) >= 2:
            p = rng.randrange(len(k))
            c = chr(ord(k[p]) ^ 1)
            it["key"] = k[:p] + c + k[p + 1:]
    else:  # case change
        it["key"] = it["key"].swapcase()
    return rewrite(text, [it])


STRATEGIES = {
    "ecfuzz": ecfuzz,
    "conftest": conftest,
    "conferr": conferr,
    "confdiag": confdiag,
}
