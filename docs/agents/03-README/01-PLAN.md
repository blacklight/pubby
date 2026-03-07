# 03 — README

## Proposed Structure

Mirror the webmentions README style — practical, code-heavy, minimal prose.

### 1. Header
- Project name + one-line description
- CI badge (GitHub Actions build), placeholder for coverage/codacy badges

**Use the following CI badges**

- Build: [![build](https://github.com/blacklight/pubby/actions/workflows/build.yml/badge.svg)](https://github.com/blacklight/pubby/actions/workflows/build.yml)
- Coverage: [![Codacy Badge](https://app.codacy.com/project/badge/Coverage/7a135acdc1e3427ab381d91c0046790c)](https://app.codacy.com/gh/blacklight/pubby/dashboard?utm_source=gh&utm_medium=referral&utm_content=&utm_campaign=Badge_coverage)
- Code quality: [![Codacy Badge](https://app.codacy.com/project/badge/Grade/7a135acdc1e3427ab381d91c0046790c)](https://app.codacy.com/gh/blacklight/pubby/dashboard?utm_source=gh&utm_medium=referral&utm_content=&utm_campaign=Badge_grade)

#### Banner image

- [ ] Generate a banner image for the project, preferably in SVG format, and place it under `img/`
- [ ] The image should write "Pubby" using a wobbly/cartoonic font (but don't overdo it)
- [ ] It should have either a wire or some blocks flowing down some channel as a theme, either in the font art or behind the letters - but don't overdo it

### 2. What is ActivityPub? (brief)
- 3-4 sentences max. Link to the spec. Explain the core idea: servers exchange JSON-LD activities to federate content (posts, likes, follows, boosts).

### 3. What is Pubby?
- Framework-agnostic Python library to add ActivityPub federation to any web app.
- Handles: inbox processing, outbox delivery, WebFinger/NodeInfo discovery, HTTP Signatures, interaction storage.
- Adapters for Flask, FastAPI, Tornado. Storage adapters for SQLAlchemy and file-based JSON.

### 4. Installation
- Base: `pip install pubby`
- Extras: `pip install "pubby[flask]"`, `"pubby[fastapi]"`, `"pubby[tornado]"`, `"pubby[db]"`
- Combined: `pip install "pubby[db,flask]"`

### 5. Quick Start
One complete, minimal example per framework (Flask, FastAPI, Tornado) — each showing:
- Create storage (SQLAlchemy in-memory for simplicity)
- Generate or load RSA keypair
- Create `ActivityPubHandler`
- Bind routes with `bind_activitypub`
- Run the app

Keep each example ~20 lines. Same pattern as webmentions README.

### 6. Publishing Content
Show how to publish an `Article` (or `Note`) to followers:
```python
from pubby import Object
obj = Object(id=..., type="Article", name="My Post", content="<p>...</p>", ...)
handler.publish_object(obj)
```

### 7. Custom Storage
- Show how to extend `ActivityPubStorage` with your own backend (same pattern as webmentions' custom storage section).
- Brief list of abstract methods to implement.

### 8. Key Management
- Show `generate_rsa_keypair()` and `load_private_key()` for generating/loading keys.
- Note: keys should be persisted (not regenerated on restart).

### 9. Configuration Reference
Table of `ActivityPubHandler` constructor parameters:
- `storage`, `actor_config`, `private_key`/`private_key_path`, `on_interaction_received`, `webfinger_domain`, `user_agent`, `http_timeout`, `max_retries`, `max_delivery_workers`, `software_name`, `software_version`

Table of `actor_config` keys:
- `base_url`, `username`, `name`, `summary`, `icon_url`, `actor_path`, `type`, `manually_approves_followers`

### 10. Rendering Interactions
- Show `handler.render_interactions(interactions)` returning safe `Markup`.
- Brief Jinja2 template example.

### 11. Rate Limiting
- Show `RateLimiter(max_requests=100, window_seconds=60)` passed to `bind_activitypub`.

### 12. Tests
```bash
pip install -e ".[test]"
pytest tests
```

### 13. Development
```bash
pip install -e ".[dev]"
pre-commit install
pre-commit run --all-files
```

### 14. License
AGPL-3.0-or-later
