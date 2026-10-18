"""
ActivityStreams ``OrderedCollection`` document helpers.

Small render helpers for endpoints that serve paged collections —
outboxes, followers/following lists, libraries and any other
dereferenceable collection.  They produce the plain-document shapes
described by the spec: a flat ``OrderedCollection``, or an index document
linking ``OrderedCollectionPage`` documents via ``first``/``last`` (and
``next``/``prev`` on the pages themselves).
"""

from typing import Any, Dict, Optional, Sequence, Union

from ._model import AP_CONTEXT

ContextLike = Union[str, Sequence[Any]]
"""The ``@context`` value of a collection document — a string or list."""


def ordered_collection(
    collection_id: str,
    items: Sequence[Any],
    context: Optional[ContextLike] = None,
) -> Dict[str, Any]:
    """
    Return a flat ``OrderedCollection`` document embedding ``items``.

    Suitable for small collections that don't need pagination (e.g. a
    ``following`` collection).

    :param collection_id: The dereferenceable URL of the collection.
    :param items: The collection members, in order.
    :param context: The ``@context`` value; defaults to pubby's context.
    :return: The collection document.
    """
    return {
        "@context": context if context is not None else AP_CONTEXT,
        "id": collection_id,
        "type": "OrderedCollection",
        "totalItems": len(items),
        "orderedItems": list(items),
    }


def ordered_collection_index(
    collection_id: str,
    total_items: int,
    page_size: int,
    context: Optional[ContextLike] = None,
) -> Dict[str, Any]:
    """
    Return an ``OrderedCollection`` index document for a paged collection.

    The index declares ``totalItems`` and links the first and last pages
    (``{collection_id}?page=N``); page documents are produced by
    :func:`ordered_collection_page`.

    :param collection_id: The dereferenceable URL of the collection.
    :param total_items: The total number of items across all pages.
    :param page_size: The number of items per page.
    :param context: The ``@context`` value; defaults to pubby's context.
    :return: The collection index document.
    """
    last_page = max(1, -(-total_items // page_size)) if total_items else 1
    return {
        "@context": context if context is not None else AP_CONTEXT,
        "id": collection_id,
        "type": "OrderedCollection",
        "totalItems": total_items,
        "first": f"{collection_id}?page=1",
        "last": f"{collection_id}?page={last_page}",
    }


def ordered_collection_page(
    collection_id: str,
    page: int,
    total_items: int,
    items: Sequence[Any],
    page_size: int,
    context: Optional[ContextLike] = None,
) -> Dict[str, Any]:
    """
    Return one ``OrderedCollectionPage`` of a paged collection.

    The page links its siblings with ``next``/``prev`` (omitted at the
    bounds), its parent collection with ``partOf``, and the collection's
    ``first``/``last`` pages, matching
    :func:`ordered_collection_index`'s ``?page=N`` addressing.

    :param collection_id: The dereferenceable URL of the collection.
    :param page: The 1-based page number.
    :param total_items: The total number of items across all pages.
    :param items: The items belonging to this page, in order.
    :param page_size: The number of items per page.
    :param context: The ``@context`` value; defaults to pubby's context.
    :return: The collection page document.
    """
    last_page = max(1, -(-total_items // page_size)) if total_items else 1
    doc: Dict[str, Any] = {
        "@context": context if context is not None else AP_CONTEXT,
        "id": f"{collection_id}?page={page}",
        "type": "OrderedCollectionPage",
        "partOf": collection_id,
        "totalItems": total_items,
        "first": f"{collection_id}?page=1",
        "last": f"{collection_id}?page={last_page}",
        "orderedItems": list(items),
    }
    if page > 1:
        doc["prev"] = f"{collection_id}?page={page - 1}"
    if page < last_page:
        doc["next"] = f"{collection_id}?page={page + 1}"
    return doc
