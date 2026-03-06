"""
Tests for interaction rendering — produces valid HTML.
"""

from datetime import datetime, timezone

from mypub._model import Interaction, InteractionStatus, InteractionType
from mypub.render import InteractionsRenderer


def _make_interaction(
    interaction_type: InteractionType = InteractionType.REPLY,
    content: str = "<p>Test reply</p>",
    author_name: str = "Alice",
    author_url: str = "https://mastodon.social/@alice",
    author_photo: str = "https://mastodon.social/avatar.png",
) -> Interaction:
    return Interaction(
        source_actor_id="https://mastodon.social/users/alice",
        target_resource="https://blog.example.com/post/1",
        interaction_type=interaction_type,
        content=content,
        author_name=author_name,
        author_url=author_url,
        author_photo=author_photo,
        published=datetime(2024, 6, 15, 12, 0, tzinfo=timezone.utc),
        status=InteractionStatus.CONFIRMED,
    )


class TestRenderSingleInteraction:
    def test_render_reply(self):
        renderer = InteractionsRenderer()
        interaction = _make_interaction(
            interaction_type=InteractionType.REPLY,
            content="<p>Great post!</p>",
        )

        html = str(renderer.render_interaction(interaction))
        assert "ap-interaction-reply" in html
        assert "Great post!" in html
        assert "Alice" in html
        assert "mastodon.social/@alice" in html
        assert "mastodon.social/avatar.png" in html

    def test_render_like(self):
        renderer = InteractionsRenderer()
        interaction = _make_interaction(
            interaction_type=InteractionType.LIKE,
            content="",
        )

        html = str(renderer.render_interaction(interaction))
        assert "ap-interaction-like" in html
        assert "liked this" in html

    def test_render_boost(self):
        renderer = InteractionsRenderer()
        interaction = _make_interaction(
            interaction_type=InteractionType.BOOST,
            content="",
        )

        html = str(renderer.render_interaction(interaction))
        assert "ap-interaction-boost" in html
        assert "boosted this" in html

    def test_render_with_date(self):
        renderer = InteractionsRenderer()
        interaction = _make_interaction()

        html = str(renderer.render_interaction(interaction))
        assert "Jun 15, 2024" in html

    def test_render_no_photo(self):
        renderer = InteractionsRenderer()
        interaction = _make_interaction(author_photo="")

        html = str(renderer.render_interaction(interaction))
        assert "ap-interaction-author-photo" not in html

    def test_render_no_author_url(self):
        renderer = InteractionsRenderer()
        interaction = _make_interaction(author_url="")

        html = str(renderer.render_interaction(interaction))
        assert '<span class="ap-interaction-author-name">' in html

    def test_custom_template(self):
        renderer = InteractionsRenderer()
        interaction = _make_interaction()

        template = "<div>{{ interaction.author_name }}: {{ interaction.content }}</div>"
        html = str(renderer.render_interaction(interaction, template=template))
        assert "Alice" in html
        # Content is auto-escaped by Jinja2 autoescape
        assert "Test reply" in html


class TestRenderMultipleInteractions:
    def test_render_collection(self):
        renderer = InteractionsRenderer()
        interactions = [
            _make_interaction(InteractionType.REPLY, "<p>Reply</p>", "Alice"),
            _make_interaction(InteractionType.LIKE, "", "Bob", "https://example.com/@bob"),
            _make_interaction(InteractionType.BOOST, "", "Carol", "https://example.com/@carol"),
        ]

        html = str(renderer.render_interactions(interactions))
        assert "ap-interactions" in html
        assert "3 Fediverse Interactions" in html

    def test_render_empty_collection(self):
        renderer = InteractionsRenderer()
        html = str(renderer.render_interactions([]))
        # The <style> block is always rendered, but the .ap-interactions
        # container <div> should not be present when there are no interactions
        assert '<div class="ap-interactions">' not in html

    def test_render_single_item_no_plural(self):
        renderer = InteractionsRenderer()
        interactions = [_make_interaction()]
        html = str(renderer.render_interactions(interactions))
        assert "1 Fediverse Interaction" in html
        assert "Interactions" not in html


class TestHTMLValidity:
    def test_output_contains_div(self):
        renderer = InteractionsRenderer()
        interaction = _make_interaction()
        html = str(renderer.render_interaction(interaction))
        assert html.strip().startswith("<div")
        assert html.strip().endswith("</div>")

    def test_safe_url_filtering(self):
        renderer = InteractionsRenderer()
        interaction = _make_interaction(
            author_url="javascript:alert(1)",
            author_photo="data:image/png;base64,abc",
        )
        html = str(renderer.render_interaction(interaction))
        assert "javascript:" not in html
        assert "data:" not in html
