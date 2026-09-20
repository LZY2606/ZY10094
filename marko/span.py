"""
Unified, immutable source-position model.

The span model describes where each parsed element came from in the original
document.  It consists of:

* :class:`Span` -- a single half-open, compact ``[start, end)`` range.
* :class:`SourceMap` -- a run-length encoded mapping from the *parsed* text
  (joined lines, stripped markers, unescaped content ...) back to positions in
  the *normalized* source text.  Contiguous ranges are stored as one run, so a
  long continuous body costs constant memory; discontinuous bodies (multiple
  lines, stripped quote/list markers, removed checkbox markers ...) are stored
  as several runs.
* :class:`NewlineMap` -- a compact record of the ``\\r\\n`` line endings that
  lets a normalized offset be translated back to a *raw* offset in the
  unnormalized input.
* :class:`Location` -- the single immutable view exposed on every element
  (as ``element.span_info``), holding the outer :attr:`Location.source_span`,
  the optional :attr:`Location.content` mapping and zero or more
  :attr:`Location.syntax_spans`.

Content spans and syntax spans are always disjoint and together describe the
outer span; the children of an element exactly tile the projection of its
content map onto the source.

Ranges are always half-open ``[start, end)`` with ``start <= end``.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Iterable, Iterator, Sequence
from typing import NamedTuple

__all__ = (
    "Span",
    "SourceMap",
    "NewlineMap",
    "Location",
    "EMPTY_LOCATION",
    "make_location",
)

_CRLF_RE = re.compile(r"\r\n")


class Span(NamedTuple):
    """A half-open ``[start, end)`` range in the normalized source text."""

    start: int
    end: int

    def __bool__(self) -> bool:
        return self.end > self.start


class SourceMap(Sequence):
    """Run-length encoded mapping from parsed-text offsets to normalized
    source offsets.

    A run ``(source_start, run_length)`` states that the next ``run_length``
    indices of the parsed text correspond to the contiguous source range
    ``[source_start, source_start + run_length)``.  Zero-length runs are
    dropped and runs touching in both the parsed text and the source are
    merged, so the representation stays compact: one tuple of pairs per map,
    near-linear slicing/concatenation and a single run for continuous ranges.
    """

    __slots__ = ("_length", "_runs")

    _runs: tuple[tuple[int, int], ...]
    _length: int

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"{type(self).__name__!s} objects are immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"{type(self).__name__!s} objects are immutable")

    def __new__(cls, runs: Iterable[tuple[int, int]] = ()) -> SourceMap:
        merged: list[tuple[int, int]] = []
        length = 0
        for source_start, run_length in runs:
            if run_length <= 0:
                continue
            if merged:
                prev_source, prev_length = merged[-1]
                if source_start == prev_source + prev_length:
                    merged[-1] = (prev_source, prev_length + run_length)
                    length += run_length
                    continue
            merged.append((source_start, run_length))
            length += run_length
        if not merged:
            return _EMPTY_SOURCE_MAP
        self = object.__new__(cls)
        object.__setattr__(self, "_runs", tuple(merged))
        object.__setattr__(self, "_length", length)
        return self

    @classmethod
    def from_positions(cls, positions: Iterable[int]) -> SourceMap:
        """Build a map from per-character source positions, folding runs of
        consecutive ``+1`` positions into compact runs."""
        runs: list[tuple[int, int]] = []
        run_start: int | None = None
        previous: int | None = None
        run_length = 0
        for position in positions:
            if previous is None or position != previous + 1:
                if run_start is not None:
                    runs.append((run_start, run_length))
                run_start = position
                run_length = 1
            else:
                run_length += 1
            previous = position
        if run_start is not None:
            runs.append((run_start, run_length))
        return cls(runs)

    @classmethod
    def from_spans(cls, spans: Iterable[tuple[int, int]]) -> SourceMap:
        """Build a map from (possibly discontinuous) spans in body order."""
        return cls((start, end - start) for start, end in spans)

    @classmethod
    def contiguous(cls, start: int, length: int) -> SourceMap:
        """The single-run map ``[start, start + length)``."""
        return cls(((start, length),))

    def _locate(self, index: int) -> tuple[int, int, int]:
        """Return ``(body_start, body_end, source_start)`` of the run covering
        parsed offset ``index`` (binary search over cumulative ends)."""
        cumulative: list[int] = []
        total = 0
        for _, run_length in self._runs:
            total += run_length
            cumulative.append(total)
        run_index = bisect_right(cumulative, index)
        body_start = cumulative[run_index - 1] if run_index else 0
        source_start, run_length = self._runs[run_index]
        return body_start, body_start + run_length, source_start

    def __len__(self) -> int:
        return self._length

    def __iter__(self) -> Iterator[int]:
        for source_start, run_length in self._runs:
            yield from range(source_start, source_start + run_length)

    def __getitem__(self, index):  # type: ignore[override]
        if isinstance(index, slice):
            start, stop, step = index.indices(self._length)
            if step != 1:
                return type(self).from_positions(
                    self[i] for i in range(start, stop, step)
                )
            if start >= stop:
                return _EMPTY_SOURCE_MAP
            runs: list[tuple[int, int]] = []
            body_cursor = 0
            for source_start, run_length in self._runs:
                body_start = body_cursor
                body_end = body_cursor + run_length
                overlap_start = max(start, body_start)
                overlap_end = min(stop, body_end)
                if overlap_start < overlap_end:
                    runs.append(
                        (
                            source_start + overlap_start - body_start,
                            overlap_end - overlap_start,
                        )
                    )
                body_cursor = body_end
            return type(self)(runs)

        if index < 0:
            index += self._length
        if index < 0 or index >= self._length:
            raise IndexError("source map index out of range")
        body_start, _, source_start = self._locate(index)
        return source_start + index - body_start

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SourceMap):
            return NotImplemented
        return self._runs == other._runs and self._length == other._length

    def __hash__(self) -> int:
        return hash((self._runs, self._length))

    def __bool__(self) -> bool:
        return self._length > 0

    def __reduce__(self) -> tuple[object, ...]:
        return (type(self), (self._runs,))

    def __repr__(self) -> str:
        runs = ", ".join(
            f"({source_start}, {run_length})" for source_start, run_length in self._runs
        )
        return f"SourceMap([{runs}])"

    @property
    def runs(self) -> tuple[tuple[int, int], ...]:
        """The ``(source_start, length)`` runs of the map."""
        return self._runs

    @property
    def source_spans(self) -> tuple[Span, ...]:
        """The discontinuous source ranges covered by the map."""
        return tuple(
            Span(source_start, source_start + run_length)
            for source_start, run_length in self._runs
        )

    def translate(self, start: int, end: int) -> Span | None:
        """Translate a half-open ``[start, end)`` parsed-text span to its
        source :class:`Span`, or ``None`` if out of range.  A single enclosing
        span is returned even across multiple runs."""
        if start < 0 or end <= start or end > self._length:
            return None
        return Span(self[start], self[end - 1] + 1)

    def span(self) -> Span | None:
        """The smallest enclosing :class:`Span`, or ``None`` if empty."""
        if not self._runs:
            return None
        first_source = self._runs[0][0]
        last_source, last_length = self._runs[-1]
        return Span(first_source, last_source + last_length)

    def slice_map(self, start: int, end: int | None = None) -> SourceMap:
        """Sub-map for parsed offsets ``[start, end)``; empty/out-of-range
        slices yield the shared empty map."""
        if end is None:
            end = self._length
        if start < 0:
            start += self._length
        if end < 0:
            end += self._length
        start = max(start, 0)
        end = min(end, self._length)
        if start >= end:
            return _EMPTY_SOURCE_MAP
        return self[start:end]  # type: ignore[return-value]

    def join(self, other: SourceMap) -> SourceMap:
        """Concatenate two maps in body order, merging touching runs."""
        return type(self)(self._runs + other._runs)


_EMPTY_SOURCE_MAP = object.__new__(SourceMap)
object.__setattr__(_EMPTY_SOURCE_MAP, "_runs", ())
object.__setattr__(_EMPTY_SOURCE_MAP, "_length", 0)


class NewlineMap(NamedTuple):
    """Compact record of the ``\\r\\n`` endings of the raw input.

    ``crlf_starts`` holds the normalized offsets of each ``\\n`` that used to
    be part of a ``\\r\\n``.  Bare ``\\r`` and ``\\f`` endings normalize
    one-to-one and never shift offsets.
    """

    crlf_starts: tuple[int, ...] = ()

    @classmethod
    def build(cls, raw: str, normalized: str) -> NewlineMap:
        starts: list[int] = []
        raw_index = 0
        norm_index = 0
        for match in _CRLF_RE.finditer(raw):
            norm_index += match.start() - raw_index
            starts.append(norm_index)
            norm_index += 1
            raw_index = match.end()
        return cls(tuple(starts))

    def raw_offset(self, offset: int) -> int:
        """Translate a normalized offset into the raw input offset.

        The count is the number of ``\\r\\n`` endings strictly before the
        offset, so the normalized ``\\n`` maps onto the raw ``\\r`` and an
        offset just past it maps just past the raw ``\\n``; raw slicing is
        therefore exact for every normalized ``[a, b)`` span."""
        count = bisect_right(self.crlf_starts, offset - 1)
        result = offset + count
        if self.crlf_starts:
            raw_length = self.crlf_starts[-1] + 1 + len(self.crlf_starts)
            if result > raw_length:
                result = raw_length
        return result

    def raw_span(self, span: Span) -> Span:
        """Translate a normalized :class:`Span` to raw endpoints.  A span that
        crosses line endings may be discontinuous in the raw text; consult
        :attr:`crlf_starts` for exact raw ranges."""
        return Span(self.raw_offset(span.start), self.raw_offset(span.end))

    def raw_map(self, source_map: SourceMap) -> SourceMap:
        """Re-base a :class:`SourceMap` onto raw offsets.  Each normalized
        character is translated independently (a ``\\n`` spans two raw bytes),
        and contiguous raw pieces are merged into runs."""
        if not self.crlf_starts:
            return source_map
        runs: list[tuple[int, int]] = []
        for source_start, length in source_map._runs:
            i = 0
            while i < length:
                normalized_pos = source_start + i
                raw_start = self.raw_offset(normalized_pos)
                count = 1
                while normalized_pos + count < source_start + length:
                    if self.raw_offset(normalized_pos + count) != raw_start + count:
                        break
                    count += 1
                raw_end = self.raw_offset(normalized_pos + count)
                runs.append((raw_start, raw_end - raw_start))
                i += count
        return SourceMap(runs)


class Location(NamedTuple):
    """The immutable position record of one element.

    ``element.span_info`` exposes a :class:`Location` view assembled from the
    element's compact primitive storage.

    Attributes:
        source_span: whole normalized range, or ``None`` when unknown;
        content: :class:`SourceMap` of the parsed content tiled by children,
            or ``None``;
        syntax_spans: marker-only spans in source order; ``None`` means
            "unknown", an empty tuple means "known to have none";
        raw: :class:`NewlineMap` for raw input offsets; only on the document.
    """

    source_span: Span | None = None
    content: SourceMap | None = None
    syntax_spans: tuple[Span, ...] | None = None
    raw: NewlineMap | None = None

    def with_changes(
        self,
        *,
        source_span: Span | tuple[int, int] | None = None,
        content: SourceMap | None = None,
        syntax_spans: Iterable[Span | tuple[int, int]] | None = None,
        raw: NewlineMap | None = None,
    ) -> Location:
        """Return a copy with selected fields replaced; unpassed fields keep
        their current value (including ``None``)."""
        new_span = self.source_span
        if source_span is not None:
            new_span = (
                source_span if isinstance(source_span, Span) else Span(*source_span)
            )
        new_syntax = self.syntax_spans
        if syntax_spans is not None:
            new_syntax = tuple(
                item if isinstance(item, Span) else Span(*item) for item in syntax_spans
            )
        return make_location(
            new_span,
            content if content is not None else self.content,
            new_syntax,
            raw if raw is not None else self.raw,
        )

    def translate(self, start: int, end: int) -> Span | None:
        """Translate a content-relative range via the content map."""
        if self.content is None:
            return None
        return self.content.translate(start, end)

    def raw_source_span(self) -> Span | None:
        """The outer span in raw (pre-normalization) coordinates."""
        if self.source_span is None:
            return None
        if self.raw is None:
            return self.source_span
        return self.raw.raw_span(self.source_span)


def make_location(
    source_span: Span | None = None,
    content: SourceMap | None = None,
    syntax_spans: tuple[Span, ...] | None = None,
    raw: NewlineMap | None = None,
) -> Location:
    """Build an immutable :class:`Location` from its (possibly empty)
    fields."""
    return Location(source_span, content, syntax_spans, raw)


EMPTY_LOCATION = Location()
