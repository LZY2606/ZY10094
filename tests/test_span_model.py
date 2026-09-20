"""Tests for the unified, immutable span model."""

from __future__ import annotations

import gc
import pickle
import re

import pytest

from marko import Markdown, MarkoExtension, block, inline
from marko.element import Element
from marko.ext.gfm import GFM, gfm
from marko.source import Source
from marko.span import (
    EMPTY_LOCATION,
    Location,
    NewlineMap,
    SourceMap,
    Span,
)


def _text(text: str, span) -> str:
    assert span is not None
    return text[span[0] : span[1]]


def _iter_blocks(element):
    yield element
    for child in getattr(element, "children", []) or []:
        if hasattr(child, "source_span"):
            yield from _iter_blocks(child)


class TestSourceMap:
    def test_continuous_range_is_one_run(self):
        sm = SourceMap([(10, 1000)])
        assert len(sm) == 1000
        assert sm[0] == 10
        assert sm[-1] == 1009
        assert len(sm._runs) == 1
        assert sm.runs == ((10, 1000),)

    def test_discontinuous_runs(self):
        sm = SourceMap([(0, 2), (10, 3)])
        assert list(sm) == [0, 1, 10, 11, 12]
        assert sm.translate(0, 5) == Span(0, 13)
        assert sm.span() == Span(0, 13)
        assert sm.source_spans == (Span(0, 2), Span(10, 13))

    def test_adjacent_runs_merge_on_build_and_join(self):
        sm = SourceMap([(0, 3), (3, 2)])
        assert sm.runs == ((0, 5),)
        joined = SourceMap([(0, 2)]).join(SourceMap([(2, 4)]))
        assert joined.runs == ((0, 6),)

    def test_slice_is_compact_and_near_linear(self):
        sm = SourceMap.from_positions(range(100))
        sub = sm[10:80]
        assert isinstance(sub, SourceMap)
        assert len(sub._runs) == 1
        assert sub.runs == ((10, 70),)
        assert sm.slice_map(50).runs == ((50, 50),)
        assert sm.slice_map(10, 10).runs == ()

    def test_slice_across_runs(self):
        sm = SourceMap([(0, 4), (100, 4)])
        sub = sm[2:6]
        assert list(sub) == [2, 3, 100, 101]
        assert sub.runs == ((2, 2), (100, 2))

    def test_from_positions_compacts(self):
        sm = SourceMap.from_positions([5, 6, 7, 20, 21])
        assert sm.runs == ((5, 3), (20, 2))

    def test_translate_out_of_range(self):
        sm = SourceMap([(0, 3)])
        assert sm.translate(-1, 2) is None
        assert sm.translate(2, 2) is None
        assert sm.translate(2, 5) is None

    def test_immutable(self):
        sm = SourceMap([(0, 1)])
        with pytest.raises(AttributeError):
            sm._runs = []  # type: ignore[attr-defined]
        with pytest.raises(AttributeError):
            sm._length = 9  # type: ignore[attr-defined]
        assert hash(sm) == hash(SourceMap([(0, 1)]))
        assert sm == SourceMap([(0, 1)])


