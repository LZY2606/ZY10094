"""Generate a large, realistic CommonMark document.

The output combines every block/inline construct in varied proportions so
that span/memory measurements reflect real documents rather than a single
pathological pattern. Re-run to regenerate:

    python tests/fixtures/make_large_commonmark.py > tests/fixtures/large_commonmark.md
"""

from __future__ import annotations

import sys

LINKS = [
    ("home", "https://example.com/"),
    ("docs", "https://example.com/docs/topic#section"),
    ("issue", "../issues/42"),
]


def paragraph(i: int) -> str:
    link, url = LINKS[i % len(LINKS)]
    return (
        f"Paragraph {i} with *emphasis*, **strong text**, `inline code`, "
        f"a [reference link][ref{i % 3}], an [inline {link}]({url} "
        f'"title {i}"), a <https://example.com/auto> autolink, an escaped '
        r"\*asterisk\* and a hard break  "
        f"\ncontinued on the next line of paragraph {i}.\n\n"
    )


def block(i: int) -> str:
    kind = i % 8
    if kind == 0:
        return f"## Heading {i}\n\n" + paragraph(i)
    if kind == 1:
        return (
            "- list item one\n"
            "  - nested item with `code`\n"
            "  - nested item with *em*\n"
            "- list item two\n\n"
        )
    if kind == 2:
        return (
            "> A quoted paragraph that spans\n"
            "> two lines and contains **strong** markup.\n"
            ">\n"
            "> > A nested quote inside.\n\n"
        )
    if kind == 3:
        return "```python\n" f"def function_{i}():\n    return {i}\n" "```\n\n"
    if kind == 4:
        return "    indented code line one\n    indented code line two\n\n"
    if kind == 5:
        return "---\n\n" + paragraph(i)
    if kind == 6:
        return (
            "<div class=\"note\">\n"
            f"  Raw HTML block number {i} with <b>markup</b>.\n"
            "</div>\n\n"
        )
    return paragraph(i)


def main() -> None:
    parts = ["# Large CommonMark fixture\n\n"]
    for i in range(3):
        parts.append(f"[ref{i}]: https://example.com/ref/{i} \"Ref {i}\"\n")
    parts.append("\n")
    i = 0
    target = 1_000_000
    out = "".join(parts)
    while len(out) < target:
        chunk = block(i)
        out += chunk
        i += 1
    sys.stdout.write(out)


if __name__ == "__main__":
    main()
