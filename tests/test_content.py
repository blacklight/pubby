"""
Tests for ``pubby.content`` — plain-text to ActivityPub HTML rendering.
"""

from unittest.mock import MagicMock, patch

import pytest

from pubby import (
    ActivityPubHandler,
    Object,
    RenderedContent,
    build_hashtag_tags,
    display_url,
    format_duration,
    is_linkable_url,
    property_value_attachment,
    render_bio_html,
    render_link_anchor,
    render_post_html,
    render_verified_link,
    set_object_content,
)


@pytest.fixture
def hashtag_url():
    """A fixed hashtag URL callback for tests."""
    return lambda name: f"https://blog.example.com/tags/{name}"


class TestIsLinkableUrl:
    def test_accepts_http_and_https(self):
        assert is_linkable_url("https://example.com") is True
        assert is_linkable_url("http://example.com/page") is True
        assert is_linkable_url("https://example.com/?a=1&b=2") is True

    def test_rejects_other_schemes_and_schemeless(self):
        assert is_linkable_url("javascript:alert(1)") is False
        assert is_linkable_url("ftp://example.com") is False
        assert is_linkable_url("example.com") is False

    def test_rejects_empty_and_whitespace(self):
        assert is_linkable_url("") is False
        assert is_linkable_url("https://exa mple.com") is False
        assert is_linkable_url(" https://example.com") is False

    def test_rejects_quotes_and_angle_brackets(self):
        assert is_linkable_url('"https://example.com"') is False
        assert is_linkable_url("https://<x>") is False
        assert is_linkable_url('https://example.com/" onmouseover="alert(1)') is False

    def test_rejects_missing_hostname(self):
        assert is_linkable_url("https://") is False
        assert is_linkable_url("http://") is False

    def test_rejects_urlparse_valueerror(self):
        assert is_linkable_url("http://[::1") is False


class TestDisplayUrl:
    def test_strips_scheme(self):
        assert display_url("https://example.com") == "example.com"
        assert display_url("http://example.com") == "example.com"

    def test_strips_single_trailing_slash_only(self):
        assert display_url("https://example.com/") == "example.com"
        assert display_url("http://example.com/") == "example.com"

    def test_keeps_trailing_slash_with_multiple_slashes(self):
        assert display_url("https://example.com/a/b/") == "example.com/a/b/"
        assert display_url("https://example.com/?q=1") == "example.com/?q=1"

    def test_keeps_path(self):
        assert display_url("https://example.com/a") == "example.com/a"


class TestRenderLinkAnchor:
    def test_default_label_is_display_url(self):
        assert render_link_anchor("https://example.com") == (
            '<a href="https://example.com">example.com</a>'
        )

    def test_rel_attribute(self):
        assert render_link_anchor("https://example.com", rel="tag") == (
            '<a href="https://example.com" rel="tag">example.com</a>'
        )

    def test_custom_label(self):
        assert render_link_anchor("https://example.com", label="My site") == (
            '<a href="https://example.com">My site</a>'
        )

    def test_escapes_href_and_label(self):
        assert render_link_anchor("https://example.com/?a=1&b=2", label="<click>") == (
            '<a href="https://example.com/?a=1&amp;b=2">&lt;click&gt;</a>'
        )


class TestRenderBioHtml:
    def test_linkifies_urls(self):
        bio = "Find me at https://blog.example.com or http://old.example.net/page."
        assert render_bio_html(bio) == (
            'Find me at <a href="https://blog.example.com">blog.example.com</a> '
            'or <a href="http://old.example.net/page">old.example.net/page</a>.'
        )

    def test_escapes_non_url_text(self):
        bio = '<b>not bold</b> see https://a.example/?q="x"'
        assert render_bio_html(bio) == (
            "&lt;b&gt;not bold&lt;/b&gt; see "
            '<a href="https://a.example/?q=">a.example/?q=</a>&quot;x&quot;'
        )

    def test_strips_wrapping_punctuation(self):
        assert render_bio_html("(see https://example.com/path)") == (
            '(see <a href="https://example.com/path">example.com/path</a>)'
        )

    def test_keeps_balanced_brackets(self):
        assert render_bio_html("https://example.com/a_(b)") == (
            '<a href="https://example.com/a_(b)">example.com/a_(b)</a>'
        )

    def test_keeps_balanced_brackets_with_trailing_punctuation(self):
        assert render_bio_html("https://example.com/a_(b).") == (
            '<a href="https://example.com/a_(b)">example.com/a_(b)</a>.'
        )

    def test_leaves_invalid_urls_as_text(self):
        assert render_bio_html("visit https:// now") == "visit https:// now"
        assert render_bio_html("plain text") == "plain text"
        assert render_bio_html("") == ""

    def test_newlines_become_br(self):
        assert render_bio_html("line one\nline two") == "line one<br>line two"

    def test_normalizes_crlf_and_cr(self):
        assert render_bio_html("a\r\nb\rc") == "a<br>b<br>c"


