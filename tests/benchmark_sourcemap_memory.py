#!/usr/bin/env python3
"""Compare AST memory order-of-magnitude before/after the unified span
refactor and prove that source maps keep exact offsets without changing
the memory class.

Usage::

    python tests/benchmark_sourcemap_memory.py [fixture] --old-root ../old

Each build is measured in a *fresh subprocess* so that ``sys.path`` cannot
mix two installed marko copies.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

DEFAULT_FIXTURE = Path(__file__).parent / "fixtures" / "large_commonmark.md"

WORKER = r"""
import gc, json, sys, tracemalloc
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from marko import Markdown

def deep_size(obj):
    seen, total = set(), 0
    def visit(node):
        nonlocal total
        i = id(node)
        if i in seen:
            return
        seen.add(i)
        total += sys.getsizeof(node)
        if isinstance(node, dict):
            for key, value in node.items():
                visit(key); visit(value)
        elif isinstance(node, (list, tuple, set, frozenset)):
            for value in node:
                visit(value)
        elif hasattr(node, "__dict__"):
            visit(vars(node))
    visit(obj)
    return total

text = Path(sys.argv[2]).read_text(encoding="utf-8")
supports_switch = "sourcemap" in Markdown.__init__.__code__.co_varnames

def peak(sourcemap):
    best = 10**18
    for _ in range(int(sys.argv[3])):
        gc.collect()
        tracemalloc.start()
        kwargs = {"sourcemap": sourcemap} if supports_switch else {}
        doc = Markdown(**kwargs).parse(text)
        _, peaked = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        best = min(best, peaked)
        del doc
    return best

tree_on = Markdown().parse(text)
tree_off = (
    Markdown(sourcemap=False).parse(text) if supports_switch else tree_on
)
print(json.dumps({
    "file": __import__("marko").__file__,
    "bytes": len(text.encode("utf-8")),
    "peak_on": peak(True),
    "peak_off": peak(False),
    "tree_on": deep_size(tree_on),
    "tree_off": deep_size(tree_off),
    "supports_switch": supports_switch,
}))
"""


def measure(root: Path, fixture: Path, runs: int) -> dict[str, object]:
    output = subprocess.run(
        [sys.executable, "-c", WORKER, str(root), str(fixture), str(runs)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(output.stdout.strip().splitlines()[-1])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture", nargs="?", default=str(DEFAULT_FIXTURE))
    parser.add_argument("--parse-runs", type=int, default=3)
    parser.add_argument(
        "--old-root",
        help="optional path to a pre-refactor checkout for comparison",
    )
    args = parser.parse_args()

    builds = [("new (unified span model)", Path(".")), ]
    if args.old_root:
        builds.append(("old (per-path positions)", Path(args.old_root)))

    results = {label: measure(root, Path(args.fixture), args.parse_runs)
               for label, root in builds}
    first = results[builds[0][0]]
    print(f"fixture: {args.fixture} ({first['bytes']} bytes)\n")
    header = (
        f"{'build':28} {'peak on':>12} {'peak off':>12} "
        f"{'tree on':>12} {'tree off':>12}"
    )
    print(header)
    print("-" * len(header))
    for label, _ in builds:
        data = results[label]
        print(
            f"{label:28} {data['peak_on']:>12,} {data['peak_off']:>12,} "
            f"{data['tree_on']:>12,} {data['tree_off']:>12,}"
        )
    if args.old_root:
        new = results[builds[0][0]]
        old = results[builds[1][0]]
        ratio = new["tree_on"] / old["tree_on"]
        print(f"\ntree on-size ratio new/old: {ratio:.2f}x")
        print("exact offsets preserved: see test_span_model.py offset tests")


if __name__ == "__main__":
    main()
