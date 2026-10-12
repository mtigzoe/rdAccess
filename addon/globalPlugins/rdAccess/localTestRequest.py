# RDAccess: Remote Desktop Accessibility for NVDA
# Copyright 2026
# License: GNU General Public License version 2.0 or later
"""Fail-closed validator for a future explicitly enabled local test interface.

No server or remote-control action is implemented here. This validator must
not be treated as caller authentication; IPC must authenticate separately.
"""

from __future__ import annotations

import hmac
import secrets
import time
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


class LocalConsent:
	"""Single-session consent gate for a future authenticated local transport.

	The random token is never persisted. Enabling this gate alone creates
	no IPC endpoint and does not grant connection/keyboard permissions.
	"""

	def __init__(self):
		self._secret: str | None = None
		self._expiresAt = 0.0

	def enable(self, *, lifetimeSeconds: int = 300) -> str:
		if type(lifetimeSeconds) is not int or not 1 <= lifetimeSeconds <= 300:
			raise ValueError("Invalid consent lifetime")
		self.revoke()
		self._secret = secrets.token_urlsafe(32)
		self._expiresAt = time.monotonic() + lifetimeSeconds
		return self._secret

	def active(self) -> bool:
		"""True only during a currently valid, explicitly enabled consent window."""
		return self._secret is not None and time.monotonic() < self._expiresAt

	def revoke(self) -> None:
		self._secret = None
		self._expiresAt = 0.0

	def authorize(self, token: str, payload: Any) -> str:
		"""Require valid consent and a read-only validated command."""
		action = validateRequest(payload)
		secret = self._secret
		if (
			secret is None
			or type(token) is not str
			or time.monotonic() >= self._expiresAt
			or not hmac.compare_digest(secret, token)
		):
			raise PermissionError("Local test consent required")
		return action
