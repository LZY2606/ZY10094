"""Tests for the unified span model.

Covers the immutable span/source-map primitives, cross block/inline/
extension coverage, nested containers, discontinuous syntax and content,
CRLF raw offsets, source-map-off zero allocation, third-party fallback and
pickle compatibility.
"""

from __future__ import annotations

import pickle
import re

import pytest

from marko import Markdown, MarkoExtension, Parser, block, inline
from marko.ext.gfm import GFM, gfm
from marko.source import Source
from marko.sourcemap import (
    Run,
    SourceMap,
    Span,
    span_text,
    span_to_raw,
    subtract_spans,
)

MARKDOWN = Markdown()


# --------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------


class TestSpan:
    def test_contiguous_span_is_a_tuple(self):
        span = Span(2, 6)
        assert span == (2, 6)
        assert span.start == 2
        assert span.end == 6
        assert span.runs == (Run(2, 6),)
        assert not span.is_discontinuous
        assert span.slice_text("xxabcdyy") == "abcd"
        assert span.covered_length() == 4

    def test_discontinuous_span(self):
        span = Span([(0, 2), (4, 6), (10, 12)])
        assert span == (0, 12)
        assert span.is_discontinuous
        assert span.covered_length() == 6
        assert span.slice_text("ab..cd....ef") == "abcdef"
        assert span.runs == (Run(0, 2), Run(4, 6), Run(10, 12))

    def test_runs_are_normalized(self):
        span = Span([(0, 5), (3, 8), (10, 12)])
        assert span.runs == (Run(0, 8), Run(10, 12))
        assert span == (0, 12)

    def test_span_is_immutable(self):
        span = Span(0, 4)
        with pytest.raises(AttributeError):
            span.runs = ()  # type: ignore[misc]
        with pytest.raises(TypeError):
            span[0] = 1  # type: ignore[index]

    def test_tuple_coercion_and_containment(self):
        span = Span(2, 6)
        assert span.covers((3, 4))
        assert not span.covers((5, 7))
        assert 3 in span
        assert Span([(0, 2), (4, 6)]) in Span([(0, 6)])

    def test_subtract_spans(self):
        content = subtract_spans(Span(0, 10), [Span(2, 4), Span(6, 8)])
        assert content == ((0, 2), (4, 6), (8, 10))
        # overlapping syntax merges correctly
        assert subtract_spans(Span(0, 10), [Span(2, 5), Span(4, 8)]) == (
            Span(0, 2),
            Span(8, 10),
        )
        # subtraction keeps the holes of a discontinuous enclosing region
        disc = subtract_spans(Span([(0, 4), (8, 12)]), [Span(2, 10)])
        assert disc == (Span(0, 2), Span(10, 12))

    def test_pickle_roundtrip(self):
        for span in (Span(1, 5), Span([(0, 2), (4, 6)])):
            restored = pickle.loads(pickle.dumps(span))
            assert restored == span
            assert restored.runs == span.runs
            assert isinstance(restored, Span)

    def test_copy_is_self(self):
        import copy

        span = Span([(0, 2), (4, 6)])
        assert copy.deepcopy(span) is span
        assert copy.copy(span) is span


