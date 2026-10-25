"""This computer's addresses on its networks, for the web UI's addresses and the camera search."""

from __future__ import annotations

import socket


def local_ipv4() -> list[str]:
    """IPv4 addresses of this computer, without the loopback one; the one it reaches other networks with comes first."""
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError:
        addresses = set()
    main = _main_ipv4()
    if main:
        addresses.add(main)
    return sorted((a for a in addresses if not a.startswith("127.")), key=lambda a: (a != main, a))


def _main_ipv4() -> str | None:
    """Connecting a UDP socket only chooses the route: nothing is sent."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))  # a documentation address, outside every local network
        except OSError:
            return None
        return probe.getsockname()[0]
