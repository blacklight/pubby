def get_default_user_agent(actor_id: str) -> str:
    """
    Get the default User-Agent header for outgoing HTTP requests.

    :param actor_id: This server's actor ID (URL).
    """
    from pubby import __version__

    return f"pubby/{__version__} (+{actor_id})"
