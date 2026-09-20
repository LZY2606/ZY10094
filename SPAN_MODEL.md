# Span Model

This document describes the internal source-position (source map) model used
by marko.  All positions of every parsed element — block, inline and
extension-provided — are described by the same small set of immutable objects
defined in `marko/span.py`.

## 1. Coordinate spaces

There are three coordinate spaces:

1. **Raw text** — the exact string passed to the parser, including `\r\n`,
   bare `\r` and `\f` line endings and `\x00` characters.
2. **Normalized text** — the text stored on the `Source` object
   (`source._buffer`).  Line terminators are normalized to `\n` and `\x00`
   is replaced by `�`.  All block and inline parsing happens in this space.
3. **Parsed/body text** — the text an element actually parses as inline
   content.  It is derived from the normalized text by stripping leading
   whitespace, joining lines, removing container markers (`>`, list bullets),
   unescaping characters (`\|`), removing checkbox prefixes, etc.

All ranges are **half-open** `[start, end)` with `start <= end`.  Slicing
`text[start:end]` reproduces the covered characters.

## 2. Objects

### `Span(start, end)`

A single compact range.  It is a `NamedTuple`, so it behaves exactly like a
plain `(start, end)` tuple (indexing, unpacking, equality) while being
immutable and self-documenting.

### `SourceMap`

A run-length encoded mapping from **parsed-text offsets** to **normalized
source offsets**.  It is stored as a list of runs `(source_start, length)`:

* A continuous body is one run, no matter how long.  A million-character
  paragraph costs a single 3-tuple, not a million integers.
* A discontinuous body (multiple joined lines, stripped markers, removed
  checkbox/escape characters) is represented by several runs.
* Zero-length runs are dropped and runs that touch in both the parsed text
  and the source are merged automatically, so representations stay compact.

Slicing (`map[a:b]`, `map.slice_map(a, b)`) and concatenation (`map.join(...)`)
run in time proportional to the number of runs (near-linear), not the number
of characters.

Useful operations:

* `map[i]` — source offset of parsed character `i`;
* `map.translate(start, end)` — enclosing `Span` of a parsed region;
* `map.source_spans` — the discontinuous source ranges;
* `map.span()` — smallest single enclosing span;
* `SourceMap.from_positions(iterable)` — compacts an explicit position list;
* `SourceMap.contiguous(start, length)` — single-run constructor.

`SourceMap` is immutable (it uses `__slots__` and rejects attribute
assignment) and hashable.

### `NewlineMap`

A compact record of the `\r\n` endings of the raw input.  It only stores the
normalized offsets of the `\n` characters that used to be part of a `\r\n`,
so a document with no `\r\n` stores an empty tuple.  Bare `\r` and `\f`
endings normalize one-to-one and never shift offsets.

`raw_offset(p)` translates a normalized offset to a raw offset.  The
convention is chosen so that **raw slicing is exact for every normalized
span**:

* the normalized `\n` maps onto the raw `\r`;
* an offset just past the `\n` maps just past the raw `\n`;
* therefore `raw[raw_offset(a):raw_offset(b)]` is the exact raw source of
  normalized `[a, b)` (with `\r\n` restored).

`raw_map(source_map)` re-bases an arbitrary `SourceMap` onto raw offsets,
splitting runs at line endings only where necessary.

### `Location`

The single position record attached to every element, available as
`element.span_info`.  It is an immutable *view* assembled on access from
compact primitive keys stored on the element (`source_span`,
`_content_map`, `_syntax`, `_raw_map`): an element that only carries the
common outer span pays for one small tuple key, and an element without any
positions allocates nothing at all:

| field          | meaning                                                       |
| -------------- | ------------------------------------------------------------- |
| `source_span`  | whole normalized range, or `None` when unknown                |
| `content`      | `SourceMap` of the parsed content (tiled by children), or `None` |
| `syntax_spans` | tuple of marker-only `Span` values; `None` = unknown, `()` = known to have none |
| `raw`          | `NewlineMap`; only set on the document root                  |

`Location` is an immutable `NamedTuple`; use `with_changes(...)` to derive a
modified copy.

For backwards compatibility the element properties `source_span`,
`syntax_spans`, `start_pos`, `end_pos` still exist (and accept tuple
assignment from third-party code), and `dest_span` / `title_span` are exposed
on links/images.

## 3. Content vs. syntax

For an element with an inline body:

* `content` is the region re-parsed into children; child elements exactly
  tile its projection onto the source;
* `syntax_spans` covers characters that are *not* content — emphasis
  delimiters, link brackets/parentheses, quote `>` markers, list bullets;
* content and syntax are **disjoint**.

Examples:

* `**bold**` — syntax `(0,2)` and `(6,8)`, content `[2,6)`;
* `[Google](u)` — syntax `[` and `](u)`, content `Google`, plus the separate
  `dest_span` for the destination;
* a quote over several lines — one `>` per marked line in `syntax_spans`;
  lazy continuation lines (no `>`) simply contribute no marker, so the set is
  naturally discontinuous;
* a list item — the bullet/ordering marker is syntax, the following indented
  content region belongs to the children.

Container outer spans may additionally cover whitespace belonging to ancestor
containers (e.g. a paragraph nested in a quote starts after the `> ` prefix
but the quote's outer span starts at its `>`).  Ancestor markers are never
claimed as syntax by a descendant and never by the descendant's children.

## 4. Newlines and CRLF

* Newlines that join lines of a single paragraph are part of the body map and
  are surfaced as `LineBreak` children; trailing newlines are stripped from
  the inline body.
* After CRLF normalization the same offsets are produced as for the LF-only
  document.  To recover original bytes, translate through the document's
  `NewlineMap` (`document.span_info.raw`).
* Unicode leading whitespace is measured in characters, not bytes, and
  excluded from the content start just like ASCII whitespace; tabs are
  expanded according to the block prefix rules.

## 5. Unknown positions

* Third-party elements created without position information keep the shared
  class-level `EMPTY_LOCATION`; `source_span` / `syntax_spans` read as
  `None` and no position storage is allocated on the instance.
* Custom inline elements may omit `_syntax_spans` (returns `None`) or raise
  from it/from `_set_extra_source_spans`; parsing still succeeds and the
  element keeps whatever positions could be computed.
* Renderers never read span attributes, so removing or ignoring
  `span_info` does not change rendered output.

## 6. Source-map switch

`Markdown(sourcemap=False)` (and `Parser(sourcemap=False)`) disables position
tracking entirely:

* no `Span`, `SourceMap` or `Location` objects are allocated while parsing
  (enforced by a test);
* the raw text and the `NewlineMap` are not retained;
* rendered output is byte-identical to `sourcemap=True`.

Pickled documents remain compatible across both representations: documents
produced before this model (with instance-level `source_span` /
`syntax_spans` / `_inline_positions`) are upgraded on unpickle, and documents
without any location stay free of position storage.


## 7. Memory

* Contiguous ranges are one run each; per-character integer lists are never
  built.  Slicing and joining a `SourceMap` is linear in the number of runs.
* Content maps are needed only while a block's inline body is being parsed;
  afterwards they are discarded, and the map is reconstructed on demand from
  the leaf children, whose spans exactly tile the content.
* `python benchmarks/memory_span_model.py` parses an ~8 MiB CommonMark
  fixture both with the pre-refactor implementation (checked out from git
  `HEAD` into a temporary directory) and the current tree, measuring retained
  AST bytes with `tracemalloc`.  The current implementation stays within
  about 1.3x the baseline retained size while exposing exact raw offsets; the
  benchmark asserts that bound and fails on a regression.
