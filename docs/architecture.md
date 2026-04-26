# Architecture

> **Status:** placeholder — populated in Phase 9.

Will contain:

- High-level component diagram (Mermaid).
- Sequence diagram for the poll loop and command dispatch path.
- Concurrency model (`asyncio.TaskGroup`, four coroutines, graceful shutdown).
- Resilience matrix (failure mode → expected behavior).
- Deployment topology in the target homelab.

For now, the architectural source of truth is the project root `CLAUDE.md`
and the original Claude Code prompt that bootstrapped the repository.
