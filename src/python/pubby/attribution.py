"""
Attribution validation — sanity-check inbound object authorship.

HTTP signature verification proves *who delivered* an activity; it does
not prove the delivered object was authored by that actor. Without a
sanity check, a signed actor on one host can publish an object claiming a
foreign ``id`` or a foreign ``attributedTo``, letting recipients index or
re-federate a forgery.

:func:`validate` enforces two generic rules, used by ``InboxProcessor``
when ``strict_attribution`` is enabled:

- a non-empty ``attributedTo`` must name the delivering actor;
- the object ``id``, when present, must be hosted on the same authority
  as the delivering actor.

Objects lacking comparable fields pass — absence of evidence is not
treated as a mismatch.
"""

from typing import Any, Dict

from ._exceptions import AttributionMismatch
from .moderation import extract_domain


def validate(actor: str, obj: Dict[str, Any]) -> None:
    """
    Check that ``obj`` can plausibly be attributed to ``actor``.

    Rules:

    - ``attributedTo`` absent or empty: accepted (nothing to compare).
    - ``attributedTo`` string: must equal ``actor``.
    - ``attributedTo`` list: the actor must appear among its string
      members; a dict member is expanded through its ``id``.
    - ``attributedTo`` of any other type: rejected — an unverifiable
      attribution must not bypass the scalar-mismatch check.
    - ``id`` absent: the host check is skipped.
    - ``id`` present: its domain (per :func:`pubby.extract_domain`) must
      match the actor's. Unparseable ids yield an empty domain and are
      rejected.

    :param actor: The authenticated actor URL from the activity envelope.
    :param obj: The activity's object mapping.
    :raises AttributionMismatch: If attribution or host checks fail.
    """
    attributed_to = obj.get("attributedTo")
    if attributed_to:
        if not _attribution_matches(attributed_to, actor):
            raise AttributionMismatch(
                f"attributedTo {attributed_to!r} does not match actor {actor!r}"
            )
    elif attributed_to is not None and not isinstance(attributed_to, (str, list, dict)):
        # A non-empty non-string/list/dict attributedTo cannot be
        # verified — treat it as a mismatch rather than letting it slide.
        raise AttributionMismatch(
            f"attributedTo of unsupported type for actor {actor!r}"
        )

    object_id = obj.get("id")
    if isinstance(object_id, str) and object_id:
        obj_domain = extract_domain(object_id)
        actor_domain = extract_domain(actor)
        if not obj_domain or not actor_domain or obj_domain != actor_domain:
            raise AttributionMismatch(
                f"object id {object_id!r} is hosted on a different authority "
                f"than actor {actor!r}"
            )


def _attribution_matches(attributed_to: Any, actor: str) -> bool:
    """Return whether an ``attributedTo`` value names ``actor``."""
    if isinstance(attributed_to, str):
        return attributed_to == actor
    if isinstance(attributed_to, dict):
        return attributed_to.get("id") == actor
    if isinstance(attributed_to, list):
        return any(_attribution_matches(item, actor) for item in attributed_to)
    return False