class TestNewlineMap:
    def test_crlf_positions(self):
        raw = "a\r\nb\r\n"
        normalized = "a\nb\n"
        nl = NewlineMap.build(raw, normalized)
        assert nl.crlf_starts == (1, 3)
        assert [nl.raw_offset(i) for i in range(5)] == [0, 1, 3, 4, 6]
        assert nl.raw_offset(5) == 6  # end of input is clamped

    def test_no_shift_for_lone_cr_or_ff(self):
        raw = "a\rb\fc"
        nl = NewlineMap.build(raw, "a\nb\nc")
        assert nl.crlf_starts == ()
        assert nl.raw_offset(3) == 3

    def test_raw_span_endpoints(self):
        nl = NewlineMap.build("a\r\nb\r\n", "a\nb\n")
        assert nl.raw_span(Span(0, 4)) == Span(0, 6)
        assert nl.raw_span(Span(2, 4)) == Span(3, 6)

    def test_raw_map_splits_at_crlf(self):
        raw = "a\r\nb\r\n"
        nl = NewlineMap.build(raw, "a\nb\n")
        rm = nl.raw_map(SourceMap.contiguous(0, 4))
        # the whole normalized body happens to be contiguous in raw space
        assert rm.source_spans == (Span(0, 6),)
        rebuilt = "".join(raw[s:e] for s, e in rm.source_spans)
        assert rebuilt == "a\r\nb\r\n"
        # slicing each normalized span reproduces the exact raw text
        for start, end in ((0, 1), (1, 2), (2, 3), (3, 4), (0, 4)):
            assert (
                raw[nl.raw_offset(start) : nl.raw_offset(end)].replace("\r\n", "\n")
                == "a\nb\n"[start:end]
            )
        # a single normalized newline maps onto the two raw CRLF bytes
        newline = nl.raw_map(SourceMap.contiguous(1, 1))
        assert newline.source_spans == (Span(1, 3),)
        assert raw[1:3] == "\r\n"
        # discontinuous normalized content stays discontinuous in raw space
        split = nl.raw_map(SourceMap([(0, 1), (2, 1)]))
        assert split.source_spans == (Span(0, 1), Span(3, 4))

    def test_element_raw_offsets(self):
        raw = "# H\r\n\r\nA *b*\r\n"
        doc = Markdown().parse(raw)

        def _find(element, name):
            if type(element).__name__ == name:
                return element
            for child in getattr(element, "children", []) or []:
                found = _find(child, name)
                if found is not None:
                    return found
            return None

        em = _find(doc, "Emphasis")
        assert em is not None
        nl = doc.span_info.raw
        normalized = raw.replace("\r\n", "\n")
        start, end = em.source_span
        assert normalized[start:end] == "*b*"
        assert raw[nl.raw_offset(start) : nl.raw_offset(end)] == "*b*"


class TestImmutableLocation:
    def test_default_location_is_shared_sentinel(self):
        element = inline.RawText("x")
        assert element.span_info is EMPTY_LOCATION
        assert element.source_span is None
        assert element.syntax_spans is None

    def test_span_is_tuple_like(self):
        doc = Markdown().parse("abc\n")
        span = doc.source_span
        assert isinstance(span, tuple)
        assert span == (0, 4)
        start, end = span
        assert (start, end) == (0, 4)

    def test_location_namedtuple_is_immutable(self):
        location = Location(Span(0, 1))
        with pytest.raises(AttributeError):
            location.source_span = Span(1, 2)  # type: ignore[misc]

    def test_with_changes_preserves_other_fields(self):
        location = Location(Span(0, 1), None, (Span(4, 5),))
        new = location.with_changes(source_span=(2, 3))
        assert new.source_span == Span(2, 3)
        assert tuple(new.syntax_spans) == (Span(4, 5),)
        assert location.source_span == Span(0, 1)

    def test_legacy_setters_keep_other_fields(self):
        element = inline.RawText("x")
        element.source_span = (1, 2)
        element.syntax_spans = [(3, 4)]
        element.source_span = (5, 6)
        assert element.source_span == (5, 6)
        assert tuple(element.syntax_spans) == ((3, 4),)


SAMPLE_DOC = (
    "# Title\n"
    "\n"
    "A [link](https://x.io) and `code` and *em*.\n"
    "\n"
    "- one\n"
    "- two\n"
    "\n"
    "> quoted *inner* text\n"
    "> second line\n"
    "\n"
    "| a | b |\n"
    "| - | - |\n"
    "| 1 | 2 |\n"
)


class TestSourceMapOff:
    def _parse(self, text=SAMPLE_DOC):
        return Markdown(extensions=[GFM], sourcemap=False).parse(text)

    def test_no_span_objects_allocated(self):
        # Warm up import/class-level allocations.
        Markdown(sourcemap=False).parse("x\n")
        gc.collect()
        tracked = (Span, SourceMap, Location)
        before = sum(1 for o in gc.get_objects() if type(o) in tracked)
        self._parse()
        gc.collect()
        after = sum(1 for o in gc.get_objects() if type(o) in tracked)
        assert after - before == 0

    def test_no_instance_position_storage(self):
        doc = self._parse()
        for element in _iter_blocks(doc):
            assert "span_info" not in element.__dict__
            assert "source_span" not in element.__dict__
            assert "_content_map" not in element.__dict__
            assert "_syntax" not in element.__dict__
            assert element.source_span is None
            assert element.syntax_spans is None
        assert doc.span_info is EMPTY_LOCATION

    def test_parser_attribute_flag(self):
        from marko.parser import Parser

        parser = Parser(sourcemap=False)
        assert parser.sourcemap is False
        source = Source("abc\n", sourcemap=False)
        assert source.raw_text is None
        assert source.newline_map.crlf_starts == ()
        source_on = Source("a\r\nb\n", sourcemap=True)
        assert source_on.raw_text == "a\r\nb\n"
        assert source_on.newline_map.crlf_starts == (1,)

    def test_rendered_output_unchanged(self):
        on = Markdown(extensions=[GFM], sourcemap=True).convert(SAMPLE_DOC)
        off = Markdown(extensions=[GFM], sourcemap=False).convert(SAMPLE_DOC)
        assert on == off

    def test_commonmark_parity_when_off(self):
        # The whole spec suite is run separately; here we just sanity-check a
        # handful of constructs.
        for text in ("# h\n", "- a\n\n- b\n", "> q\n", "```\nx\n```\n"):
            assert Markdown(sourcemap=False).convert(text) == Markdown().convert(text)


