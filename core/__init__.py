"""
Daedalus core package.

The live dispatch model creates a Hermes kanban card carrying the issue plus
8-phase lifecycle instructions, then lets the spawned agent run the skills.
It does NOT execute the lifecycle in-process. Submodules:

- kanban          — Hermes kanban board integration (the universal tracker)
- providers       — VCS providers (GitHub / GitLab / Azure DevOps) over HTTPS APIs
- lifecycle       — LifecycleEngine: the 8-phase lifecycle logic + unit tests
- registry        — plain-text project registry (add, list, remove repo paths)

Callers import submodules directly (e.g. ``from core import kanban``); this
package intentionally exposes nothing at the top level and pulls in no heavy
dependencies, so importing it can never crash a kanban worker.
"""