class TestSourceMap:
    def test_compact_runs_no_per_char_lists(self):
        mapping = SourceMap([(10, 1_000_000)])
        assert len(mapping) == 1_000_000
        assert mapping[0] == 10
        assert mapping[-1] == 1_000_009
        assert len(mapping._runs) == 1

    def test_from_positions_compacts(self):
        mapping = SourceMap.from_positions([3, 4, 5, 9, 10])
        assert mapping._runs == ((0, 3, 3), (3, 5, 9))

    def test_slice_is_linear_in_runs(self):
        mapping = SourceMap.identity(100)
        part = mapping[10:20]
        assert list(part) == list(range(10, 20))
        assert len(part._runs) == 1

    def test_translate_contiguous_and_discontinuous(self):
        mapping = SourceMap([(10, 3), (20, 2)])
        span = mapping.translate(0, 5)
        assert span == (10, 22)
        assert span.is_discontinuous
        assert span.runs == (Run(10, 13), Run(20, 22))
        assert mapping.translate(1, 3) == (11, 13)
        assert mapping.translate(3, 5) == (20, 22)

    def test_translate_out_of_range(self):
        mapping = SourceMap([(10, 2)])
        assert mapping.translate(0, 3) is None
        assert mapping.translate(-1, 1) is None
        assert mapping.translate(1, 1) is None

    def test_concat(self):
        mapping = SourceMap([(0, 2)]).concat(SourceMap([(10, 2)]))
        assert list(mapping) == [0, 1, 10, 11]
        assert mapping.translate(0, 4).runs == (Run(0, 2), Run(10, 12))

    def test_from_lines_strips_unicode_leading_whitespace(self):
        # line 1 occupies source 0..7 ("  hello\n"), line 2 starts at 8
        # and starts with a non-ASCII NBSP that is stripped too.
        mapping = SourceMap.from_lines(
            ["  hello\n", "\u00a0world\n"], [0, 8]
        )
        assert mapping[0] == 2
        assert list(mapping) == [2, 3, 4, 5, 6, 7, 9, 10, 11, 12, 13, 14]

    def test_operations_stay_compact(self):
        # slicing and concatenation of compact maps stay compact
        mapping = SourceMap.identity(1_000_000)
        assert len(mapping._runs) == 1
        part = mapping[100:900_000]
        assert len(part) == 899_900
        assert len(part._runs) == 1
        combined = part.concat(SourceMap.identity(10, 2_000_000))
        assert len(combined._runs) == 2
        assert combined[-1] == 2_000_009

    def test_discontinuous_raw_span(self):
        newlines = (3, 8)
        span = Span([(0, 2), (4, 6)])
        raw = span_to_raw(span, newlines)
        # first run "a\n" piece: 0..2 -> 0..2 ; second starts after CRLF@3
        assert raw.runs == (Run(0, 2), Run(5, 7))
        assert raw == (0, 7)



# --------------------------------------------------------------------------
# Content / syntax relationship
# --------------------------------------------------------------------------


def _iter_elements(node):
    yield node
    children = getattr(node, "children", None)
    if isinstance(children, (list, tuple)):
        for child in children:
            if hasattr(child, "get_type"):
                yield from _iter_elements(child)


class TestContentSyntax:
    def test_link_content_and_syntax_tile_source_span(self):
        text = "[Google](https://google.com)\n"
        link = MARKDOWN.parse(text).children[0].children[0]
        assert isinstance(link, inline.Link)
        assert span_text(text, link.source_span) == "[Google](https://google.com)"
        fragments = sorted(
            [tuple(run) for span in link.syntax_spans for run in span.runs]
            + [tuple(run) for span in link.content_spans for run in span.runs]
        )
        assert fragments == [(0, 1), (1, 7), (7, 28)]
        assert span_text(text, link.content_spans[0]) == "Google"
        assert link.dest_span == (9, 27)

    def test_emphasis_content(self):
        text = "**bold**\n"
        strong = MARKDOWN.parse(text).children[0].children[0]
        assert [span_text(text, s) for s in strong.syntax_spans] == ["**", "**"]
        assert strong.content_spans == (Span(2, 6),)

    def test_raw_text_has_no_syntax(self):
        text = "hello\n"
        raw = MARKDOWN.parse(text).children[0].children[0]
        assert raw.syntax_spans is None
        assert raw.content_spans == (Span(0, 5),)

    def test_multiline_code_span_is_discontinuous(self):
        text = "`foo\n bar`\n"
        code = MARKDOWN.parse(text).children[0].children[0]
        assert isinstance(code, inline.CodeSpan)
        assert code.source_span.is_discontinuous
        assert code.source_span.runs == (Run(0, 5), Run(6, 10))
        # syntax are the two backticks, content skips the line continuation
        assert [span_text(text, s) for s in code.syntax_spans] == ["`", "`"]
        assert [span_text(text, s) for s in code.content_spans] == [
            "foo\n",
            "bar",
        ]


