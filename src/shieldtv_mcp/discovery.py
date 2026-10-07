"""Find Android TV devices on the LAN via mDNS (_androidtvremote2._tcp)."""

from __future__ import annotations

import asyncio

from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

SERVICE = "_androidtvremote2._tcp.local."


async def discover(timeout: float = 5.0) -> dict[str, str]:
    """Return {device name: IPv4 address} for Android TV Remote v2 devices."""
    names: set[str] = set()

    def on_change(zeroconf, service_type, name, state_change) -> None:
        if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
            names.add(name)

    azc = AsyncZeroconf()
    browser = AsyncServiceBrowser(azc.zeroconf, SERVICE, handlers=[on_change])
    try:
        await asyncio.sleep(timeout)
        found: dict[str, str] = {}
        for name in sorted(names):
            info = AsyncServiceInfo(SERVICE, name)
            if await info.async_request(azc.zeroconf, 3000):
                addrs = info.parsed_addresses(IPVersion.V4Only)
                if addrs:
                    found[name.removesuffix("." + SERVICE)] = addrs[0]
        return found
    finally:
        await browser.async_cancel()
        await azc.async_close()
