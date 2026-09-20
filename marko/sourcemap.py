"""
Unified source position ("span") model.

This module is the single place that knows how source positions flow through
the parser. It defines two immutable representations:

* :class:`Span` -- a public, half-open range ``[start, end)`` into the
  normalized source text. When the covered region is discontinuous, the
  exact source fragments are stored in :attr:`Span.runs`; indexing/slicing
  the span (``span[0]``, ``span == (start, end)``) still refers to the
  enclosing range so legacy code keeps working.
* :class:`SourceMap` -- an internal, compact mapping from offsets in a
  derived text (an inline body, a table cell, ...) to offsets in the
  normalized source text. Contiguous offsets are stored as runs, never as a
  per-character integer list. Slicing and concatenating maps are linear in

The relationship between the two is::

    element.source_span          enclosing range of the whole element
    element.syntax_spans         ranges that are pure syntax (``**``, ``[``)
    element.content_span         the remaining ranges (covered by children)

See ``SPAN_MODEL.md`` for the full description.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Iterator, Sequence
from typing import NamedTuple, overload

__all__ = [
    "Span",
    "SourceMap",
    "Run",
    "subtract_spans",
    "span_text",
    "span_to_raw",
]

#: One contiguous source fragment ``[start, end)``.
Run = NamedTuple("Run", [("start", int), ("end", int)])


def _normalize_runs(
    runs: Iterable[tuple[int, int]],
) -> tuple[Run, ...]:
    """Sort, merge and validate raw fragment tuples into compact runs."""
    merged: list[Run] = []
    for run in sorted(runs):
        start, end = int(run[0]), int(run[1])
        if end < start:
            raise ValueError(f"invalid span fragment: {(start, end)!r}")
        if end == start:
            continue
        if merged and start <= merged[-1].end:
            if end > merged[-1].end:
                merged[-1] = Run(merged[-1].start, end)
            continue
        merged.append(Run(start, end))
    return tuple(merged)


class Span(tuple):
    """An immutable half-open source range ``[start, end)``.

    A ``Span`` behaves as the two-tuple of its *enclosing* range, so legacy
    code and unpickled trees that only know ``(start, end)`` keep working.

    A contiguous span is a bare two-tuple (no per-instance dictionary):
    position data for large documents therefore costs the same as storing
    tuples. When the covered region is discontinuous, the factory returns
    the :class:`_DiscontinuousSpan` subclass whose :attr:`runs` holds the
    exact, compact fragments.
    """

    __slots__ = ()

    def __new__(
        cls,
        start: int | Sequence[tuple[int, int]],
        end: int | None = None,
        runs: Iterable[tuple[int, int]] | None = None,
    ) -> Span:
        if end is None and not isinstance(start, int):
            normalized = _normalize_runs(start)
            if not normalized:
                return tuple.__new__(cls, (0, 0))
            if len(normalized) == 1:
                return tuple.__new__(cls, (normalized[0].start, normalized[0].end))
            return _DiscontinuousSpan._create(normalized)
        enclosing_start = int(start)  # type: ignore[arg-type]
        enclosing_end = int(end)  # type: ignore[arg-type]
        if enclosing_end < enclosing_start:
            raise ValueError(
                f"invalid span: ({enclosing_start}, {enclosing_end})"
            )
        if runs is None:
            return tuple.__new__(cls, (enclosing_start, enclosing_end))
        normalized = _normalize_runs(runs)
        if normalized and (
            normalized[0].start < enclosing_start
            or normalized[-1].end > enclosing_end
        ):
            raise ValueError("span runs must be contained in [start, end)")
        if (
            not normalized
            or (
                len(normalized) == 1
                and normalized[0].start == enclosing_start
                and normalized[0].end == enclosing_end
            )
        ):
            return tuple.__new__(cls, (enclosing_start, enclosing_end))
        return _DiscontinuousSpan._create(
            normalized, enclosing_start, enclosing_end
        )

    def __reduce__(
        self,
    ) -> tuple[object, tuple[tuple[tuple[int, int], ...], ...]]:
        # A plain contiguous span reconstructs as a bare span; the
        # discontinuous subclass reconstructs through its runs.
        return (_restore_span, (tuple((r.start, r.end) for r in self.runs),))

    def __copy__(self) -> Span:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> Span:
        # All state is immutable.
        return self

    @property
    def start(self) -> int:
        return self[0]

    @property
    def end(self) -> int:
        return self[1]

    @property
    def runs(self) -> tuple[Run, ...]:
        """Exact covered fragments; one run for a contiguous span."""
        return (Run(self[0], self[1]),) if self[0] != self[1] else ()

    @property
    def is_discontinuous(self) -> bool:
        """True when the covered region has gaps (multiple fragments)."""
        return False

    def covered_length(self) -> int:
        """Number of source positions actually covered (excluding gaps)."""
        return self[1] - self[0]

    def covers(self, other: Span | tuple[int, int]) -> bool:
        """Whether every fragment of ``other`` lies inside this span."""
        other_runs = other.runs if isinstance(other, Span) else (Run(*other),)
        return all(
            any(s <= run.start and run.end <= e for s, e in self.runs)
            for run in other_runs
        )

    def __contains__(self, item: object) -> bool:
        if isinstance(item, Span):
            return self.covers(item)
        if isinstance(item, tuple) and len(item) == 2:
            return any(s <= item[0] and item[1] <= e for s, e in self.runs)
        if isinstance(item, int):
            return any(s <= item < e for s, e in self.runs)
        return False

    def __add__(self, other: object) -> Span:
        if not isinstance(other, Span):
            return NotImplemented
        return Span(self.runs + other.runs)

    __or__ = __add__

    def slice_text(self, text: str) -> str:
        """Concatenate the slices of ``text`` covered by the fragments."""
        return "".join(text[s:e] for s, e in self.runs)


def _restore_span(runs: tuple[tuple[int, int], ...]) -> Span:
    return Span(runs)


class _DiscontinuousSpan(Span):
    """:class:`Span` variant that carries exact fragments for a region
    with gaps. Only this variant needs a per-instance dictionary."""

    _exact_runs: tuple[Run, ...]

    @classmethod
    def _create(
        cls,
        runs: tuple[Run, ...],
        start: int | None = None,
        end: int | None = None,
    ) -> _DiscontinuousSpan:
        obj = tuple.__new__(
            cls,
            (
                runs[0].start if start is None else start,
                runs[-1].end if end is None else end,
            ),
        )
        object.__setattr__(obj, "_exact_runs", runs)
        return obj

    @property
    def runs(self) -> tuple[Run, ...]:
        return self._exact_runs

    @property
    def is_discontinuous(self) -> bool:
        return True

    def covered_length(self) -> int:
        return sum(run.end - run.start for run in self._exact_runs)


class SourceMap(Sequence[int]):
    """A compact, immutable mapping from derived-text offsets to source
    offsets.

    The mapping is stored as runs ``(source_start, length)``: offset ``i``
    of the derived text maps to ``source_start + (i - body_start)`` inside a
    run. Contiguous positions therefore cost constant memory. Mappings may
    be *partial* (shorter than the text they describe) -- indices past the
    end are simply unknown.
    """

    def __init__(self, runs: Iterable[tuple[int, int]]) -> None:
        self._runs: tuple[tuple[int, int, int], ...]
        normalized: list[tuple[int, int, int]] = []
        run_starts: list[int] = []
        length = 0
        for source_start, run_length in runs:
            if run_length <= 0:
                continue
            normalized.append((length, length + run_length, source_start))
            run_starts.append(length)
            length += run_length
        object.__setattr__(self, "_runs", tuple(normalized))
        object.__setattr__(self, "_run_starts", tuple(run_starts))
        object.__setattr__(self, "_length", length)

    _length: int
    _run_starts: tuple[int, ...]

    @classmethod
    def identity(cls, length: int, source_start: int = 0) -> SourceMap:
        """A mapping where ``i`` maps to ``source_start + i``."""
        return cls([(source_start, length)])

    @classmethod
    def from_positions(cls, positions: Iterable[int]) -> SourceMap:
        """Compact an arbitrary iterable of source positions into runs."""
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
    def from_lines(
        cls,
        lines: Sequence[str],
        line_starts: Sequence[int],
        trailing: int = 0,
    ) -> SourceMap:
        """Build the mapping for ``"".join(line.lstrip() ... )`` style bodies.

        Each line contributes one run starting at its first non-whitespace
        character; Unicode whitespace is handled by :py:meth:`str.lstrip`.
        ``trailing`` characters are dropped from the end of the joined body
        (e.g. a final newline of a paragraph).
        """
        runs: list[tuple[int, int]] = []
        for line, base in zip(lines, line_starts):
            stripped = line.lstrip()
            leading = len(line) - len(stripped)
            runs.append((base + leading, len(stripped)))
        mapping = cls(runs)
        if trailing:
            mapping = mapping[: len(mapping) - trailing]
        return mapping

    def __len__(self) -> int:
        return self._length

    def __iter__(self) -> Iterator[int]:
        for body_start, body_end, source_start in self._runs:
            for offset in range(body_end - body_start):
                yield source_start + offset

    @overload
    def __getitem__(self, index: int) -> int: ...

    @overload
    def __getitem__(self, index: slice) -> SourceMap: ...

    def __getitem__(self, index: int | slice) -> int | SourceMap:
        if isinstance(index, slice):
            start, stop, step = index.indices(self._length)
            if step != 1:
                return self.from_positions(self[i] for i in range(start, stop, step))
            runs: list[tuple[int, int]] = []
            for body_start, body_end, source_start in self._runs:
                overlap_start = max(start, body_start)
                overlap_end = min(stop, body_end)
                if overlap_start < overlap_end:
                    runs.append(
                        (
                            source_start + overlap_start - body_start,
                            overlap_end - overlap_start,
                        )
                    )
            return type(self)(runs)

        if index < 0:
            index += self._length
        if index < 0 or index >= self._length:
            raise IndexError("source map index out of range")
        run_index = bisect_right(self._run_starts, index) - 1
        body_start, _, source_start = self._runs[run_index]
        return source_start + index - body_start

    def concat(self, other: SourceMap) -> SourceMap:
        """Concatenate two mappings, renumbering ``other``'s body offsets."""
        runs = [
            (source_start, body_end - body_start)
            for body_start, body_end, source_start in self._runs
        ]
        runs.extend(
            (source_start, body_end - body_start)
            for body_start, body_end, source_start in other._runs
        )
        return type(self)(runs)

    __add__ = concat

    def translate(
        self, start: int, end: int | None = None
    ) -> Span | None:
        """Translate a half-open range ``[start, end)`` of the derived text
        into a :class:`Span` in the source text.

        The result carries one run per intersected mapping run, so it stays
        exact when the body skips source characters (line breaks, nested
        quote/list markers, ...). Returns ``None`` if any covered index has
        no known mapping or the range is empty/invalid.
        """
        if end is None:
            end = start + 1
        if start < 0 or end <= start or end > self._length:
            return None
        runs: list[tuple[int, int]] = []
        for body_start, body_end, source_start in self._runs:
            overlap_start = max(start, body_start)
            overlap_end = min(end, body_end)
            if overlap_start < overlap_end:
                runs.append(
                    (
                        source_start + overlap_start - body_start,
                        source_start + overlap_end - body_start,
                    )
                )
        if sum(e - s for s, e in runs) != end - start:
            # Some derived offsets have no known source position.
            return None
        if len(runs) == 1:
            return Span(runs[0][0], runs[0][1])
        enclosing = Span(runs)
        return enclosing