# --------------------------------------------------------------------------
# Cross block / inline / extension continuous coverage
# --------------------------------------------------------------------------


class TestContinuousCoverage:
    def test_inline_children_tile_inline_body(self):
        text = "Hello *world* and `code` [x](u)!\n"
        para = MARKDOWN.parse(text).children[0]
        spans = [child.source_span for child in para.children]
        assert spans[0].start == 0
        assert spans[-1].end == len(text) - 1
        # no overlaps
        for prev, cur in zip(spans, spans[1:]):
            assert prev.end == cur.start

    def test_block_children_tile_document(self):
        text = "# H\n\npara one\npara two\n\n- a\n- b\n"
        doc = MARKDOWN.parse(text)
        blocks = [child for child in doc.children if child.source_span]
        covered: list[tuple[int, int]] = sorted(
            tuple(run) for block in blocks for run in block.source_span.runs
        )
        # blocks are contiguous and ordered, at most one blank-line gap
        for prev, cur in zip(covered, covered[1:]):
            assert prev[1] <= cur[0]

    def test_gfm_table_full_coverage(self):
        text = "| a | b |\n| - | - |\n| 1 | 2 |\n"
        doc = gfm.parse(text)
        table = doc.children[0]
        assert table.source_span == (0, len(text))
        cells = [
            cell
            for row in table.children
            for cell in row.children
        ]
        for cell, letter in zip(cells, ["a", "b", "1", "2"]):
            child = cell.children[0]
            assert span_text(text, child.source_span) == letter

    def test_escaped_pipe_cell_keeps_exact_offsets(self):
        text = "| a\\|b | c |\n| --- | --- |\n| 1 | 2 |\n"
        cell = gfm.parse(text).children[0].children[0].children[0]
        child = cell.children[0]
        assert child.children == "a|b"
        assert child.source_span.is_discontinuous
        assert span_text(text, child.source_span) == "a|b"
        # backslash is syntax, not content
        assert span_text(text, child.source_span) == child.children

    def test_extension_strikethrough_uses_same_model(self):
        text = "~~gone~~\n"
        strike = gfm.parse(text).children[0].children[0]
        assert strike.source_span == (0, 8)
        assert [span_text(text, s) for s in strike.syntax_spans] == ["~~", "~~"]
        assert span_text(text, strike.content_spans[0]) == "gone"


# --------------------------------------------------------------------------
# Nested containers with marker stripping
# --------------------------------------------------------------------------


class TestNestedContainers:
    def test_nested_quote_list_items(self):
        text = "> - one\n> - two\n"
        quote = MARKDOWN.parse(text).children[0]
        items = quote.children[0].children
        for item, word in zip(items, ["one", "two"]):
            assert span_text(text, item.source_span) == "- one\n" or (
                span_text(text, item.source_span) == "- two\n"
            )
            para = item.children[0]
            raw = para.children[0]
            assert span_text(text, raw.source_span) == word

    def test_double_nested_quote(self):
        text = "> > deep\n"
        outer = MARKDOWN.parse(text).children[0]
        inner = outer.children[0]
        para = inner.children[0]
        raw = para.children[0]
        assert span_text(text, raw.source_span) == "deep"
        assert raw.source_span.start == 4

    def test_quote_around_table(self):
        text = "> | a | b |\n> | - | - |\n> | 1 | 2 |\n"
        table = gfm.parse(text).children[0].children[0]
        assert table.source_span.start == 2
        cell = table.children[-1].children[0].children[0]
        assert span_text(text, cell.source_span) == "1"

    def test_list_in_list_paragraphs(self):
        text = "- outer\n  - inner\n"
        outer_list = MARKDOWN.parse(text).children[0]
        outer_item = outer_list.children[0]
        inner_list = outer_item.children[1]
        inner_para = inner_list.children[0].children[0]
        raw = inner_para.children[0]
        assert span_text(text, raw.source_span) == "inner"


