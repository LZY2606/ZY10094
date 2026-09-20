# Span model

This document describes the unified internal model for source positions
("spans") in the parsed tree. Block elements, inline elements and bundled
extensions (GFM tables, task lists, alerts, ...) all expose positions
through the same immutable representation defined in
[`marko/sourcemap.py`](marko/sourcemap.py).

## Endpoints

All positions are **half-open** ranges `[start, end)` of character offsets:

* `start` is the offset of the first covered character;
* `end` is the offset immediately after the last covered character;
* therefore `text[start:end]` reconstructs the covered source text and the
  length is `end - start`;
* empty ranges are allowed (`start == end`) and carry no runs.

Offsets index the **normalized** source text held by the parser. The
normalization (see `marko.source._preprocess_text`) collapses every line
terminator to `\n` (`\r\n`, bare `\r`, `\f`) and replaces `\x00` with `�`.
Normalization never changes text length except for `\r\n` (two characters
become one); see [Raw text and CRLF](#raw-text-and-crlf) below.

### `Span`

`marko.sourcemap.Span` is the single public position type:

* it is a `(start, end)` tuple subclass, so legacy code that compares or
  unpacks `(start, end)` tuples keeps working (`span == (start, end)`);
* it is **immutable** -- spans can be shared, stored on elements and
  pickled;
* a contiguous span is a bare two-tuple (no per-instance dictionary), so a
  large document stores position data as compactly as plain tuples;
* a **discontinuous** span uses an internal subclass and additionally
  exposes `span.runs`, a sorted, merged tuple of `Run(start, end)`
  fragments with the exact covered characters. Its two-tuple value stays
  the *enclosing* range `(first_run.start, last_run.end)`.

Helpers:

| API | Meaning |
| --- | --- |
| `span.start`, `span.end` | enclosing endpoints |
| `span.runs` | exact fragments (one run when contiguous) |
| `span.is_discontinuous` | whether there is a gap |
| `span.covered_length()` | covered characters excluding gaps |
| `span.covers(other)` | containment over runs |
| `span.slice_text(text)` | concatenated slices of all runs |
| `span_text(text, span)` | functional form, accepts plain tuples and `None` |

### Content span vs syntax spans

Every element relates three regions, all in the same coordinate system:

```
element.source_span       enclosing range of the whole element
element.syntax_spans      fragments that are pure markup
element.content_spans     source_span minus syntax_spans
```

* `source_span` covers the complete source region that produced the node.
* `syntax_spans` is an immutable tuple of spans that only contain markup:
  the `**` of emphasis, the `[` / `](...)` of a link, the backticks of a
  code span, the backslash of an escape, the trailing spaces of a hard
  line break, etc. It is `None` for elements that have no syntax
  characters (plain text, bare URLs).
* `content_spans` is computed on demand as
  `subtract_spans(source_span, syntax_spans)`. The content region is what
  the children (or the text content) cover.

For `[Google](https://google.com)`:

```
source_span   [0, 28)  "[Google](https://google.com)"
syntax_spans  [0,1) "["            [7,28) "](https://google.com)"
content_spans [1,7) "Google"
```

For `**bold**`, the syntax spans are the two `**` runs and the content
span is `[2,6)` ("bold"). Children tile the content region exactly.

Element-specific regions are exposed alongside it: links/images carry
`dest_span` and `title_span` (`None` for reference links whose
destination lives elsewhere, e.g. in a link reference definition).

### `SourceMap`

`marko.sourcemap.SourceMap` is the internal, compact mapping from offsets
in a **derived** text (an inline body, a table cell body, ...) back to
normalized-source offsets. It powers every translation but never appears
in the public tree (it is cleared once inline parsing finishes):

* stored as runs `(source_start, length)` -- contiguous offsets never
  become a per-character integer list;
* `map[i]` gives the source offset of derived index `i`, in O(log runs);
* slicing (`map[a:b]`) and concatenation (`map.concat(other)`,
  `map + other`) are linear in the number of runs;
* `map.translate(start, end)` returns a `Span`, with one output run per
  intersected mapping run, so a span that crosses stripped regions stays
  **exact** (discontinuous) instead of a lossy enclosing slice;
* mappings may be partial; `translate` returns `None` when any covered
  derived index has no known source position.

`SourceMap.from_lines(lines, line_starts)` builds the mapping for
paragraph/heading bodies: each line's leading whitespace is skipped using
`str.lstrip`, which also strips Unicode whitespace (e.g. `\u00a0`).
`SourceMap.from_positions(iterable)` compacts arbitrary offset iterables
(the legacy construction form).

## Where ranges become discontinuous

The parser deliberately keeps exact runs instead of inflating a span to
its enclosing range in these cases:

* **multi-line bodies after marker stripping** -- paragraph continuation
  lines, nested quotes/list items: the leading indentation and the quote
  `>` / list bullet markers are not part of the inline body;
* **multi-line inline elements** -- code spans (line endings become
  spaces, up to one space of indentation is removed), links/emphasis that
  span lines;
* **escaped characters** -- GFM table cells unescape `\|` to `|`; the
  body position maps onto the source `|` and the backslash is a gap
  between runs;
* **stripped decorations** -- the GFM task list checkbox
  (`[ ] ` / `[x] `) is sliced off the front of the body mapping.

The raw content that produced a node is always recoverable via
`span.slice_text(normalized_text)`.

## Raw text and CRLF

Normalized offsets intentionally ignore line-ending differences. To map
them back onto the **original** document text, use the document helpers:

```python
doc = markdown.parse(text)
raw_span = doc.to_raw_span(span)        # Span in original-text offsets
offset   = doc.to_raw_offset(normalized_offset)
```

* `doc.crlf_newlines` lists the normalized offsets of newlines produced by
  collapsing `\r\n` (empty for documents without `\r\n`);
* a run's start shifts by the number of collapsed newlines at or before
  it; the exclusive end shifts by those strictly before it, so
  `raw_text[raw_span.start:raw_span.end]` is byte-exact even when the
  fragment ends right before a collapsed newline;
* discontinuous runs are translated independently;
* bare `\r` and `\f` are length-preserving and need no correction.

Example (`a\r\nb\r\n`):

| normalized node | normalized span | raw span | raw slice |
| --- | --- | --- | --- |
| text `a` | `[0,1)` | `[0,1)` | `a` |
| line break | `[1,2)` | `[2,3)` | `\n` |
| text `b` | `[2,3)` | `[3,4)` | `b` |

## Unknown positions

Position information is optional.

* `Markdown(sourcemap=False)` / `Parser(sourcemap=False)` parse without
  allocating any position object: every `source_span`, `syntax_spans`,
  `dest_span`/`title_span` and the internal inline mapping stay `None`
  (documented and asserted by tests). Rendering output is identical.
* Third-party elements built through the legacy constructors
  (match objects, plain line lists, hand-built trees) simply have
  `source_span is None`; their children inherit no fake positions.
* Assigning a plain `(start, end)` tuple to `element.source_span` is
  accepted and coerced to an immutable contiguous `Span`.
* An extension's span hook that raises or returns out-of-range offsets
  only loses the extra spans for that element; the parse continues and
  the enclosing span (when known) is retained.
* Renderers never depend on spans: the bundled HTML/Markdown/AST/GFM
  renderers read no position attributes.

`Span` and whole parsed trees survive `pickle` (discontinuous spans are
reconstructed through their runs), and spans support `copy`/`deepcopy`.

## Memory characteristics

Positions must stay compact on large documents:

* contiguous offsets live in `SourceMap` runs and contiguous `Span`s are
  bare tuples -- characters are never stored as integer lists;
* slicing/concatenation are linear in the number of runs, and a
  discontinuous representation is only allocated when a region actually
  has gaps;
* turning source maps off removes position allocations entirely.

Measured on the ~1 MiB generated fixture
([`tests/fixtures/large_commonmark.md`](tests/fixtures/large_commonmark.md),
reproducible with
[`tests/benchmark_sourcemap_memory.py`](tests/benchmark_sourcemap_memory.py),
`tracmalloc` parse peak / GC-reachable tree size):

| build | peak, maps on | peak, maps off | tree, maps on | tree, maps off |
| --- | ---: | ---: | ---: | ---: |
| old per-path propagation | ~35.8 MB | ~35.8 MB (no switch) | ~39.3 MB | ~39.3 MB |
| unified span model | ~37.4 MB | ~26.4 MB | ~38.4 MB | ~27.1 MB |

The reachable tree with maps on is ~0.98x the old size (same order of
magnitude), while parsing with maps off cuts ~30% off the tree. The
savings/parity must not be obtained by dropping offsets: the exact-offset
tests in
[`tests/test_span_model.py`](tests/test_span_model.py) (discontinuous
code spans/cells, nested containers, CRLF raw slices) pin correctness.
