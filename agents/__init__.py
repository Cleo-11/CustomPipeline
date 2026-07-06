"""agents/ — agent records (JSON) and their business tool modules.

Everything in here is business content, not runtime: the runtime never
imports from this package. Agent JSONs are loaded by runtime.agent_registry;
tool modules register their ToolSpecs at the composition root (server.py).
"""
