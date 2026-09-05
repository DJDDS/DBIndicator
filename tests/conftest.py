"""Test-only dependency shims for the deployment snapshot.

The production image installs requirements.txt, including kiteconnect.  The
sandbox used for release verification is offline and may not have that wheel
installed, so tests that import app.background need an import-compatible shim.
"""
import sys
import types

try:  # pragma: no cover - exercised only when dependency exists
    import kiteconnect  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - environment-specific shim
    mod = types.ModuleType("kiteconnect")

    class KiteConnect:
        pass

    mod.KiteConnect = KiteConnect
    sys.modules["kiteconnect"] = mod
