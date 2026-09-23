"""Authentication and authorisation: passwords, tokens, role guards, seed accounts.

Ticket #4 deliberately lands before the tracer bullet (#5) so every endpoint
written after it is born behind a guard, instead of having auth retrofitted onto
dozens of finished endpoints later.
"""
