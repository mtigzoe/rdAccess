# RDAccess: Remote Desktop Accessibility for NVDA
# Copyright 2026
# License: GNU General Public License version 2.0 or later
"""Inactive controller for user-consented, read-only local status sessions.

The controller never opens an IPC listener. The application must explicitly
enable it and separately install a secure same-user transport.
"""

from __future__ import annotations

import secrets
import threading
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
		self._cancelled = threading.Event()
		self._cancelled.set()

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
		self._cancelled = threading.Event()
		self._descriptor = descriptor
		return descriptor

	def active(self) -> bool:
		if self._descriptor is None or not self._consent.active():
			self._cancelled.set()
			return False
		return not self._cancelled.is_set()

	def stop(self) -> None:
		"""Revoke without contacting NVDA, starting a listener, or sending keys."""
		self._cancelled.set()
		self._consent.revoke()
		self._descriptor = None

	def cancellationEvent(self) -> threading.Event:
		"""Expose the current session's revocation signal to a trusted status waiter."""
		if not self.active():
			raise PermissionError("Local status session inactive")
		return self._cancelled

	def descriptor(self) -> LocalSessionDescriptor:
		if not self.active() or self._descriptor is None:
			raise PermissionError("Local status session inactive")
		return self._descriptor
