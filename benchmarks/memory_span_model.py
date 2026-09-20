"""Compare source-map memory cost before and after the unified span refactor.

It parses a large CommonMark-derived fixture repeatedly (to approximate a
long real-world document), measures the retained size of the parsed AST and
counts the position objects, then prints a ratio between the baseline
implementation (git HEAD) and the current working tree.

Run:  python benchmarks/memory_span_model.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.join(REPO_ROOT, "tests", "spec", "commonmark.txt")
REPEAT = 40  # produces a document well over a megabyte

EXAMPLE_RE = re.compile(
    r"^`{32} example\b.*?\n([\s\S]*?)^\.\n([\s\S]*?)^`{32}$",
    flags=re.MULTILINE,
)


def build_fixture() -> str:
    with open(SPEC, encoding="utf8") as f:
        data = f.read()
    blocks = [match.group(1) for match in EXAMPLE_RE.finditer(data)]
    base = "\n".join(blocks)
    return (base * ((REPEAT * 1_000_000) // len(base) + 1))[: 8 * 1024 * 1024]


def measure(code_dir: str, fixture_path: str) -> dict:
    script = f"""
import gc, sys, importlib.util, json, tracemalloc
sys.path.insert(0, {code_dir!r})
import marko
with open({fixture_path!r}, encoding='utf8') as _f:
    fixture = _f.read()
gc.collect()
tracemalloc.start()
docs = []
chunks = [fixture[i:i+262144] for i in range(0, len(fixture), 262144)]
for chunk in chunks:
    docs.append(marko.Markdown().parse(chunk))
gc.collect()
current, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()
try:
    from marko.span import SourceMap, Span, Location  # noqa: F401
    span_names = ('Span', 'Location', 'SourceMap', '_SourceMap')
except ImportError:
    from marko.element import _SourceMap  # old location

    class Span:  # type: ignore[no-redef]
        pass

    class Location:  # type: ignore[no-redef]
        pass

    span_names = ('_SourceMap',)

def count_types(roots):
    seen = set()
    counts = {{}}
    stack = list(roots)
    while stack:
        e = stack.pop()
        if id(e) in seen:
            continue
        seen.add(id(e))
        t = type(e).__name__
        counts[t] = counts.get(t, 0) + 1
        for child in getattr(e, 'children', []) or []:
            if hasattr(child, '__dict__') or hasattr(child, 'children'):
                stack.append(child)
    return counts

counts = count_types(docs)
print(json.dumps({{
    'bytes': current,
    'peak': peak,
    'nchunks': len(chunks),
    'span_objects': sum(
        counts.get(name, 0) for name in ('Span', 'Location', 'SourceMap')
    ),
}}))
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": code_dir},
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)
    import json

    return json.loads(proc.stdout.strip().splitlines()[-1])


def export_baseline(target: str) -> None:
    os.makedirs(os.path.join(target, "marko"), exist_ok=True)
    for name in (
        "__init__.py",
        "ast_renderer.py",
        "block.py",
        "element.py",
        "helpers.py",
        "html_renderer.py",
        "inline.py",
        "inline_parser.py",
        "parser.py",
        "patterns.py",
        "renderer.py",
        "source.py",
        "md_renderer.py",
    ):
        data = subprocess.run(
            ["git", "show", f"HEAD:marko/{name}"],
            capture_output=True,
            check=True,
            cwd=REPO_ROOT,
        ).stdout
        with open(os.path.join(target, "marko", name), "wb") as f:
            f.write(data)


def main() -> int:
    fixture = build_fixture()
    print(f"fixture size: {len(fixture):,} bytes")
    fixture_path = os.path.join(tempfile.gettempdir(), "marko_span_fixture.md")
    with open(fixture_path, "w", encoding="utf8") as f:
        f.write(fixture)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            baseline_dir = os.path.join(tmp, "baseline")
            export_baseline(baseline_dir)
            baseline = measure(baseline_dir, fixture_path)
        current = measure(REPO_ROOT, fixture_path)
    finally:
        os.unlink(fixture_path)

    print("metric                         baseline        current")
    print(
        f"retained AST bytes             {baseline['bytes']:>14,}  {current['bytes']:>14,}"
    )
    print(
        f"peak bytes                     {baseline['peak']:>14,}  {current['peak']:>14,}"
    )
    print(
        f"position objects               {baseline['span_objects']:>14,}  {current['span_objects']:>14,}"
    )
    ratio = current["bytes"] / baseline["bytes"]
    print(f"retained-size ratio current/baseline: {ratio:.3f}")
    # Guard: the refactor must not blow memory up by more than 30%.
    assert ratio <= 1.30, f"memory regression: ratio {ratio:.3f}"
    # And precise offsets still present on the current tree.
    from marko import Markdown

    doc = Markdown().parse("a\nb\n")
    assert doc.children[0].children[0].source_span == (0, 1)
    print("OK: memory within budget and offsets preserved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