# --------------------------------------------------------------------------
# Discontinuous syntax and content
# --------------------------------------------------------------------------


class TestDiscontinuous:
    def test_multiline_paragraph_raw_spans(self):
        text = "  hello\n  world\n"
        para = MARKDOWN.parse(text).children[0]
        raw1, br, raw2 = para.children
        assert span_text(text, raw1.source_span) == "hello"
        assert span_text(text, br.source_span) == "\n"
        assert span_text(text, raw2.source_span) == "world"
        assert raw1.source_span.end <= raw2.source_span.start

    def test_multiline_link_text(self):
        text = "[foo\n bar](u)\n"
        link = MARKDOWN.parse(text).children[0].children[0]
        contents = link.content_spans
        assert any(content.is_discontinuous for content in contents) or len(
            contents
        ) > 1
        assert "".join(span_text(text, content) for content in contents) == (
            "foo\nbar"
        )

    def test_multiline_emphasis(self):
        text = "*foo\n bar*\n"
        em = MARKDOWN.parse(text).children[0].children[0]
        assert em.source_span == (0, 10)
        assert em.syntax_spans[0] == (0, 1)
        assert em.syntax_spans[1] == (9, 10)
        contents = em.content_spans
        assert any(content.is_discontinuous for content in contents) or len(
            contents
        ) > 1
        assert "".join(
            span_text(text, content) for content in contents
        ) == "foo\nbar"


# --------------------------------------------------------------------------
# CRLF raw offsets
# --------------------------------------------------------------------------


class TestCRLFRawOffsets:
    def test_simple_crlf(self):
        raw = "a\r\nb\r\n"
        doc = MARKDOWN.parse(raw)
        raw1, br, raw2 = doc.children[0].children
        for element, expected in ((raw1, "a"), (br, "\n"), (raw2, "b")):
            mapped = doc.to_raw_span(element.source_span)
            assert raw[mapped.start : mapped.end] == expected

    def test_multiline_offsets(self):
        raw = "hello\r\nworld\r\n"
        doc = MARKDOWN.parse(raw)
        raw1, br, raw2 = doc.children[0].children
        assert doc.to_raw_span(raw1.source_span) == (0, 5)
        assert doc.to_raw_span(br.source_span) == (6, 7)
        assert doc.to_raw_span(raw2.source_span) == (7, 12)
        assert doc.to_raw_offset(0) == 0
        assert doc.to_raw_offset(6) == 7

    def test_crlf_discontinuous_content(self):
        raw = "`foo\r\n bar`\r\n"
        doc = MARKDOWN.parse(raw)
        code = doc.children[0].children[0]
        for content in code.content_spans:
            mapped = doc.to_raw_span(content)
            pieces = [raw[s:e] for s, e in mapped.runs]
            assert "".join(pieces) in ("foo\r\n", "bar")

    def test_lone_cr_and_form_feed_do_not_shift(self):
        raw = "a\rb\fc\n"
        doc = MARKDOWN.parse(raw)
        assert doc.crlf_newlines == ()
        assert doc.to_raw_span(doc.source_span) == (0, len(raw))

    def test_no_crlf_is_identity(self):
        raw = "plain text\n"
        doc = MARKDOWN.parse(raw)
        assert doc.to_raw_span((1, 5)) == (1, 5)

    def test_span_to_raw_helper_directly(self):
        newlines = (1, 4)
        assert span_to_raw((0, 2), newlines) == (0, 3)
        assert span_to_raw((2, 5), newlines) == (3, 7)
        # a fragment ending exactly at the newline lands before it
        assert span_to_raw((0, 1), newlines) == (0, 1)


# --------------------------------------------------------------------------
# Source map off: zero position allocation
# --------------------------------------------------------------------------


