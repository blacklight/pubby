"""
WebFinger and NodeInfo response builders.
"""

from typing import Any


def build_webfinger_response(
    username: str,
    domain: str,
    actor_url: str,
) -> dict[str, Any]:
    """
    Build a WebFinger JRD response (RFC 7033).

    :param username: The actor's preferred username.
    :param domain: The domain for the ``acct:`` URI (may differ from the
        actor URL domain when using a custom webfinger domain).
    :param actor_url: The full URL of the actor endpoint.
    :return: A JRD dictionary.
    """
    return {
        "subject": f"acct:{username}@{domain}",
        "aliases": [actor_url],
        "links": [
            {
                "rel": "self",
                "type": "application/activity+json",
                "href": actor_url,
            },
            {
                "rel": "http://webfinger.net/rel/profile-page",
                "type": "text/html",
                "href": actor_url,
            },
        ],
    }


def build_nodeinfo_discovery(base_url: str) -> dict[str, Any]:
    """
    Build the NodeInfo well-known discovery document.

    :param base_url: The base URL of the server.
    :return: A NodeInfo discovery document.
    """
    return {
        "links": [
            {
                "rel": "http://nodeinfo.diaspora.software/ns/schema/2.1",
                "href": f"{base_url.rstrip('/')}/nodeinfo/2.1",
            }
        ]
    }


def build_nodeinfo_document(
    software_name: str = "mypub",
    software_version: str = "0.0.1",
    total_users: int = 1,
    total_posts: int = 0,
    open_registrations: bool = False,
) -> dict[str, Any]:
    """
    Build the NodeInfo 2.1 document.

    :param software_name: Name of the software.
    :param software_version: Version of the software.
    :param total_users: Total number of users.
    :param total_posts: Total number of posts.
    :param open_registrations: Whether new registrations are accepted.
    :return: A NodeInfo 2.1 document.
    """
    return {
        "version": "2.1",
        "software": {
            "name": software_name,
            "version": software_version,
        },
        "protocols": ["activitypub"],
        "usage": {
            "users": {
                "total": total_users,
                "activeMonth": total_users,
                "activeHalfyear": total_users,
            },
            "localPosts": total_posts,
        },
        "openRegistrations": open_registrations,
        "services": {
            "inbound": [],
            "outbound": [],
        },
        "metadata": {},
    }
