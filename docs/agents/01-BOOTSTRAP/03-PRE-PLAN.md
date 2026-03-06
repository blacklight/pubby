Read /home/blacklight/git_tree/madblog/docs/research/ActivityPub/02-RESEARCH.md again and use it as an input for the next step.

Create an empty directory ~/git_tree/mypub. Use it as your context root from now on and create an empty repo in it (`git init`).

Initialize the repo as a Python library to add ActivityPub support to an existing Web app, use ~/git_tree/webmentions as a template.

## Configuration

Take particularly into account the external configuration API exposed by the library (e.g. how to configure a `Person` and its attributes.

It should work with a single-use case like Madblog for now, but ideally it should also scale to a more complex setup with more users and roles.

## Testing

Add a test suite for the library.

Aim at maximum coverage, unless it leads to impractical test suite complexity.

## Storage

- [ ] Implement as part of the library a SQLAlchemy adapter along the lines of what is provided in the Webmentions library.
- [ ] File-based storage should however be preferred in Madblog. Keep this as a high priority task once the library is ready.
- [ ] Madblog for now will not support db-based storage, but keep it as an option for the next steps.

## Documentation

- [ ] Write a README for the library, using the Webmentions README as a template. It can be deferred to a follow-up if generation exceeds the context window.

## Other questions

Responding to the _Open Questions_ part of 02-RESEARCH.md:

- [ ] **Article vs Note**: use _Article_ for Madblog, but make it configurable on the library. However keep a follow-up task on how to support replies from the blog author (this is a solution that should apply both to ActivityPub and Madblog).

- [ ] **Custom FQN domain**: Madblog will use the same domain as the `link` parameter in the config, but the library should support custom domains and adjust the WebFinger response accordingly.

- [ ] **Key rotation**: The process should be documented on the README.

- [ ] **Inbox rate limit**: A simple per-IP window (with configurable window length) should suffice for now.

## Output

Prepare a more detailed plan of the required components, dependencies and architecture considerations and write it to ~/git_tree/mypub/docs/agents/04-PLAN.md.

Focus only on the Mypub library part for now and defer all the Madblog implementation to the follow-ups.

Write down any follow-up tasks to ~/git_tree/mypub/docs/agents/05-FOLLOW-UP.md.