class TestRenderPostHtml:
    def test_linkifies_urls_and_hashtags(self, hashtag_url):
        rc = render_post_html(
            "New track #LoFi out now: https://band.example.com/song #chill",
            hashtag_url,
        )
        assert rc.html == (
            'New track <a href="https://blog.example.com/tags/lofi" rel="tag">#LoFi</a> '
            'out now: <a href="https://band.example.com/song">band.example.com/song</a> '
            '<a href="https://blog.example.com/tags/chill" rel="tag">#chill</a>'
        )
        assert rc.hashtags == ["lofi", "chill"]

    def test_escapes_text(self, hashtag_url):
        rc = render_post_html('<b>bold</b> #tag "quotes"', hashtag_url)
        assert rc.html == (
            "&lt;b&gt;bold&lt;/b&gt; "
            '<a href="https://blog.example.com/tags/tag" rel="tag">#tag</a> '
            "&quot;quotes&quot;"
        )
        assert rc.hashtags == ["tag"]

    def test_skips_invalid_tokens(self, hashtag_url):
        rc = render_post_html("code #123 and https:// here a#b", hashtag_url)
        assert rc.html == "code #123 and https:// here a#b"
        assert rc.hashtags == []

    def test_dedupes_hashtags(self, hashtag_url):
        rc = render_post_html("#rock #Rock #ROCK", hashtag_url)
        assert rc.hashtags == ["rock"]
        assert rc.html.count('href="https://blog.example.com/tags/rock"') == 3

    def test_url_fragment_not_treated_as_hashtag(self, hashtag_url):
        rc = render_post_html("see https://example.com#fragment", hashtag_url)
        assert 'href="https://example.com#fragment"' in rc.html
        assert rc.hashtags == []

    def test_rendered_content_shape(self, hashtag_url):
        rc = render_post_html("hello", hashtag_url)
        assert isinstance(rc, RenderedContent)
        assert rc.html == "hello"
        assert rc.hashtags == []

    def test_acceptance_example(self, hashtag_url):
        rc = render_post_html("<b>x</b> #Tag1 https://e.com/a_(b).", hashtag_url)
        assert rc.html == (
            "&lt;b&gt;x&lt;/b&gt; "
            '<a href="https://blog.example.com/tags/tag1" rel="tag">#Tag1</a> '
            '<a href="https://e.com/a_(b)">e.com/a_(b)</a>.'
        )
        assert rc.hashtags == ["tag1"]

    def test_newlines_become_br(self, hashtag_url):
        rc = render_post_html("line one\n\nline two", hashtag_url)
        assert rc.html == "line one<br><br>line two"

    def test_newlines_around_tokens(self, hashtag_url):
        rc = render_post_html("a\n#tag\nhttps://e.com/x", hashtag_url)
        assert rc.html == (
            "a<br>"
            '<a href="https://blog.example.com/tags/tag" rel="tag">#tag</a><br>'
            '<a href="https://e.com/x">e.com/x</a>'
        )
        assert rc.hashtags == ["tag"]

    def test_normalizes_crlf_and_cr(self, hashtag_url):
        rc = render_post_html("a\r\nb\rc", hashtag_url)
        assert rc.html == "a<br>b<br>c"


