"""runtime — the provider-agnostic core of the conversational AI runtime.

Nothing in this package may import a vendor SDK or a concrete provider
adapter. Providers implement the Protocols in runtime.interfaces and are
wired in at the composition root (server.py today).
"""
