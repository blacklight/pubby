class ActivityPubError(Exception):
    """
    Base exception for ActivityPub errors.
    """

    def __init__(self, message: str, **_):
        self.message = message
        super().__init__(message)


class SignatureVerificationError(ActivityPubError):
    """
    Raised when an HTTP signature cannot be verified.
    """

    def __init__(self, message: str = "HTTP signature verification failed", **_):
        super().__init__(message)


class DeliveryError(ActivityPubError):
    """
    Raised when activity delivery to a remote inbox fails.
    """

    def __init__(self, inbox_url: str, message: str = "Delivery failed", **_):
        self.inbox_url = inbox_url
        super().__init__(f"Delivery to {inbox_url} failed: {message}")


class RateLimitError(ActivityPubError):
    """
    Raised when a rate limit is exceeded.
    """

    def __init__(self, message: str = "Rate limit exceeded", **_):
        super().__init__(message)