class TestBuildHashtagTags:
    def test_maps_names_to_hashtag_dicts(self, hashtag_url):
        tags = build_hashtag_tags(["rock", "pop"], hashtag_url)
        assert tags == [
            {
                "type": "Hashtag",
                "name": "#rock",
                "href": "https://blog.example.com/tags/rock",
            },
            {
                "type": "Hashtag",
                "name": "#pop",
                "href": "https://blog.example.com/tags/pop",
            },
        ]

    def test_preserves_order_and_does_not_dedup(self, hashtag_url):
        tags = build_hashtag_tags(["rock", "Rock"], hashtag_url)
        assert tags == [
            {
                "type": "Hashtag",
                "name": "#rock",
                "href": "https://blog.example.com/tags/rock",
            },
            {
                "type": "Hashtag",
                "name": "#Rock",
                "href": "https://blog.example.com/tags/Rock",
            },
        ]

    def test_object_tag_round_trip(self, hashtag_url):
        tags = build_hashtag_tags(["rock"], hashtag_url)
        obj = Object(
            id="https://example.com/posts/1",
            type="Note",
            content="hello",
            tag=tags,
        )
        assert obj.to_dict()["tag"] == tags


class TestRenderVerifiedLink:
    def test_valid_url_renders_rel_me_anchor(self):
        value = render_verified_link("https://e.com")
        assert value == '<a href="https://e.com" rel="me">e.com</a>'

    def test_invalid_url_renders_escaped_text(self):
        assert render_verified_link("javascript:alert(1)") == "javascript:alert(1)"
        assert render_verified_link("ftp://example.com") == "ftp://example.com"
        assert render_verified_link("") == ""

    def test_custom_label(self):
        value = render_verified_link("https://e.com", label="My site")
        assert value == '<a href="https://e.com" rel="me">My site</a>'

    def test_escapes_invalid_url(self):
        value = render_verified_link('https://example.com/" onmouseover="alert(1)')
        assert "<a" not in value
        assert "&quot;" in value


class TestPropertyValueAttachment:
    def test_dict_shape(self):
        attachment = property_value_attachment("Website", "https://e.com")
        assert attachment == {
            "type": "PropertyValue",
            "name": "Website",
            "value": '<a href="https://e.com" rel="me">e.com</a>',
        }

    def test_invalid_url_is_escaped(self):
        attachment = property_value_attachment("Bad", "javascript:alert(1)")
        assert attachment["value"] == "javascript:alert(1)"

    def test_appears_in_actor_document(self, private_key):
        storage = MagicMock()
        storage.get_followers.return_value = []
        storage.get_activities.return_value = []

        handler = ActivityPubHandler(
            storage=storage,
            actor_config={
                "base_url": "https://blog.example.com",
                "username": "blog",
                "name": "Test Blog",
                "summary": "A test blog",
                "attachment": [property_value_attachment("Website", "https://e.com")],
            },
            private_key=private_key,
            async_delivery=False,
        )

        doc = handler.get_actor_document()
        assert doc["attachment"] == [
            {
                "type": "PropertyValue",
                "name": "Website",
                "value": '<a href="https://e.com" rel="me">e.com</a>',
            },
        ]

    def test_appears_in_publish_actor_update(self, private_key):
        storage = MagicMock()
        storage.get_followers.return_value = []
        storage.get_activities.return_value = []

        handler = ActivityPubHandler(
            storage=storage,
            actor_config={
                "base_url": "https://blog.example.com",
                "username": "blog",
                "name": "Test Blog",
                "summary": "A test blog",
                "attachment": [property_value_attachment("Website", "https://e.com")],
            },
            private_key=private_key,
            async_delivery=False,
        )

        # Avoid real network delivery; return the activity unchanged.
        with patch.object(
            handler.outbox, "publish", side_effect=lambda activity: activity
        ):
            activity = handler.publish_actor_update()

        obj = activity["object"]
        assert obj["attachment"] == [
            {
                "type": "PropertyValue",
                "name": "Website",
                "value": '<a href="https://e.com" rel="me">e.com</a>',
            },
        ]


class TestFormatDuration:
    def test_seconds_only(self):
        assert format_duration(3.0) == "PT3S"
        assert format_duration(59) == "PT59S"

    def test_minutes(self):
        assert format_duration(60) == "PT1M"
        assert format_duration(185) == "PT3M5S"

    def test_hours(self):
        assert format_duration(3600) == "PT1H"
        assert format_duration(3723.0) == "PT1H2M3S"

    def test_truncates_fractional_seconds(self):
        assert format_duration(63.9) == "PT1M3S"
        assert format_duration(0.5) == "PT0S"

    def test_zero_and_negative(self):
        assert format_duration(0) == "PT0S"
        assert format_duration(-42) == "PT0S"