class TestContinuousCoverage:
    def test_inline_children_tile_content(self):
        text = "Hello *world* and `code` end.\n"
        doc = Markdown().parse(text)
        para = doc.children[0]
        spans = [c.source_span for c in para.children]
        assert all(span is not None for span in spans)
        assert spans[0].start == 0
        assert spans[-1].end == len(text) - 1
        for prev, cur in zip(spans, spans[1:]):
            assert prev.end == cur.start

    def test_syntax_and_content_are_disjoint_for_emphasis(self):
        text = "**bold**\n"
        strong = Markdown().parse(text).children[0].children[0]
        outer = strong.source_span
        syntax = strong.syntax_spans
        assert syntax == ((0, 2), (6, 8))
        child = strong.children[0]
        assert child.source_span == (2, 6)
        # syntax + content exactly partition the outer span
        covered = sorted([tuple(s) for s in syntax] + [tuple(child.source_span)])
        assert covered == [(0, 2), (2, 6), (6, 8)]
        assert outer == (0, 8)

    def test_link_syntax_content_dest_partition(self):
        text = "[Google](https://google.com)\n"
        link = Markdown().parse(text).children[0].children[0]
        assert isinstance(link, inline.Link)
        assert link.source_span == (0, 28)
        assert [_text(text, s) for s in link.syntax_spans] == [
            "[",
            "](https://google.com)",
        ]
        assert _text(text, link.children[0].source_span) == "Google"
        assert _text(text, link.dest_span) == "https://google.com"

    def test_block_inline_extension_continuous(self):
        """A custom block + GFM inline + builtin inline span the full body."""
        text = "@**text** ~~strike~~\n"

        class CustomBlock(block.BlockElement):
            pattern = re.compile(r"@(.*)$", re.M)

            def __init__(self, match):
                self.inline_body = match.group(1)
                self.span_info = Location(
                    Span(0, len(text)),
                    SourceMap.contiguous(1, len(match.group(1))),
                )

            @classmethod
            def match(cls, source):
                return source.expect_re(cls.pattern)

            @classmethod
            def parse(cls, source):
                match = source.match
                source.consume()
                return match

        extension = MarkoExtension(elements=[CustomBlock])
        markdown = Markdown(extensions=[extension, GFM])
        element = markdown.parse(text).children[0]
        strong, space, strike = element.children
        assert isinstance(strong, inline.StrongEmphasis)
        assert strong.source_span == (1, 9)
        assert strike.source_span == (10, 20)
        assert _text(text, strike.source_span) == "~~strike~~"
        assert _text(text, strong.children[0].source_span) == "text"


