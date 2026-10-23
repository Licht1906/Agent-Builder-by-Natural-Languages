"""The one gate every outbound BYODB probe passes through — RAG-20 / #106.

`POST /external-connections/{id}/test` is a free internal network scanner the
moment it accepts a creator-supplied URL and dials it: `src.services.
connection_probe` is the only caller that ever leaves this process on a
creator's say-so, and this module is what stands between the two.

**Resolve, then check the IP — never the string.** A hostname is not an
address; `evil.example` can be made to resolve to `127.0.0.1` at any moment
its owner chooses, and a check against the literal host in the URL would
never see that. Blocking by the resolved address is what makes the check
correct rather than merely plausible.

**Checking a resolution is not the same as dialling it.** A hostname
re-resolved between the check and the actual connect can answer differently
— a low-TTL DNS rebind, exactly the ticket's own "`evil.com` trỏ về
`127.0.0.1`" line — so `pin_safe_ip` exists beside `assert_safe_url`: it
hands back the *literal* address that was validated, so a caller can dial
that address directly and never ask DNS a second time whose answer this
module never saw.
"""

import ipaddress
import socket
from urllib.parse import urlparse, urlsplit, urlunsplit


class SSRFBlockedError(ValueError):
    """`url` may not be dialled — an unsafe address, or a URL too malformed to have one.

    One exception for every way a probe must refuse, because
    `src.services.connection_probe` does the same thing with all of them:
    reports `failed` with this message as the reason, never a 500 and never a
    silent retry against the address it just refused.
    """


def _is_unsafe(ip: str) -> bool:
    """Whether `ip` names an address a probe must never be allowed to reach.

    Covers RFC1918 (`is_private`), loopback, link-local — which is also the
    cloud metadata range `169.254.169.254` lives in, so no separate rule is
    needed for it — plus the reserved, multicast and unspecified ranges
    `ipaddress` already knows about. IPv4 and IPv6 alike: `ipaddress.
    ip_address` tells the two apart from the string alone.
    """
    address = ipaddress.ip_address(ip)
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _resolve_and_validate(url: str) -> tuple[str, list[str]]:
    """`url`'s host, and every address it resolves to — raises unless all of them are safe.

    Every resolved address is checked, not just the first — `getaddrinfo` can
    return several, and a host that resolves to one public and one internal
    address is still a host a probe must not be allowed to reach, because
    nothing downstream of this call controls which of the two a later
    connection actually picks.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SSRFBlockedError(f"URL không hợp lệ (thiếu scheme http/https): {url!r}")
    host = parsed.hostname
    if not host:
        raise SSRFBlockedError(f"URL không hợp lệ (thiếu host): {url!r}")

    try:
        resolved = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise SSRFBlockedError(f"Không phân giải được host {host!r}: {exc}") from exc

    ips: list[str] = []
    for info in resolved:
        # `info[4]` is `(host, port)` for IPv4 or `(host, port, flowinfo,
        # scopeid)` for IPv6 — the union confuses mypy's tuple indexing, but
        # index 0 is the address string either way.
        ip = str(info[4][0])
        if _is_unsafe(ip):
            raise SSRFBlockedError(f"Host {host!r} phân giải về địa chỉ nội bộ/dành riêng ({ip}) — bị chặn")
        ips.append(ip)

    return host, ips


def assert_safe_url(url: str) -> str:
    """Raise `SSRFBlockedError` unless every address `url`'s host resolves to is safe to dial. Returns the host.

    A fail-fast check for a caller that wants a clear refusal before doing
    anything else with `url` — it does not, on its own, protect the
    connection actually made afterwards from a rebind between this call and
    that one. `pin_safe_ip` is what a real dial-out should use instead.
    """
    host, _ = _resolve_and_validate(url)
    return host


def pin_safe_ip(url: str) -> tuple[str, str]:
    """The host and one already-validated address to dial — closes the check-then-reconnect gap.

    Where `assert_safe_url` only proves *a* resolution was safe, this hands
    back the literal address that was checked, so the caller (`src.services.
    connection_probe.http_get_via_httpx`) can connect to that address
    directly instead of asking DNS again — the second lookup a rebind
    attacker is waiting for. The first resolved, safe address is used
    deterministically, so a caller pinning to it and one calling
    `assert_safe_url` are always talking about the same address.
    """
    host, ips = _resolve_and_validate(url)
    return host, ips[0]


def url_with_pinned_host(url: str, ip: str) -> str:
    """`url` with its host literal replaced by `ip` — port, path and query untouched.

    Shared by every caller that dials the address `pin_safe_ip` validated
    (`src.services.connection_probe`, `src.services.vector_adapter`) rather
    than kept as a private copy in each: this string surgery is as much a
    part of closing the check-then-reconnect gap as `pin_safe_ip` itself, and
    a second copy is a second place it could drift from the address that was
    actually checked. IPv6 needs bracket notation in a URL's netloc
    (`[::1]`), handled here rather than by every caller.
    """
    parsed = urlsplit(url)
    netloc_host = f"[{ip}]" if ":" in ip else ip
    netloc = netloc_host if parsed.port is None else f"{netloc_host}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
