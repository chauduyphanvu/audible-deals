"""Which addresses a webhook may reach, checked when it is sent.

``validate_webhook_url`` checks a URL when it is entered, but DNS answers
change: a name that resolved to a public address then can resolve to
127.0.0.1 or a cloud metadata address when the webhook is sent (DNS
rebinding). So the check is repeated at send time, on the very addresses the
connection will use: :class:`AddressGuard` resolves the host once, refuses
non-public addresses, and connects to the address it checked. TLS still
verifies the certificate against the host name, because only the socket is
pinned, not the URL.

Self-hosted targets on your own network (ntfy, Home Assistant) are allowed
with ``deals config set webhook-allow-private true``.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

Resolver = Callable[[str, int], Sequence[str]]

ALLOW_PRIVATE_HINT = (
    "To send to a server on your own network, run "
    "'deals config set webhook-allow-private true'."
)


class BlockedAddressError(ValueError):
    """The webhook host resolves to an address that is not on the public internet."""


def system_resolver(host: str, port: int) -> list[str]:
    """Every address the operating system resolves *host* to, in order."""
    seen: list[str] = []
    for info in socket.getaddrinfo(host, port):
        address = str(info[4][0])
        if address not in seen:
            seen.append(address)
    return seen


def is_public_address(address: str) -> bool:
    """Whether *address* is a globally routable unicast address."""
    ip = ipaddress.ip_address(address.partition("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global


@dataclass(frozen=True)
class AddressGuard:
    """Resolve, check and connect, as one step."""

    allow_private: bool = False
    resolver: Resolver = field(default=system_resolver)

    def addresses(self, host: str, port: int) -> list[str]:
        """The addresses to connect to for *host*, or :class:`BlockedAddressError`."""
        addresses = list(self.resolver(host, port))
        if not addresses:
            raise BlockedAddressError(f"Cannot resolve webhook host {host!r}")
        if not self.allow_private:
            for address in addresses:
                if not is_public_address(address):
                    raise BlockedAddressError(
                        f"Webhook URL resolves to non-public address {address}. "
                        f"{ALLOW_PRIVATE_HINT}"
                    )
        return addresses

    def create_connection(
        self,
        address: tuple[str, int],
        timeout: float | None = socket.getdefaulttimeout(),
        source_address: tuple[str, int] | None = None,
    ) -> socket.socket:
        """``socket.create_connection`` that only connects to checked addresses."""
        host, port = address
        last_error: OSError | None = None
        for ip in self.addresses(host, port):
            try:
                return socket.create_connection((ip, port), timeout, source_address)
            except OSError as exc:
                last_error = exc
        raise last_error or OSError(f"Cannot connect to {host}:{port}")
