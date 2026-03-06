# Refined follow-ups

What follows is a list of the follow-up tasks, in order of priority.

Please use `/home/blacklight/git_tree/pubby/docs/agents/00-BOOTSTRAP/04-PLAN.md` and `/home/blacklight/git_tree/pubby/docs/agents/00-BOOTSTRAP/05-FOLLOW-UP.md` for more in-depth context.

1. [ ] **Synchronous/blocking delivery**: Handle requests concurrently through a `ThreadPoolExecutor`. 
2. [ ] Add **FastAPI** and **Tornado** adapters
3. [ ] **Madblog** integration
    - [ ] Wire to the existing Flask implementation
    - [ ] Handle `on_content_change`
    - [ ] Render responses
4. [ ] **Mentions**: posts that match the `@name@domain.tld` pattern should be rendered as ActivityPub handles, and mention notifications dispatched to the user's inbox. Evaluate whether this should be implemented on Madblog or directly in the library.
5. [ ] Implement the **Mastodon API** (at least the endpoints that make sense for the supported entities)
6. [ ] **Moderation**: Implement support for `domain` and `actor` allow/block lists. These should be either lists of strings (regex and IP addresses) or maps in the format `{"domain/URL/FQN/regex/IP": "reason"}`.
  - [ ] Also expose these lists as configuration options in Madblog
7. [ ] Support (or document) **multi user/actor** setup
8. [ ] The discussion on how to handle replies consistently (preferably with a unified interface for ActivityPub and Webmentions) and the proposed implementation can be deferred. For now generate a more detailed plan under `./docs/agents/<nn>-REPLIES.md`
9. [ ] **Featured collections** / pinned posts
  - [ ] Support that on Madblog too
10. [ ] **Hashtag federation**.
  - [ ] Add `Hashtag` tag objects to `Article` objects based on article tags/categories.
  - [ ] Deliver to followers of specific hashtags.

Implement the tasks one by one.

For each task in the list above, generate a plan under `./docs/agents/<nn>-<DESCRIPTION>/01-PLAN.md` and a follow-up task under `./docs/agents/<nn>-<DESCRIPTION>/02-FOLLOW-UP.md`.

Do not modify any code yet.

After each cycle of plan+follow-up, stop and wait for my input.

If I answer yes it means that you can proceed with the implementation agreed in the plan. Also make sure that test coverage remains very high.

If I answer no it means that you should re-analyze the plan files, address my comments (marked by <!-- ... --> sections) and regenerate the plan until I answer yes.

After the implementation phase of each of the tasks:

- [ ] Add a summary of the changes in the `CHANGELOG.md` (follow `AGENTS.md` for guidelines around changelog and commit messages)
- [ ] Wait for my approval before proceeding with the next task
