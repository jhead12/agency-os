"""agency-os toolkit: convenience wrappers and introspection tools.

Each module runs standalone:

    python -m tools.pipeline sync --campaign voter-guide-cbo --dry-run
    python -m tools.inspect plugins
    python -m tools.doctor
    python -m tools.wiki_serve --port 8888

The thin `pipeline` wrapper exists so toolbox scripts can import the engine
without going through Click. `inspect` answers "what do I have" questions.
`doctor` checks the environment against what the plugins need.
"""
