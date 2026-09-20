from __future__ import annotations

from collections.abc import Iterable

from .helpers import camel_to_snake_case
from .span import EMPTY_LOCATION, SourceMap, Span, make_location

#: Deprecated alias kept for imports written against earlier marko versions.
_SourceMap = SourceMap


class Element:
    """This class holds attributes common to both the BlockElement and
    InlineElement classes.
    This class should not be subclassed by any other classes beside these.

    Position information is exposed uniformly as a single immutable
    :class:`~marko.span.Location` through :attr:`span_info`.  It is stored as
    compact primitive keys (``source_span``, ``_content_map``, ``_syntax``,
    ``_raw_map``): an element that only carries the overwhelmingly common
    outer span allocates that one key (a plain ``(start, end)`` tuple), and an
    element without any position allocates nothing at all, so third-party
    elements and ``sourcemap=False`` parses keep the plain element footprint.
    """

    override: bool

    @property
    def span_info(self):
        outer = self.__dict__.get("source_span")
        content = self.__dict__.get("_content_map")
        syntax = self.__dict__.get("_syntax")
        raw = self.__dict__.get("_raw_map")
        if outer is None and content is None and syntax is None and raw is None:
            return EMPTY_LOCATION
        return make_location(outer, content, syntax, raw)

    @span_info.setter
    def span_info(self, location) -> None:
        if location is None or location is EMPTY_LOCATION:
            for key in ("source_span", "_content_map", "_syntax", "_raw_map"):
                self.__dict__.pop(key, None)
            return
        mapping = {
            "source_span": location.source_span,
            "_content_map": location.content,
            "_syntax": location.syntax_spans,
            "_raw_map": location.raw,
        }
        for key, value in mapping.items():
            if value is None:
                self.__dict__.pop(key, None)
            else:
                self.__dict__[key] = value

    @property
    def source_span(self):
        """The ``(start, end)`` :class:`~marko.span.Span` of the element in
        the normalized source text, or ``None`` when unknown.  The object
        behaves as a plain tuple; raw coordinates live in the document
        location's newline map."""
        return self.__dict__.get("source_span")

    @source_span.setter
    def source_span(self, value) -> None:
        if value is None:
            self.__dict__.pop("source_span", None)
        else:
            self.__dict__["source_span"] = (
                value if isinstance(value, Span) else Span(value[0], value[1])
            )

    @property
    def syntax_spans(self):
        """Immutable tuple of syntax-only spans, or ``None`` when unknown."""
        return self.__dict__.get("_syntax")

    @syntax_spans.setter
    def syntax_spans(self, value: Iterable | None) -> None:
        if value is None:
            self.__dict__.pop("_syntax", None)
        else:
            self.__dict__["_syntax"] = tuple(
                item if isinstance(item, Span) else Span(item[0], item[1])
                for item in value
            )

    @property
    def content_map(self) -> SourceMap | None:
        """The run-length map from the parsed content body to normalized
        source offsets.

        For a parsed element the map is reconstructed from the spans of its
        leaf children (which exactly tile the content), so the parser does
        not retain a redundant copy.  During inline parsing the block
        temporarily keeps the map as ``_content_map``."""
        content = self.__dict__.get("_content_map")
        if content is not None:
            return content
        return _content_from_children(self)

    @property
    def _inline_positions(self) -> SourceMap | None:
        return self.__dict__.get("_content_map")

    @_inline_positions.setter
    def _inline_positions(self, value: SourceMap | None) -> None:
        if value is None:
            self.__dict__.pop("_content_map", None)
        else:
            self.__dict__["_content_map"] = value

    @property
    def start_pos(self):
        span = self.__dict__.get("source_span")
        return span.start if span is not None else None

    @property
    def end_pos(self):
        span = self.__dict__.get("source_span")
        return span.end if span is not None else None

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

    def __getstate__(self) -> dict:
        return self.__dict__.copy()

    def __setstate__(self, state: dict) -> None:
        # Accept the current form and pickles from earlier versions that
        # stored source_span / syntax_spans / _inline_positions directly.
        legacy_syntax = state.pop("syntax_spans", None)
        legacy_positions = state.pop("_inline_positions", None)
        state.pop("span_info", None)
        if legacy_syntax is not None and "_syntax" not in state:
            state["_syntax"] = tuple(Span(*item) for item in legacy_syntax)
        if legacy_positions is not None and "_content_map" not in state:
            state["_content_map"] = legacy_positions
        self.__dict__.update(state)


def _content_from_children(element: Element) -> SourceMap | None:
    """Reconstruct the content source map of a parsed element by merging the
    spans of its leaf children, which exactly tile its content."""
    runs: list[tuple[int, int]] = []
    _collect_leaf_runs(element, runs)
    if not runs:
        return None
    return SourceMap(runs)


def _collect_leaf_runs(element: Element, runs: list[tuple[int, int]]) -> None:
    children = getattr(element, "children", None)
    if isinstance(children, str) or not children:
        span = element.__dict__.get("source_span")
        if span is not None:
            runs.append((span.start, span.end - span.start))
        return
    for child in children:
        if hasattr(child, "__dict__"):
            _collect_leaf_runs(child, runs)
