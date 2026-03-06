# ActivityPub support

## Requirements

- [ ] `.well-known` endpoints implemented
- [ ] `/inbox` and `/outbox` implemented
- [ ] Supports custom FQN
- [ ] Support+render followers
- [ ] Sends updates when posts are updated, following the existing file monitor pattern
- [ ] Renders ActivityPub reactions and comments on the UI, preferably reusing as much infrastructure as possible from the existing Webmentions implementation
- [ ] Compatible with #Mastodon API (can be implemented in a second stage)

## Specs

- [ActivityPub specs](https://www.w3.org/TR/activitypub/)

Choice: implement the specs from scratch (and my own thin library) or reuse an existing one.

## Existing implementations (Python)

- [pyfed](https://dev.funkwhale.audio/funkwhale/pyfed) (used by Funkwhale)
    - [Getting started](https://dev.funkwhale.audio/funkwhale/pyfed/-/blob/main/documentation/getting-started.md)
- [Takahe](https://github.com/jointakahe/takahe), a full-featured ActivityPub server
    - Last commit: 2 years ago
    - ⭐ 1.2k stars
    - 🔀 90 forks
- [microblog.pub](https://github.com/tsileo/microblog.pub), single-user microblog
    - Last commit: 3 years ago
    - ⭐ 1.1k stars
    - 🔀 90 forks

## New implementation

Evaluate implementation as a:

- New module in [Madblog](file:///home/blacklight/git_tree/madblog/README.md)
- Stand-alone library, with an API similar to [Webmentions](file:///home/blacklight/git_tree/webmentions/README.md) which allows dynamic binding to several Web frameworks (FastAPI, Tornado, Flask...)

### Shopping list of components

- **pyld** (JSON-LD)
- **cryptography** (+ an HTTP Signature lib such as **httpsig**) for request signing/verification
- a job queue (Celery/RQ) for delivery retries
…but you will still be implementing:
- actor/object storage and addressing
- inbox processing rules
- outbox pagination/ordering
- delivery fan-out, retry strategy, deduplication
- WebFinger + NodeInfo
- block/undo/follow state machine quirks for Mastodon compatibility

## Considerations

- [ ] **Storage**: still file-based, like articles and Webmentions are currently implemented? Or should we use a db for ActivityPub?
- [ ] **Moderation**: configuration-based? (Can be tackled in a second stage)
