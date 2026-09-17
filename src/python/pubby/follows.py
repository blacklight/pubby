"""
Pending follow-request resolution.

When an inbox ``follow_policy`` (or ``manually_approves_followers`` on the
actor config) marks an incoming ``Follow`` as requiring manual approval,
pubby stores a :class:`FollowRequest` instead of a follower and sends no
reply. The application later calls :func:`accept_follow_request` or
:func:`reject_follow_request` to promote or discard the request and
deliver the matching ``Accept``/``Reject`` activity to the requester.
"""

import logging
import uuid
from typing import Callable

from cryptography.hazmat.primitives.asymmetric import rsa

from ._model import AP_CONTEXT, Follower, FollowRequest
from .handlers._outbox import deliver_activity
from .storage import ActivityPubStorage

logger = logging.getLogger(__name__)


def build_follow_response(
    actor_id: str,
    follow_activity: dict,
    response_type: str,
) -> dict:
    """
    Build the ``Accept``/``Reject`` activity answering a ``Follow``.

    The original Follow document is embedded as ``object`` so remote
    servers can match the response to the pending request.

    :param actor_id: The local actor answering the request.
    :param follow_activity: The raw incoming ``Follow`` activity.
    :param response_type: ``"Accept"`` or ``"Reject"``.
    :return: The response activity dictionary.
    """
    return {
        "@context": AP_CONTEXT,
        "id": f"{actor_id}#{response_type.lower()}-{uuid.uuid4()}",
        "type": response_type,
        "actor": actor_id,
        "object": follow_activity,
    }


def _deliver_response(
    request: FollowRequest,
    activity: dict,
    *,
    deliver: Callable[[str, dict], None] | None,
    key_id: str | None,
    private_key: rsa.RSAPrivateKey | None,
    user_agent: str | None,
    timeout: float,
) -> None:
    """Deliver the response activity to the requester's inbox."""
    inbox_url = request.inbox or request.shared_inbox
    if not inbox_url:
        logger.warning(
            "Follow request from %s has no inbox; cannot deliver %s",
            request.actor_id,
            activity.get("type"),
        )
        return
    if deliver is not None:
        deliver(inbox_url, activity)
        return
    if key_id is None or private_key is None:
        raise ValueError(
            "key_id and private_key are required when no deliver "
            "callback is provided"
        )
    deliver_activity(
        activity,
        inbox_url,
        key_id=key_id,
        private_key=private_key,
        user_agent=user_agent,
        timeout=timeout,
    )


def accept_follow_request(
    storage: ActivityPubStorage,
    request: FollowRequest,
    *,
    actor_id: str,
    deliver: Callable[[str, dict], None] | None = None,
    key_id: str | None = None,
    private_key: rsa.RSAPrivateKey | None = None,
    user_agent: str | None = None,
    timeout: float = 15.0,
) -> dict:
    """
    Approve a pending follow request.

    Promotes the request to a stored follower, removes the pending record
    and delivers an ``Accept`` activity to the requester.

    :param storage: The storage backend the request was stored in.
    :param request: The pending :class:`FollowRequest` to approve.
    :param actor_id: The local actor answering the request.
    :param deliver: Optional callback ``(inbox_url, activity)`` used to
        hand the ``Accept`` to the application's delivery pipeline (e.g.
        a task queue). When omitted, the activity is POSTed synchronously
        via :func:`pubby.deliver_activity`, which requires ``key_id`` and
        ``private_key``.
    :param key_id: Key ID for the HTTP signature (``<actor_id>#main-key``).
    :param private_key: RSA private key used to sign the delivery.
    :param user_agent: Optional ``User-Agent`` for the synchronous
        delivery.
    :param timeout: Timeout for the synchronous delivery.
    :return: The delivered ``Accept`` activity.
    """
    follower = Follower(
        actor_id=request.actor_id,
        inbox=request.inbox,
        shared_inbox=request.shared_inbox,
        followed_at=request.requested_at,
        actor_data=request.actor_data,
        target_actor_id=request.target_actor_id,
    )
    storage.store_follower(follower)
    storage.remove_follow_request(request.actor_id, request.target_actor_id)

    activity = build_follow_response(actor_id, request.activity, "Accept")
    _deliver_response(
        request,
        activity,
        deliver=deliver,
        key_id=key_id,
        private_key=private_key,
        user_agent=user_agent,
        timeout=timeout,
    )
    return activity


def reject_follow_request(
    storage: ActivityPubStorage,
    request: FollowRequest,
    *,
    actor_id: str,
    deliver: Callable[[str, dict], None] | None = None,
    key_id: str | None = None,
    private_key: rsa.RSAPrivateKey | None = None,
    user_agent: str | None = None,
    timeout: float = 15.0,
) -> dict:
    """
    Decline a pending follow request.

    Removes the pending record and delivers a ``Reject`` activity to the
    requester so their server clears the pending state.

    :param storage: The storage backend the request was stored in.
    :param request: The pending :class:`FollowRequest` to decline.
    :param actor_id: The local actor answering the request.
    :param deliver: Optional callback ``(inbox_url, activity)`` used to
        hand the ``Reject`` to the application's delivery pipeline. When
        omitted, the activity is POSTed synchronously via
        :func:`pubby.deliver_activity`, which requires ``key_id`` and
        ``private_key``.
    :param key_id: Key ID for the HTTP signature (``<actor_id>#main-key``).
    :param private_key: RSA private key used to sign the delivery.
    :param user_agent: Optional ``User-Agent`` for the synchronous
        delivery.
    :param timeout: Timeout for the synchronous delivery.
    :return: The delivered ``Reject`` activity.
    """
    storage.remove_follow_request(request.actor_id, request.target_actor_id)

    activity = build_follow_response(actor_id, request.activity, "Reject")
    _deliver_response(
        request,
        activity,
        deliver=deliver,
        key_id=key_id,
        private_key=private_key,
        user_agent=user_agent,
        timeout=timeout,
    )
    return activity
