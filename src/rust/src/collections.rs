//! ActivityStreams `OrderedCollection` document helpers.
//! Mirrors `pubby.collections`.

use serde_json::{json, Value};

use crate::model::ap_context;

/// The `@context` value of a collection document — pass `None` to use
/// pubby's default context, or `Some(json!(...))` for a custom one.
fn context_or_default(context: Option<&Value>) -> Value {
    context.cloned().unwrap_or_else(ap_context)
}

/// Return a flat `OrderedCollection` document embedding `items`.
///
/// Suitable for small collections that don't need pagination (e.g. a
/// `following` collection).
///
/// :param collection_id: The dereferenceable URL of the collection.
/// :param items: The collection members, in order.
/// :param context: The `@context` value; `None` uses pubby's context.
pub fn ordered_collection(collection_id: &str, items: &[Value], context: Option<&Value>) -> Value {
    json!({
        "@context": context_or_default(context),
        "id": collection_id,
        "type": "OrderedCollection",
        "totalItems": items.len(),
        "orderedItems": items,
    })
}

/// Return an `OrderedCollection` index document for a paged collection.
///
/// The index declares `totalItems` and links the first and last pages
/// (`{collection_id}?page=N`); page documents are produced by
/// [`ordered_collection_page`].
pub fn ordered_collection_index(
    collection_id: &str,
    total_items: usize,
    page_size: usize,
    context: Option<&Value>,
) -> Value {
    let last_page = if total_items > 0 {
        total_items.div_ceil(page_size).max(1)
    } else {
        1
    };
    json!({
        "@context": context_or_default(context),
        "id": collection_id,
        "type": "OrderedCollection",
        "totalItems": total_items,
        "first": format!("{collection_id}?page=1"),
        "last": format!("{collection_id}?page={last_page}"),
    })
}

/// Return one `OrderedCollectionPage` of a paged collection.
///
/// The page links its siblings with `next`/`prev` (omitted at the
/// bounds), its parent collection with `partOf`, and the collection's
/// `first`/`last` pages, matching [`ordered_collection_index`]'s
/// `?page=N` addressing.
pub fn ordered_collection_page(
    collection_id: &str,
    page: usize,
    total_items: usize,
    items: &[Value],
    page_size: usize,
    context: Option<&Value>,
) -> Value {
    let last_page = if total_items > 0 {
        total_items.div_ceil(page_size).max(1)
    } else {
        1
    };
    let mut doc = json!({
        "@context": context_or_default(context),
        "id": format!("{collection_id}?page={page}"),
        "type": "OrderedCollectionPage",
        "partOf": collection_id,
        "totalItems": total_items,
        "first": format!("{collection_id}?page=1"),
        "last": format!("{collection_id}?page={last_page}"),
        "orderedItems": items,
    });
    if page > 1 {
        doc["prev"] = json!(format!("{collection_id}?page={}", page - 1));
    }
    if page < last_page {
        doc["next"] = json!(format!("{collection_id}?page={}", page + 1));
    }
    doc
}