class TestNestedContainers:
    def test_nested_quote_markers_and_content(self):
        text = "> > deep text\n"
        outer = Markdown().parse(text).children[0]
        inner = outer.children[0]
        para = inner.children[0]
        assert outer.syntax_spans == ((0, 1),)
        assert inner.syntax_spans == ((2, 3),)
        assert _text(text, para.children[0].source_span) == "deep text"
        # markers are disjoint from content
        for markers in (outer.syntax_spans, inner.syntax_spans):
            for span in markers:
                assert (
                    para.source_span.start >= span[1] or para.source_span.end <= span[0]
                )

    def test_quote_in_list_content_excludes_markers(self):
        text = "> - one\n> - two\n"
        quote = Markdown().parse(text).children[0]
        assert quote.syntax_spans == ((0, 1), (8, 9))
        item1, item2 = quote.children[0].children
        assert _text(text, item1.source_span) == "- one\n"
        assert _text(text, item2.source_span) == "- two\n"
        assert item1.syntax_spans == ((2, 3),)
        assert item2.syntax_spans == ((10, 11),)
        assert _text(text, item1.children[0].children[0].source_span) == "one"

    def test_deeply_nested_list(self):
        text = "- a\n  - b\n    - c\n"
        doc = Markdown().parse(text)
        outer_item = doc.children[0].children[0]
        nested = outer_item.children[1].children[0]
        deep = nested.children[1].children[0]
        assert _text(text, outer_item.syntax_spans[0]) == "-"
        assert _text(text, nested.syntax_spans[0]) == "-"
        assert _text(text, deep.syntax_spans[0]) == "-"

    def test_nested_code_block_excludes_quote_marker(self):
        text = ">     code\n"
        code = Markdown().parse(text).children[0].children[0]
        assert isinstance(code, block.CodeBlock)
        assert _text(text, code.source_span) == "    code\n"

    def test_gfm_nested_quote_table(self):
        text = "> | a | b |\n> | - | - |\n> | 1 | 2 |\n"
        table = gfm.parse(text).children[0].children[0]
        assert table.source_span.start == 2
        body = table.children[-1]
        assert _text(text, body.children[0].children[0].source_span) == "1"
        assert _text(text, body.children[1].children[0].source_span) == "2"


class TestDiscontinuousSyntax:
    def test_quote_with_lazy_continuation(self):
        text = "> outer\nlazy line\n> end\n"
        quote = Markdown().parse(text).children[0]
        # only two physical lines carry the quote marker
        assert quote.syntax_spans == ((0, 1), (18, 19))
        para = quote.children[0]
        words = [c for c in para.children if isinstance(c, inline.RawText)]
        assert _text(text, words[0].source_span) == "outer"
        assert _text(text, words[1].source_span) == "lazy line"
        assert _text(text, words[2].source_span) == "end"

    def test_multiline_emphasis_spans_discontinuous_content(self):
        text = "*foo\nbar*\n"
        em = Markdown().parse(text).children[0].children[0]
        assert isinstance(em, inline.Emphasis)
        assert em.syntax_spans == ((0, 1), (8, 9))
        content = em.content_map
        assert content is not None
        # the joined inline body excludes the trailing newline; the content
        # maps the word pieces to their source positions around the break.
        assert content.translate(0, 3) == Span(1, 4)
        assert _text(text, content.translate(4, 7)) == "bar"
        assert _text(text, em.children[-1].source_span) == "bar"
        # a multi-line paragraph with leading whitespace has a genuinely
        # discontinuous body map (one run per stripped line)
        multiline = "  foo\n  bar\n"
        para = Markdown().parse(multiline).children[0]
        body = para.content_map
        assert body is not None
        assert body.source_spans == (Span(2, 6), Span(8, 11))
        assert _text(multiline, body.translate(0, 3)) == "foo"
        assert _text(multiline, body.translate(3, 4)) == "\n"
        assert _text(multiline, body.translate(4, 7)) == "bar"

    def test_emphasis_content_and_syntax_disjoint(self):
        text = "*foo\nbar*\n"
        em = Markdown().parse(text).children[0].children[0]
        outer = em.source_span
        syntax = em.syntax_spans
        content = em.content_map
        assert outer is not None and content is not None
        for span in syntax:
            for run_span in content.source_spans:
                assert span.end <= run_span.start or span.start >= run_span.end

    def test_custom_inline_with_three_syntax_fragments(self):
        class Tag(inline.InlineElement):
            pattern = re.compile(r"\[(@[a-z]+)\]\{([^}]+)\}")
            parse_children = True
            parse_group = 1
            priority = 8

            def _syntax_spans(self, match):
                return [
                    (match.start(), match.start(1)),
                    (match.end(1), match.start(2)),
                    (match.end(2), match.end()),
                ]

        text = "[@name]{target}\n"
        md = Markdown(extensions=[MarkoExtension(elements=[Tag])])
        tag = md.parse(text).children[0].children[0]
        assert _text(text, tag.children[0].source_span) == "@name"
        assert [_text(text, s) for s in tag.syntax_spans] == ["[", "]{", "}"]


