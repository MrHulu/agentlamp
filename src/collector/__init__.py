"""AgentLamp local collector.

Turns real Codex / Claude Code lifecycle hooks into the live orb status, with a
default-deny privacy boundary:

    provider hook fires
      -> hook_sink.py   (fire-and-forget: append raw hook JSON to the queue, exit 0)
      -> daemon.py      (drain queue -> normalize -> SANITIZE -> POST /admin/event)
      -> frame server   (sanitizes again, 2nd gate) -> device shows live status

The sanitizer is REUSED from the server package (``agentlamp_server.sanitize``);
the collector never reinvents redaction. See ``docs/collector/collector_contract.md``
and ``docs/devlog/06-task005-kickoff.md``.
"""

__all__ = ["config", "netpost", "normalize", "relaypost", "secretstore", "cli"]
