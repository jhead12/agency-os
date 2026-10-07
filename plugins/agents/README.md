# Plugin agents

Agent personas that plugins add: one Markdown file per agent, named by its key
(`grant-finder.md`). The front matter's `tasks` decide what the agent offers on
prospect pages: built-in task keys or the agent's own `{key, label, ask}`.
They appear next to the vendored personas in `agents/`; if both folders have
an agent with the same key, the one in `agents/` wins.

See [docs/PLUGINS.md](../../docs/PLUGINS.md#agents-personas).
