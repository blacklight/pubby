"""
Tests for ``pubby.collections`` — OrderedCollection(Page) document helpers.
"""

from pubby import (
    ordered_collection,
    ordered_collection_index,
    ordered_collection_page,
)
from pubby._model import AP_CONTEXT


class TestOrderedCollection:
    def test_flat_collection(self):
        doc = ordered_collection(
            "https://e.com/users/a/following", ["https://r.com/u/1"]
        )
        assert doc == {
            "@context": AP_CONTEXT,
            "id": "https://e.com/users/a/following",
            "type": "OrderedCollection",
            "totalItems": 1,
            "orderedItems": ["https://r.com/u/1"],
        }

    def test_custom_context(self):
        doc = ordered_collection(
            "https://e.com/c", [], context="https://www.w3.org/ns/activitystreams"
        )
        assert doc["@context"] == "https://www.w3.org/ns/activitystreams"


class TestOrderedCollectionIndex:
    def test_links_first_and_last_pages(self):
        doc = ordered_collection_index("https://e.com/u/a/outbox", 45, 20)
        assert doc["type"] == "OrderedCollection"
        assert doc["totalItems"] == 45
        assert doc["first"] == "https://e.com/u/a/outbox?page=1"
        assert doc["last"] == "https://e.com/u/a/outbox?page=3"
        assert "orderedItems" not in doc

    def test_empty_collection_links_page_one(self):
        doc = ordered_collection_index("https://e.com/c", 0, 20)
        assert doc["totalItems"] == 0
        assert doc["first"] == doc["last"] == "https://e.com/c?page=1"


class TestOrderedCollectionPage:
    def test_middle_page_links_siblings(self):
        doc = ordered_collection_page("https://e.com/c", 2, 45, ["i1", "i2"], 20)
        assert doc["type"] == "OrderedCollectionPage"
        assert doc["id"] == "https://e.com/c?page=2"
        assert doc["partOf"] == "https://e.com/c"
        assert doc["totalItems"] == 45
        assert doc["orderedItems"] == ["i1", "i2"]
        assert doc["prev"] == "https://e.com/c?page=1"
        assert doc["next"] == "https://e.com/c?page=3"
        assert doc["first"] == "https://e.com/c?page=1"
        assert doc["last"] == "https://e.com/c?page=3"

    def test_bounds_omit_sibling_links(self):
        first = ordered_collection_page("https://e.com/c", 1, 45, [], 20)
        assert "prev" not in first
        assert first["next"] == "https://e.com/c?page=2"

        last = ordered_collection_page("https://e.com/c", 3, 45, [], 20)
        assert last["prev"] == "https://e.com/c?page=2"
        assert "next" not in last

    def test_single_page_collection(self):
        doc = ordered_collection_page("https://e.com/c", 1, 5, ["a"], 20)
        assert "prev" not in doc
        assert "next" not in doc
