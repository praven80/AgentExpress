"""Observability package — token/tool/cost/latency metering for every flow.

Isolated on purpose: all telemetry capture, pricing, storage, and aggregation
live here. The rest of the app touches it only through:
  * observability.scope.set_scope(...)  — set once per agent run (AgentContext)
  * observability.meter.record_llm(...) / record_tool(...) — at the two chokepoints

Remove this folder + those few calls and the app runs exactly as before.
"""