def subtract_spans(
    enclosing: Span | tuple[int, int],
    syntax: Iterable[Span | tuple[int, int]],
) -> tuple[Span, ...]:
    """Return the fragments of ``enclosing`` not covered by ``syntax``.

    This is the content region of an element: ``content = source - syntax``.
    Runs stay compact (adjacent fragments are merged) and the operation is
    linear in the number of fragments.
    """
    enclosing_runs = (
        list(enclosing.runs)
        if isinstance(enclosing, Span)
        else list(_normalize_runs([enclosing]))
    )
    # Normalize the cut spans into flat fragments contained in the enclosing
    # region, merge them, then subtract them in one sweep per fragment.
    cuts: list[tuple[int, int]] = []
    enclosing_start = enclosing[0]
    enclosing_end = enclosing[1]
    for item in syntax:
        fragments = item.runs if isinstance(item, Span) else (Run(*item),)
        for start, end in fragments or ((item[0], item[1]),):
            start = max(start, enclosing_start)
            end = min(end, enclosing_end)
            if start < end:
                cuts.append((start, end))
    normalized_cuts = _normalize_runs(cuts)
    result = enclosing_runs
    for cut_start, cut_end in normalized_cuts:
        next_runs: list[Run] = []
        for run in result:
            if cut_end <= run.start or cut_start >= run.end:
                next_runs.append(run)
            else:
                if cut_start > run.start:
                    next_runs.append(Run(run.start, cut_start))
                if cut_end < run.end:
                    next_runs.append(Run(cut_end, run.end))
        result = next_runs
    return tuple(Span(run.start, run.end) for run in result)