class TestSetObjectContent:
    def test_sets_content_and_tags(self, hashtag_url):
        obj: dict = {"type": "Audio"}
        set_object_content(obj, "New track! #rock https://e.com/t", hashtag_url)

        assert obj["content"] == (
            "New track! "
            '<a href="https://blog.example.com/tags/rock" rel="tag">#rock</a> '
            '<a href="https://e.com/t">e.com/t</a>'
        )
        assert obj["tag"] == [
            {
                "type": "Hashtag",
                "name": "#rock",
                "href": "https://blog.example.com/tags/rock",
            }
        ]

    def test_merges_with_existing_tags(self, hashtag_url):
        obj = {
            "type": "Audio",
            "tag": [
                {
                    "type": "Hashtag",
                    "name": "#jazz",
                    "href": "https://blog.example.com/tags/jazz",
                }
            ],
        }
        set_object_content(obj, "Live set #jazz #fusion", hashtag_url)

        names = [t["name"] for t in obj["tag"]]
        assert names == ["#jazz", "#fusion"]

    def test_dedup_is_case_insensitive(self, hashtag_url):
        obj = {
            "type": "Audio",
            "tag": [
                {
                    "type": "Hashtag",
                    "name": "#Rock",
                    "href": "https://blog.example.com/tags/Rock",
                }
            ],
        }
        set_object_content(obj, "Out now #rock", hashtag_url)
        assert [t["name"] for t in obj["tag"]] == ["#Rock"]

    def test_noop_on_empty_or_blank_text(self, hashtag_url):
        obj: dict = {"type": "Audio"}
        set_object_content(obj, "", hashtag_url)
        set_object_content(obj, "   \n", hashtag_url)
        assert "content" not in obj
        assert "tag" not in obj

    def test_text_without_hashtags_leaves_tag_untouched(self, hashtag_url):
        obj = {
            "type": "Audio",
            "tag": [{"type": "Mention", "name": "@alice", "href": "https://e.com/a"}],
        }
        set_object_content(obj, "Just a description.", hashtag_url)
        assert obj["content"] == "Just a description."
        assert obj["tag"] == [
            {"type": "Mention", "name": "@alice", "href": "https://e.com/a"}
        ]

    def test_publish_audio_object_integration(self, private_key, hashtag_url):
        """publish_object produces a Create(Audio) in the federated shape."""
        storage = MagicMock()
        storage.get_followers.return_value = []
        storage.get_activities.return_value = []

        handler = ActivityPubHandler(
            storage=storage,
            actor_config={
                "base_url": "https://blog.example.com",
                "username": "blog",
                "name": "Test Blog",
            },
            private_key=private_key,
            async_delivery=False,
        )

        stream_url = "https://blog.example.com/files/1/download"
        track_url = "https://blog.example.com/tracks/1"
        audio = Object(
            id=track_url,
            type="Audio",
            name="Track title",
            url=[
                {"type": "Link", "href": stream_url, "mediaType": "audio/mpeg"},
                {"type": "Link", "href": track_url, "mediaType": "text/html"},
            ],
            attributed_to=[
                "https://blog.example.com/artists/1",
                handler.actor_id,
            ],
            duration=format_duration(185),
            tag=build_hashtag_tags(["jazz"], hashtag_url),
            attachment=[
                {
                    "type": "Document",
                    "mediaType": "audio/mpeg",
                    "url": stream_url,
                    "name": "Track title",
                }
            ],
        )

        obj_dict = audio.to_dict()
        set_object_content(obj_dict, "Live set #jazz #fusion", hashtag_url)
        audio.content = obj_dict["content"]
        audio.tag = obj_dict["tag"]

        with patch.object(
            handler.outbox, "publish", side_effect=lambda activity: activity
        ):
            activity = handler.publish_object(audio)

        assert activity["type"] == "Create"
        published = activity["object"]
        assert published["type"] == "Audio"
        assert published["url"] == [
            {"type": "Link", "href": stream_url, "mediaType": "audio/mpeg"},
            {"type": "Link", "href": track_url, "mediaType": "text/html"},
        ]
        assert published["attributedTo"] == [
            "https://blog.example.com/artists/1",
            handler.actor_id,
        ]
        assert published["duration"] == "PT3M5S"
        assert [t["name"] for t in published["tag"]] == ["#jazz", "#fusion"]
        assert "#jazz" in published["content"]
        assert published["attachment"][0]["type"] == "Document"
