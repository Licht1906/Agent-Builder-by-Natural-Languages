"""The V2 runtime's audit chain — DS-10 / ADR-0059 §11.

`chain` is everything: what one entry is, how it is appended, and how the
chain is checked. The runtime writes through `RunServices.audit`, so the
exported package can hand it a sink of its own.
"""