def span_text(text: str, span: Span | tuple[int, int] | None) -> str | None:
    """Slice ``text`` using a span, honoring discontinuous runs."""
    if span is None:
        return None
    if isinstance(span, Span):
        return span.slice_text(text)
    return text[span[0] : span[1]]


def span_to_raw(
    span: Span | tuple[int, int],
    crlf_newlines: Sequence[int],
) -> Span:
    """Translate a normalized-text span into offsets of the original text.

    ``crlf_newlines`` is a sorted sequence of normalized offsets where a
    ``\n`` was produced by collapsing a ``\r\n``. Only ``\r\n`` changes
    the length, so endpoints shift by the number of such newlines before
    them. A run's (exclusive) end lands right after its last covered
    character, i.e. before the ``\n`` of a terminating ``\r\n``, which
    keeps ``raw_text[raw.start:raw.end]`` exact. Discontinuous runs are
    translated independently.
    """
    def to_run(run: tuple[int, int]) -> Run:
        return Run(
            run[0] + bisect_right(crlf_newlines, run[0]),
            run[1] + bisect_left(crlf_newlines, run[1]),
        )

    raw_start = span[0] + bisect_right(crlf_newlines, span[0])
    raw_end = span[1] + bisect_left(crlf_newlines, span[1])
    if isinstance(span, Span) and span.is_discontinuous:
        return Span(raw_start, raw_end, runs=(to_run(run) for run in span.runs))
    return Span(raw_start, raw_end)
