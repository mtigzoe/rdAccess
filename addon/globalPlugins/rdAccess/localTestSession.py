# RDAccess: Remote Desktop Accessibility for NVDA
# Copyright 2026
# License: GNU General Public License version 2.0 or later
"""Inactive controller for user-consented, read-only local status sessions.

The controller never opens an IPC listener. The application must explicitly
enable it and separately install a secure same-user transport.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from .localTestRequest import LocalConsent


@dataclass(frozen=True)
class LocalSessionDescriptor:
	address: str
	authKey: bytes = field(repr=False)


class LocalStatusSession:
	"""Hold ephemeral IPC credentials and revocable consent in memory only."""

	def __init__(self):
		self._consent = LocalConsent()
		self._descriptor: LocalSessionDescriptor | None = None

	def begin(self, *, lifetimeSeconds: int = 300) -> LocalSessionDescriptor:
		"""Explicit opt-in. Caller controls actual transport creation."""
		self.stop()
		from .localTestIpc import pipeAddress

		# Prepare credentials before granting consent: allocation errors must not
		# leave a half-activated session behind.
		descriptor = LocalSessionDescriptor(
			address=pipeAddress(secrets.token_hex(16)),
			authKey=secrets.token_bytes(32),
		)
		self._consent.enable(lifetimeSeconds=lifetimeSeconds)
		self._descriptor = descriptor
		return descriptor

	def active(self) -> bool:
		return self._descriptor is not None and self._consent.active()

	def stop(self) -> None:
		"""Revoke without contacting NVDA, starting a listener, or sending keys."""
		self._consent.revoke()
		self._descriptor = None

	def descriptor(self) -> LocalSessionDescriptor:
		if not self.active() or self._descriptor is None:
			raise PermissionError("Local status session inactive")
		return self._descriptor
