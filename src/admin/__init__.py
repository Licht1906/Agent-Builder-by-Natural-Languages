"""Platform governance: the settings and queues an admin owns, not a creator.

Sibling of `src.agents`, which is where an agent's own behaviour lives. The
split follows the one in issue #2: a creator decides what their agent does, an
admin decides what any agent is *allowed* to do. `src.api.admin_routes` is the
HTTP surface over this package.
"""
