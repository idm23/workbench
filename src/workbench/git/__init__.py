"""Talking to git and to GitHub.

`github` parses repository references and reports run results back; `worktrees`
gives each task its own checkout. Grouped because they are the two places
Workbench reaches outside its own database, and they fail in the same way —
network, permissions, someone else's state — which is why both return results
rather than raising.
"""
