"""Kill switch: READ_ONLY_SAFE_MODE.

A hard deterministic controller. When engaged, the policy engine denies
every non-READ_ONLY action regardless of capabilities or approvals.

State is a flag file (default: ~/workspace/keel/hidden_files/security/
safe_mode) so the mode survives process restarts, plus the
KEEL_SAFE_MODE=1 environment override (used by tests and emergencies).
Engage/disengage are explicit, reasoned, ledger-auditable calls —
disengage additionally requires a named approver identity.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

SAFE_MODE_FILE = os.path.join(
    os.path.expanduser("~"), "workspace", "keel", "hidden_files",
    "security", "safe_mode")
SAFE_MODE_ENV = "KEEL_SAFE_MODE"
SAFE_MODE_FILE_ENV = "KEEL_SAFE_MODE_FILE"


def _flag_path() -> str:
    return os.environ.get(SAFE_MODE_FILE_ENV, SAFE_MODE_FILE)


class SafeMode:
    def __init__(self, flag_path: str | None = None):
        self._path = flag_path or _flag_path()

    def is_engaged(self) -> bool:
        if os.environ.get(SAFE_MODE_ENV) == "1":
            return True
        try:
            return os.path.exists(self._path)
        except OSError:
            return True  # fail closed: unreadable flag file -> assume engaged

    def engage(self, reason: str = "") -> None:
        if not reason or not reason.strip():
            raise ValueError("safe mode engage requires a reason")
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()}\n"
                    f"{reason.strip()}\n")
        os.replace(tmp, self._path)  # atomic

    def disengage(self, reason: str, approver: str) -> None:
        """Disengage requires BOTH a reason and a named approver identity.

        Deterministic: no LLM, no auto-recovery. An empty approver refuses.
        """
        if not reason or not reason.strip():
            raise ValueError("safe mode disengage requires a reason")
        if not approver or not approver.strip():
            raise ValueError("safe mode disengage requires an approver "
                             "identity — refusing")
        try:
            os.remove(self._path)
        except FileNotFoundError:
            pass
        except OSError as e:
            raise OSError(f"safe mode disengage failed: {e}")

    def status(self) -> dict:
        engaged = self.is_engaged()
        reason = ""
        engaged_at = ""
        if engaged and os.path.exists(self._path):
            try:
                with open(self._path, encoding="utf-8") as f:
                    lines = f.read().splitlines()
                engaged_at = lines[0] if lines else ""
                reason = lines[1] if len(lines) > 1 else ""
            except OSError:
                pass
        return {"engaged": engaged, "reason": reason,
                "engaged_at": engaged_at, "flag_path": self._path}
