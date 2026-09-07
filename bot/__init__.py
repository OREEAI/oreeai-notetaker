"""OreeAI Meet bot.

A separate deployable. Must never import `oreeai_notetaker`, and the service must
never import this package. The only contract with the service is the
container boundary and the exit-code table (see `bot/README.md`).
"""
