"""transports/ — Carrier adapters implementing runtime.interfaces.Transport.

Each adapter owns one wire protocol end-to-end: parsing carrier JSON into
TransportEvents and turning play/clear/checkpoint calls back into carrier
messages. The session never sees a carrier payload.
"""
