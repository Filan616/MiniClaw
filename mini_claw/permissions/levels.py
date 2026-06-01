"""Permission level constants and descriptions."""

from __future__ import annotations

# Permission level constants
L0 = "L0"
L1 = "L1"
L2 = "L2"
L3 = "L3"
L4 = "L4"

ALL_LEVELS = (L0, L1, L2, L3, L4)

LEVEL_DESCRIPTIONS: dict[str, str] = {
    L0: "read-only (read_file, http_get)",
    L1: "restricted write (write_file in allowed dirs)",
    L2: "shell (command whitelist + danger blacklist)",
    L3: "network side-effects (send message, external HTTP POST) -> approval flow",
    L4: "high-risk (rm -rf, sudo, read private keys) -> default deny",
}
