"""
Tests for render helpers — TemplateUtils and _sanitize_html.
"""

import datetime
from datetime import timezone

from markupsafe import Markup

from pubby.render._renderer import TemplateUtils, _sanitize_html


class TestSanitizeHtml:
    def test_allows_basic_tags(self):
        html = "<p>Hello <strong>world</strong></p>"
        result = _sanitize_html(html)
        assert str(result) == html

    def test_strips_disallowed_tags(self):
        assert "<script>" not in str(_sanitize_html("<script>alert(1)</script>"))
        assert "<img" not in str(_sanitize_html('<img src="x">'))
        assert "<div>" not in str(_sanitize_html("<div>text</div>"))

    def test_strips_disallowed_attributes(self):
        result = str(
            _sanitize_html('<a href="https://x.com" onclick="alert(1)">link</a>')
        )
        assert 'href="https://x.com"' in result
        assert "onclick" not in result

    def test_strips_javascript_href(self):
        result = str(_sanitize_html('<a href="javascript:alert(1)">link</a>'))
        assert "javascript" not in result

    def test_allows_safe_href_schemes(self):
        result = str(_sanitize_html('<a href="https://example.com">link</a>'))
        assert 'href="https://example.com"' in result

    def test_allows_class_attribute(self):
        result = str(_sanitize_html('<span class="h-card">text</span>'))
        assert 'class="h-card"' in result

    def test_returns_markup(self):
        result = _sanitize_html("<p>test</p>")
        assert isinstance(result, Markup)

    def test_preserves_nested_tags(self):
        html = "<ul><li><strong>item</strong></li></ul>"
        assert str(_sanitize_html(html)) == html


class TestActorFqn:
    def test_mastodon_at_pattern(self):
        assert (
            TemplateUtils.actor_fqn("https://mastodon.social/@alice")
            == "@alice@mastodon.social"
        )

    def test_users_pattern(self):
        assert (
            TemplateUtils.actor_fqn("https://mastodon.social/users/alice")
            == "@alice@mastodon.social"
        )

    def test_bare_path(self):
        assert (
            TemplateUtils.actor_fqn("https://example.com/alice") == "@alice@example.com"
        )

    def test_trailing_slash(self):
        assert (
            TemplateUtils.actor_fqn("https://mastodon.social/@alice/")
            == "@alice@mastodon.social"
        )

    def test_empty_string(self):
        assert TemplateUtils.actor_fqn("") == ""

    def test_none(self):
        assert TemplateUtils.actor_fqn(None) == ""


class TestHostname:
    def test_extracts_hostname(self):
        assert TemplateUtils.hostname("https://example.com/path") == "example.com"

    def test_empty(self):
        assert TemplateUtils.hostname("") == ""

    def test_none(self):
        assert TemplateUtils.hostname(None) == ""  # type: ignore


class TestSafeUrl:
    def test_http(self):
        assert TemplateUtils.safe_url("https://example.com") == "https://example.com"

    def test_rejects_javascript(self):
        assert TemplateUtils.safe_url("javascript:alert(1)") == ""

    def test_rejects_no_netloc(self):
        assert TemplateUtils.safe_url("/relative/path") == ""

    def test_empty(self):
        assert TemplateUtils.safe_url("") == ""

    def test_none(self):
        assert TemplateUtils.safe_url(None) == ""

    def test_rejects_ftp(self):
        assert TemplateUtils.safe_url("ftp://example.com/file") == ""


class TestFormatDate:
    def test_datetime_obj(self):
        d = datetime.datetime(2024, 6, 15, tzinfo=timezone.utc)
        assert TemplateUtils.format_date(d) == "Jun 15, 2024"

    def test_iso_string(self):
        assert TemplateUtils.format_date("2024-06-15T12:00:00Z") == "Jun 15, 2024"

    def test_empty(self):
        assert TemplateUtils.format_date("") == ""

    def test_none(self):
        assert TemplateUtils.format_date(None) == ""

    def test_fallback(self):
        assert TemplateUtils.format_date(12345) == "12345"


class TestFormatDatetime:
    def test_datetime_obj(self):
        dt = datetime.datetime(2024, 6, 15, 14, 30, tzinfo=timezone.utc)
        assert TemplateUtils.format_datetime(dt) == "Jun 15, 2024 at 14:30"

    def test_iso_string(self):
        result = TemplateUtils.format_datetime("2024-06-15T14:30:00Z")
        assert result == "Jun 15, 2024 at 14:30"

    def test_empty(self):
        assert TemplateUtils.format_datetime("") == ""

    def test_none(self):
        assert TemplateUtils.format_datetime(None) == ""


class TestSanitizeHtmlHelper:
    """Tests for TemplateUtils.sanitize_html wrapper."""

    def test_passes_through(self):
        result = TemplateUtils.sanitize_html("<p>hello</p>")
        assert str(result) == "<p>hello</p>"

    def test_empty_returns_markup(self):
        result = TemplateUtils.sanitize_html("")
        assert isinstance(result, Markup)
        assert str(result) == ""

    def test_none_returns_markup(self):
        result = TemplateUtils.sanitize_html(None)
        assert isinstance(result, Markup)
        assert str(result) == ""


class TestToDict:
    def test_returns_callable_dict(self):
        d = TemplateUtils.to_dict()
        assert "actor_fqn" in d
        assert "format_date" in d
        assert "sanitize_html" in d
        assert callable(d["actor_fqn"])
