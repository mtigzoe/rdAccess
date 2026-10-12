# RDAccess: Remote Desktop Accessibility for NVDA
# Copyright 2026
# License: GNU General Public License version 2.0 or later
"""Fail-closed validator for a future explicitly enabled local test interface.

No server or remote-control action is implemented here. This validator must
not be treated as caller authentication; IPC must authenticate separately.
"""

from __future__ import annotations

from typing import Any

_ALLOWED_ACTIONS = frozenset({"status"})


def validateRequest(payload: Any) -> str:
	"""Accept only a versioned, argument-free, read-only status request."""
	if type(payload) is not dict:
		raise ValueError("Invalid request envelope")
	if set(payload) != {"version", "action"}:
		raise ValueError("Unsupported request fields")
	if type(payload["version"]) is not int or payload["version"] != 1:
		raise ValueError("Unsupported request version")
	if type(payload["action"]) is not str or payload["action"] not in _ALLOWED_ACTIONS:
		raise ValueError("Unsupported action")
	return payload["action"]
