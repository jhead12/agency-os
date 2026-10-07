# Starter plugin templates

`python agency_os.py new-plugin <name>` (core/scaffold.py) fills these in to
make a plugin whose page, prospect source, scheduled AI job, agent and test
already work together. See [docs/PLUGINS.md](../../docs/PLUGINS.md).

Placeholders use Python's `string.Template`: `${module}` (grant_finder),
`${key}` (grant-finder), `${Class}` (GrantFinder), `${title}` (Grant finder)
and `${ENV}` (GRANT_FINDER). Write a literal `$` as `$$`. After changing a
template, run `python -m pytest tests/test_plugin_kit.py`, which generates a
plugin and runs its tests.
