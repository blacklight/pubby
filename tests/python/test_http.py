import pytest

from pubby._http import guarded_get
from pubby._exceptions import ActivityPubError


class FakeResponse:
    def __init__(self, url, status_code=200, location=None, json_data=None):
        self.url = url
        self.status_code = status_code
        self.headers = {"location": location} if location else {}
        self._json = json_data or {}

    def json(self):
        return self._json


class FakeSession:
    """Queue of canned ``requests.get`` responses keyed per call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return self.responses.pop(0)(url)


class TestGuardedGet:
    def test_returns_response_without_redirect(self):
        sess = FakeSession([lambda url: FakeResponse(url, json_data={"ok": True})])
        resp = guarded_get(
            "https://remote.test/objects/1",
            headers={},
            timeout=5,
            session=sess,
        )
        assert resp.status_code == 200
        assert resp.url == "https://remote.test/objects/1"
        assert len(sess.urls) == 1

    def test_follows_redirect_and_exposes_final_url(self):
        # A web permalink redirecting to the canonical AP domain —
        # the domain-split shape Mastodon resolves on first lookup.
        sess = FakeSession(
            [
                lambda url: FakeResponse(
                    url, status_code=302, location="https://ap.test/objects/1"
                ),
                lambda url: FakeResponse(url, json_data={"id": url}),
            ]
        )
        resp = guarded_get(
            "https://web.test/post/1",
            headers={},
            timeout=5,
            session=sess,
        )
        assert resp.status_code == 200
        assert resp.url == "https://ap.test/objects/1"
        assert sess.urls == [
            "https://web.test/post/1",
            "https://ap.test/objects/1",
        ]

    def test_refuses_redirect_to_private_target(self):
        sess = FakeSession(
            [
                lambda url: FakeResponse(
                    url, status_code=302, location="http://169.254.169.254/x"
                ),
            ]
        )
        resp = guarded_get(
            "https://web.test/post/1",
            headers={},
            timeout=5,
            session=sess,
        )
        assert resp.status_code == 302
        assert len(sess.urls) == 1

    def test_refuses_unsafe_initial_url(self):
        sess = FakeSession([])
        with pytest.raises(ActivityPubError):
            guarded_get(
                "http://127.0.0.1/internal",
                headers={},
                timeout=5,
                session=sess,
            )
        assert sess.urls == []
