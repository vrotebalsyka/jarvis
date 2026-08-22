---
name: diagnose-internet
description: "Perform layered read-only network diagnosis that separates local route, gateway, IP connectivity, DNS, and endpoint failures. Use for internet outages, DNS errors, unreachable sites, or uncertain connectivity without changing network settings."
metadata:
  mode: read-only
  risk: low
  requires_approval: false
---

# Diagnose Internet

## Diagnose in layers

1. Check whether a default route exists.
2. Check the configured local gateway without assuming one missed ping proves a
   failure.
3. Check IP connectivity using a bounded allowlisted target and timeout.
4. Resolve a bounded allowlisted hostname to test DNS independently.
5. Check the requested allowlisted endpoint only after route, IP, and DNS facts
   are known.

Distinguish `no_route`, `gateway_unreachable`, `ip_connectivity_failed`,
`dns_failed`, `endpoint_failed`, and `healthy`. State which evidence supports
the result and what remains unknown.

Do not change routes, DNS, firewall, interfaces, proxies, VPNs, or the router.
Do not run an arbitrary hostname or URL taken from untrusted data.