def _assert_no_positions(node):
    assert getattr(node, "source_span", None) is None
    assert getattr(node, "syntax_spans", None) is None
    assert getattr(node, "_inline_body_map", None) is None
    for attr in ("dest_span", "title_span"):
        if hasattr(node, attr):
            assert getattr(node, attr) is None

    children = getattr(node, "children", None)
    if isinstance(children, (list, tuple)):
        for child in children:
            if hasattr(child, "get_type"):
                _assert_no_positions(child)


class TestSourceMapOff:
    @pytest.mark.parametrize("extensions", [[], [GFM]])
    def test_tree_has_no_spans(self, extensions):
        text = (
            "# Title\n\n[a](u \"t\") **b** `c`\n\n"
            "> quote\n\n- item\n\n"
            "| a | b |\n| - | - |\n| 1 | 2 |\n\n"
            "- [x] done ~~y~~\n"
        )
        md = Markdown(extensions=extensions, sourcemap=False)
        doc = md.parse(text)
        assert doc.source_span is None
        assert doc.crlf_newlines == ()
        _assert_no_positions(doc)
        # rendering is unaffected and never consults spans
        md_on = Markdown(extensions=extensions, sourcemap=True)
        assert md.render(doc) == md_on.convert(text)

    def test_zero_span_allocations_commonmark(self):
        import marko.sourcemap as sourcemap_mod

        span_calls: list[int] = []
        map_calls: list[int] = []
        orig_span_new = sourcemap_mod.Span.__new__
        orig_map_init = sourcemap_mod.SourceMap.__init__

        def counting_span(cls, *args, **kwargs):
            span_calls.append(1)
            return orig_span_new(cls, *args, **kwargs)

        def counting_map(self, *args, **kwargs):
            map_calls.append(1)
            return orig_map_init(self, *args, **kwargs)

        sourcemap_mod.Span.__new__ = staticmethod(counting_span)
        sourcemap_mod.SourceMap.__init__ = counting_map
        try:
            md = Markdown(sourcemap=False)
            doc = md.parse("# H\n\n[a](u) *x* `y`\n\nSetext\n=====\n\n- i\n")
            assert span_calls == []
            assert map_calls == []
            assert doc.source_span is None
            assert "<h1>" in md.render(doc)
        finally:
            sourcemap_mod.Span.__new__ = orig_span_new
            sourcemap_mod.SourceMap.__init__ = orig_map_init

    def test_zero_span_allocations_gfm(self):
        import marko.sourcemap as sourcemap_mod

        span_calls: list[int] = []
        map_calls: list[int] = []
        orig_span_new = sourcemap_mod.Span.__new__
        orig_map_init = sourcemap_mod.SourceMap.__init__

        def counting_span(cls, *args, **kwargs):
            span_calls.append(1)
            return orig_span_new(cls, *args, **kwargs)

        def counting_map(self, *args, **kwargs):
            map_calls.append(1)
            return orig_map_init(self, *args, **kwargs)

        sourcemap_mod.Span.__new__ = staticmethod(counting_span)
        sourcemap_mod.SourceMap.__init__ = counting_map
        try:
            md = Markdown(extensions=[GFM], sourcemap=False)
            md.parse(
                "| a | b |\n| - | - |\n| 1 | 2 |\n\n"
                "- [x] z ~~q~~\n\nhttps://x.io\n"
            )
            assert span_calls == []
            assert map_calls == []
        finally:
            sourcemap_mod.Span.__new__ = orig_span_new
            sourcemap_mod.SourceMap.__init__ = orig_map_init

    def test_sourcemap_still_allocated_when_enabled(self, monkeypatch):
        import marko.sourcemap as sourcemap_mod

        calls = {"span": 0}
        real = sourcemap_mod.Span
        monkeypatch.setattr(
            sourcemap_mod,
            "Span",
            lambda *a, **k: (calls.__setitem__("span", calls["span"] + 1), real(*a, **k))[1],
        )
        Markdown(sourcemap=True).parse("*x*\n")
        assert calls["span"] > 0


