"""Tests for project.text — canonical import path for clean_post_body."""
import pytest

from project.text import clean_post_body


class TestCleanPostBody:
    def test_strips_markdown_images(self):
        assert "![" not in clean_post_body("Hello ![alt](http://img.png) world")

    def test_strips_html_tags(self):
        assert "<div>" not in clean_post_body("<div>Hello</div> world")

    def test_strips_urls(self):
        result = clean_post_body("Check https://example.com for details")
        assert "https://" not in result
        assert "Check" in result

    def test_keeps_link_text(self):
        result = clean_post_body("See [my post](http://example.com) here")
        assert "my post" in result
        assert "http://" not in result

    def test_strips_headers(self):
        result = clean_post_body("## Hello\nSome text")
        assert result.startswith("Hello")

    def test_strips_dividers(self):
        result = clean_post_body("Above\n---\nBelow")
        assert "---" not in result

    def test_collapses_whitespace(self):
        result = clean_post_body("Hello    world")
        assert "    " not in result

    def test_empty_input(self):
        assert clean_post_body("") == ""

    def test_preserves_plain_text(self):
        text = "This is a normal paragraph with no special formatting."
        assert clean_post_body(text) == text