class TestExceptionFallback:
    def test_extension_without_position_constructor(self):
        """Third-party elements built without positions keep working."""

        class CustomBlock(block.BlockElement):
            pattern = re.compile(r"@(.*)$", re.M)

            def __init__(self, match):
                self.inline_body = match.group(1)

            @classmethod
            def match(cls, source):
                return source.expect_re(cls.pattern)

            @classmethod
            def parse(cls, source):
                match = source.match
                source.consume()
                return match

        md = Markdown(extensions=[MarkoExtension(elements=[CustomBlock])])
        emphasis = md.parse("@**text**\n").children[0].children[0]
        assert emphasis.source_span is None
        assert emphasis.syntax_spans is None
        # renderer must not need spans; the custom block falls back to
        # rendering its children verbatim.
        assert "text" in md.convert("@**text**\n")
        # the standard pipeline is unaffected and stays a paragraph
        assert Markdown().convert("**text**\n") == ("<p><strong>text</strong></p>\n")

    def test_broken_syntax_hook_keeps_outer_span(self):
        class Broken(inline.InlineElement):
            pattern = re.compile(r"~(x)~")
            parse_group = 1
            priority = 8

            def __init__(self, match):
                self.children = match.group(1)

            def _syntax_spans(self, match):
                raise RuntimeError("boom")

        md = Markdown(extensions=[MarkoExtension(elements=[Broken])])
        element = md.parse("~x~\n").children[0].children[0]
        # the outer span survives even though the syntax hook failed
        assert element.source_span == (0, 3)
        assert element.children == "x"

    def test_broken_extra_hook_keeps_basic_spans(self):
        class BrokenMention(inline.InlineElement):
            pattern = re.compile(r"(@[a-z]+)")
            parse_group = 1
            priority = 8

            def __init__(self, match):
                self.children = match.group(1)

            def _set_extra_source_spans(self, match, positions):
                raise RuntimeError("boom")

        md = Markdown(extensions=[MarkoExtension(elements=[BrokenMention])])
        element = md.parse("@bob\n").children[0].children[0]
        assert element.source_span == (0, 4)
        # the failure did not corrupt the rest of the paragraph
        assert element.children == "@bob"


class TestUnicodeWhitespace:
    def test_unicode_leading_whitespace_content_start(self):
        text = "\u00a0hello\n"
        child = Markdown().parse(text).children[0].children[0]
        assert child.source_span == (1, 6)
        assert _text(text, child.source_span) == "hello"

    def test_space_indentation_in_multiline_paragraph(self):
        text = "  hello\n  world\n"
        para = Markdown().parse(text).children[0]
        first, _br, second = para.children
        assert _text(text, first.source_span) == "hello"
        assert _text(text, second.source_span) == "world"

    def test_tab_indented_content_is_code_block(self):
        text = "\thello\n"
        element = Markdown().parse(text).children[0]
        assert isinstance(element, block.CodeBlock)
        assert element.source_span == (0, len(text))


class TestPickleCompatibility:
    def test_roundtrip_preserves_spans(self):
        doc = Markdown().parse("# H *i*\n\n[l](u)\n")
        restored = pickle.loads(pickle.dumps(doc))
        assert restored.source_span == doc.source_span
        heading = restored.children[0]
        assert heading.source_span == doc.children[0].source_span
        assert heading.children[1].syntax_spans == (
            doc.children[0].children[1].syntax_spans
        )

    def test_legacy_pickle_format_loads(self):
        # Simulate pickles produced before the span-model refactor, which
        # stored source_span / syntax_spans directly on the instance dict.
        from marko.span import Span

        legacy = block.Paragraph.__new__(block.Paragraph)
        Element.__setstate__(
            legacy,
            {
                "inline_body": "",
                "source_span": (3, 7),
                "syntax_spans": [(0, 1), (2, 3)],
            },
        )
        assert legacy.source_span == Span(3, 7)
        assert tuple(legacy.syntax_spans) == (Span(0, 1), Span(2, 3))
        again = pickle.loads(pickle.dumps(legacy))
        assert again.source_span == (3, 7)

    def test_legacy_paragraph_override_constructor(self):
        class Paragraph(block.Paragraph):
            override = True

            def __init__(self, lines):
                super().__init__(lines)

        from marko.parser import Parser

        parser = Parser()
        parser.add_element(Paragraph)
        para = parser.parse("hello\n").children[0]
        assert _text("hello\n", para.children[0].source_span) == "hello"


class TestRendererIndependence:
    def test_renderer_output_is_identical_on_and_off(self):
        text = "**x** [a](u)\n"
        expected = '<p><strong>x</strong> <a href="u">a</a></p>\n'
        assert Markdown(sourcemap=True).convert(text) == expected
        assert Markdown(sourcemap=False).convert(text) == expected
