"""OreeAI Meet bot.

A separate deployable. Must never import `oreeai_nt`, and the service must
never import this package. The only contract with the service is the
container boundary and the exit-code table (bot/README.md and the Shared
contracts section of plans/note-taker-prs.md).
"""
