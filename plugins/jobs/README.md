# Plugin jobs

Scheduled jobs: one class per file, with `key`, `label`, `every_minutes` and
`run(job)`. They show on Administration → Jobs and run with the built-in jobs
when `AGENCY_OS_RUN_JOBS=1`. A job must be safe to repeat and never sends or
spends anything.

See [docs/PLUGINS.md](../../docs/PLUGINS.md#scheduled-jobs). To start a
plugin with a job already wired to a page and an agent, run
`python agency_os.py new-plugin <name>`.
