from __future__ import annotations

from collections.abc import Iterable, Sequence

from .helpers import camel_to_snake_case
from .sourcemap import Span, SourceMap, subtract_spans


def _coerce_span(value: Span | tuple[int, int] | None) -> Span | None:
    """Accept plain ``(start, end)`` tuples from third-party code but store
    an immutable :class:`~marko.sourcemap.Span` internally."""
    if value is None or isinstance(value, Span):
        return value
    return Span(value[0], value[1])


def _coerce_spans(
    values: Iterable[Span | tuple[int, int]] | None,
) -> tuple[Span, ...] | None:
    if values is None:
        return None
    return tuple(
        value if isinstance(value, Span) else Span(value[0], value[1])
        for value in values
    )


class _SourceMap(SourceMap):
    """Deprecated alias for :class:`marko.sourcemap.SourceMap`."""


def _translate_span(
    positions: Sequence[int] | None, start: int, end: int
) -> Span | None:
    """Translate a span ``[start, end)`` in a derived text to a source
    :class:`~marko.sourcemap.Span`, or return ``None`` if not possible.
    """
    if positions is None:
        return None
    if isinstance(positions, SourceMap):
        return positions.translate(start, end)
    if start < 0 or end <= start or end > len(positions):
        return None
    return Span(positions[start], positions[end - 1] + 1)


class Element:
    """This class holds attributes common to both the BlockElement and
    InlineElement classes.
    This class should not be subclassed by any other classes beside these.
    """

    override: bool

    #: The span of the source text that this element corresponds to:
    #: an immutable :class:`~marko.sourcemap.Span` (a ``(start, end)``
    #: half-open range, with exact runs for discontinuous regions), or
    #: ``None`` when position information is not available (e.g. elements
    #: built by third-party extensions).
    #:
    #: .. note:: The positions are indices into the source text after
    #:     normalization (line terminators are normalized to ``\n``). Use
    #:     the :class:`~marko.block.Document` helpers to map them to the
    #:     original text offsets when it contains ``\r\n``.
    _source_span: Span | None = None

    #: Immutable tuple of spans that contain only syntax characters
    #: (e.g. ``**`` of emphasis, brackets of a link), or ``None`` if not
    #: available. The rest of :attr:`source_span` is the content region,
    #: covered by children; see :attr:`content_spans`.
    _syntax_span_data: tuple[Span, ...] | None = None

    #: Internal: compact mapping of the indices of :attr:`inline_body` to
    #: the positions in the source text. Only set on elements whose inline
    #: body is parsed, and cleared after the inline parsing is done.
    _inline_body_map: SourceMap | None = None

    @property
    def source_span(self) -> Span | None:
        """The enclosing source range of this element, or ``None``."""
        return self._source_span

    @source_span.setter
    def source_span(self, value: Span | tuple[int, int] | None) -> None:
        # Elements themselves are mutable nodes (they are assembled during
        # parsing), but the span object they hold is immutable. Plain
        # ``(start, end)`` tuples (third-party extensions, unpickled trees)
        # are coerced to the unified representation.
        self._source_span = _coerce_span(value)

    @property
    def syntax_spans(self) -> tuple[Span, ...] | None:
        """The syntax-only fragments of this element, or ``None``."""
        return self._syntax_span_data

    @syntax_spans.setter
    def syntax_spans(
        self, values: Iterable[Span | tuple[int, int]] | None
    ) -> None:
        self._syntax_span_data = _coerce_spans(values)

    @property
    def content_spans(self) -> tuple[Span, ...] | None:
        """Exact content fragments: :attr:`source_span` minus
        :attr:`syntax_spans`. They are the regions covered by children
        (or text content). ``None`` when the source span is unknown."""
        if self._source_span is None:
            return None
        return subtract_spans(self._source_span, self._syntax_span_data or ())

    @property
    def _inline_positions(self) -> Sequence[int] | None:
        """Backward-compatible alias for :attr:`_inline_body_map`."""
        return self._inline_body_map

    @_inline_positions.setter
    def _inline_positions(self, value: Sequence[int] | None) -> None:
        self._inline_body_map = (
            value
            if value is None or isinstance(value, SourceMap)
            else SourceMap.from_positions(value)
        )

    @property
    def start_pos(self) -> int | None:
        """The start index of the element in the source text, or None."""
        return None if self._source_span is None else self._source_span.start

    @property
    def end_pos(self) -> int | None:
        """The end index of the element in the source text, or None."""
        return None if self._source_span is None else self._source_span.end

    @classmethod
    def get_type(cls, snake_case: bool = False) -> str:
        """
        Return the Markdown element type that the object represents.

        :param snake_case: Return the element type name in snake case if True
        """

        # Prevent override of BlockElement and InlineElement
        if (
            cls.override
            and cls.__base__
            and cls.__base__ not in Element.__subclasses__()
        ):
            name = cls.__base__.__name__
        else:
            name = cls.__name__
        return camel_to_snake_case(name) if snake_case else name

    def __repr__(self) -> str:
        try:
            from objprint import objstr
        except ImportError:
            from pprint import pformat

            if hasattr(self, "children"):
                children = f" children={pformat(self.children)}"
            else:
                children = ""

            return f"<{self.__class__.__name__}{children}>"
        else:
            return objstr(self, honor_existing=False, include=["children"])
