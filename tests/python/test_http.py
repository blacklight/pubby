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
        self.headers_seen = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        self.headers_seen.append(kwargs.get("headers") or {})
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

    def test_drops_host_and_credentials_on_cross_origin_redirect(self):
        # Signed-fetch headers are bound to the request target — the
        # next hop must recompute Host and not carry the signature to
        # a different origin.
        sess = FakeSession(
            [
                lambda url: FakeResponse(
                    url, status_code=302, location="https://ap.test/objects/1"
                ),
                lambda url: FakeResponse(url),
            ]
        )
        guarded_get(
            "https://web.test/post/1",
            headers={
                "Host": "web.test",
                "Signature": "sig",
                "Authorization": "Bearer x",
                "Accept": "application/activity+json",
            },
            timeout=5,
            session=sess,
        )
        second = sess.headers_seen[1]
        assert "Host" not in second
        assert "Signature" not in second
        assert "Authorization" not in second
        assert second["Accept"] == "application/activity+json"

    def test_keeps_signature_on_same_origin_redirect(self):
        sess = FakeSession(
            [
                lambda url: FakeResponse(url, status_code=302, location="/other/path"),
                lambda url: FakeResponse(url),
            ]
        )
        guarded_get(
            "https://web.test/post/1",
            headers={"Host": "web.test", "Signature": "sig"},
            timeout=5,
            session=sess,
        )
        second = sess.headers_seen[1]
        assert "Host" not in second
        assert second["Signature"] == "sig"
        assert sess.urls[1] == "https://web.test/other/path"

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
