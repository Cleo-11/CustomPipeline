"""providers — concrete vendor adapters implementing runtime.interfaces.

Vendor names live here and nowhere else. Adapters take their credentials
and settings as constructor arguments; only the composition root reads
config and decides which adapter to wire.
"""
