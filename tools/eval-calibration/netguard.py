"""Every hostname this process resolves, recorded — and only an allowlist reached.

ADR-0059 §2. The brief's rule was "DeepEval phones home by default: opt out and
*verify* it, do not just set it and assume". An environment variable is a
request, not evidence. This module is the evidence: it wraps the one chokepoint
every hostname-addressed connection in CPython goes through
(`socket.getaddrinfo` — `socket.create_connection`, `httpx`/`httpcore`,
`requests` and `asyncio.BaseEventLoop.getaddrinfo` all end up here, the last one
by looking the attribute up on the module at call time), records the host, and
raises on anything not named in `allow`.

So the run either finishes with a host list containing nothing but the model
provider, or it dies at the moment something tried to reach a telemetry
endpoint. Both are verdicts; neither is an assumption.

**What it does not cover, said plainly.** A library that dials a bare IP with no
DNS lookup, or one that resolves through a C-level resolver of its own (grpc's
c-ares — `grpcio` is in this venv as an OpenTelemetry transitive dep), does not
pass through `socket.getaddrinfo`. `socket.socket.connect` is wrapped too, which
catches the first of those; the second is covered only by the library's own
opt-out switch, which is why `verify_opt_outs` in the runner asserts the
libraries' own predicates *as well as* running under this guard.
"""

import socket
from dataclasses import dataclass, field

#: The only hosts a calibration run has any business reaching: the model
#: provider, the loopback the platform's own cost ledger lives on, and OpenAI's
#: tokenizer-vocabulary CDN.
#:
#: That third one was **not** in the first version of this list, and the guard
#: blocked it on the first real run: `tiktoken` — pulled in by `langchain` and
#: used by both libraries to count tokens — downloads its BPE vocabulary from
#: `openaipublic.blob.core.windows.net` the first time a cold cache asks for an
#: encoding. It carries no run data and is the provider's own asset, so it is
#: allowed by name rather than by widening the rule; it still shows up in
#: `hosts_resolved` in the results file, so the claim the run can make is the
#: precise one ("the provider and its tokenizer CDN") rather than a vague one.
DEFAULT_ALLOW: frozenset[str] = frozenset(
    {
        "api.openai.com",
        "openaipublic.blob.core.windows.net",
        "localhost",
        "127.0.0.1",
        "::1",
        "0.0.0.0",
    }
)


class BlockedHostError(RuntimeError):
    """Something tried to reach a host outside the allowlist. Never caught by the runner."""


@dataclass
class NetLog:
    """What was resolved and what was refused, for the results file to carry."""

    allowed: frozenset[str]
    resolved: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)

    def hosts(self) -> list[str]:
        return sorted(set(self.resolved))


def install(allow: frozenset[str] = DEFAULT_ALLOW) -> NetLog:
    """Wrap the socket layer for the rest of this process. Idempotent enough for one run."""
    log = NetLog(allowed=allow)
    original_getaddrinfo = socket.getaddrinfo
    original_connect = socket.socket.connect

    def guarded_getaddrinfo(host, port, *args, **kwargs):  # type: ignore[no-untyped-def]
        name = _hostname(host)
        if name and name not in allow:
            log.blocked.append(f"{name}:{port}")
            raise BlockedHostError(
                f"netguard chặn {name}:{port} — không nằm trong allowlist {sorted(allow)}. "
                "Một thư viện vừa thử gọi ra ngoài nhà cung cấp model."
            )
        log.resolved.append(name)
        return original_getaddrinfo(host, port, *args, **kwargs)

    def guarded_connect(self, address):  # type: ignore[no-untyped-def]
        # Catches a raw-IP dial that never asked DNS for a name.
        host = _hostname(address[0]) if isinstance(address, tuple) and address else ""
        if host and host not in allow and not _looks_resolved(host, log):
            log.blocked.append(f"{host} (raw)")
            raise BlockedHostError(f"netguard chặn kết nối IP thẳng tới {host!r}")
        return original_connect(self, address)

    socket.getaddrinfo = guarded_getaddrinfo  # type: ignore[assignment]
    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    return log


def _hostname(host: object) -> str:
    """A hostname as text, whatever the socket layer handed us.

    `httpcore` passes the host through as `bytes`, and a `str(b"api.openai.com")`
    is the string `"b'api.openai.com'"` — which matches no allowlist entry and
    blocks the one host the run is *supposed* to reach. Found by this guard
    blocking the model provider on its first call, which is at least the failure
    mode landing on the safe side.
    """
    if host is None:
        return ""
    if isinstance(host, (bytes, bytearray)):
        return bytes(host).decode("ascii", "replace")
    return str(host)


def _looks_resolved(host: str, log: NetLog) -> bool:
    """True when `host` is an IP we handed out ourselves via an allowed lookup.

    Without this the guard would block every real connection: `getaddrinfo`
    returns addresses, and the very next call is `connect` to one of them. Any
    lookup that got through the guard was already allowlisted by name, so its
    addresses are allowed by construction — a resolution that happened at all is
    the permission.
    """
    return bool(log.resolved) and _is_ip(host)


def _is_ip(host: str) -> bool:
    try:
        socket.inet_pton(socket.AF_INET, host)
        return True
    except OSError:
        pass
    try:
        socket.inet_pton(socket.AF_INET6, host)
        return True
    except OSError:
        return False