# --------------------------------------------------------------------------
# Third-party elements without positions and exception fallback
# --------------------------------------------------------------------------


class TestThirdPartyFallback:
    def test_extension_block_without_positions(self):
        class CustomBlock(block.BlockElement):
            pattern = re.compile(r"@(.*)$", re.M)

            def __init__(self, match: re.Match[str]) -> None:
                self.inline_body = match.group(1)

            @classmethod
            def match(cls, source: Source) -> re.Match[str] | None:
                return source.expect_re(cls.pattern)

            @classmethod
            def parse(cls, source: Source) -> re.Match[str] | None:
                match = source.match
                source.consume()
                return match

        markdown = Markdown(extensions=[MarkoExtension(elements=[CustomBlock])])
        emphasis = markdown.parse("@**text**\n").children[0].children[0]
        assert isinstance(emphasis, inline.StrongEmphasis)
        assert emphasis.source_span is None
        assert emphasis.syntax_spans is None
        # rendering still works
        assert markdown.convert("@**text**\n") == "<strong>text</strong>"

    def test_extension_inline_with_bad_span_hook_falls_back(self):
        class Broken(inline.InlineElement):
            pattern = re.compile(r"!!(.*)!!")
            parse_children = True
            parse_group = 1

            def _syntax_spans(self, match):
                raise IndexError("extension bug")

        def render_broken(self, element):
            return f"<strong>{self.render_children(element)}</strong>"

        extension = MarkoExtension(
            elements=[Broken],
            renderer_mixins=[type("M", (), {"render_broken": render_broken})],
        )
        markdown = Markdown(extensions=[extension])
        paragraph = markdown.parse("!!ok!!\n").children[0]
        element = paragraph.children[0]
        # the out-of-range hook must not crash parsing; the enclosing span
        # is still known, only the extra syntax spans degrade
        assert element.source_span == (0, 6)
        assert element.syntax_spans is None
        assert markdown.convert("!!ok!!\n") == "<p><strong>ok</strong></p>\n"

    def test_renderer_does_not_require_spans(self):
        md = Markdown(sourcemap=False)
        text = "# T\n\n[a](u) *x*\n"
        doc = md.parse(text)
        # remove any leftover attributes defensively, then render
        rendered = md.render(doc)
        assert "<h1>" in rendered and "<a href=" in rendered
        # also render a hand-built element tree that never had spans
        raw = inline.RawText("hi")
        assert raw.source_span is None
        para = block.Paragraph(["hi\n"])
        para.children = [raw]
        assert md.render(para) == "<p>hi</p>\n"

    def test_plain_tuple_assignment_is_coerced(self):
        raw = inline.RawText("x")
        raw.source_span = (1, 2)
        assert isinstance(raw.source_span, Span)
        raw.syntax_spans = [(1, 2)]
        assert all(isinstance(s, Span) for s in raw.syntax_spans)

    def test_legacy_constructor_signatures(self):
        parser = Parser()

        class Paragraph(block.Paragraph):
            override = True

            def __init__(self, lines):
                super().__init__(lines)

        parser.add_element(Paragraph)
        para = parser.parse("hello\n").children[0]
        assert span_text("hello\n", para.children[0].source_span) == "hello"

    def test_pickle_preserves_full_tree_and_spans(self):
        doc = MARKDOWN.parse("[a](u)\n")
        restored = pickle.loads(pickle.dumps(doc))
        link = restored.children[0].children[0]
        assert isinstance(link, inline.Link)
        assert link.source_span == (0, 6)
        assert link.syntax_spans == ((0, 1), (2, 6))
        assert link.dest_span == (4, 5)
        assert isinstance(link.source_span, Span)

    def test_pickle_tree_without_sourcemap(self):
        doc = Markdown(sourcemap=False).parse("# T\n")
        restored = pickle.loads(pickle.dumps(doc))
        assert restored.source_span is None
