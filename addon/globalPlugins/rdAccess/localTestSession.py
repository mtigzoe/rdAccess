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
from collections.abc import Callable
from typing import Any
from dataclasses import dataclass, field

from .localTestRequest import LocalConsent


def validateLifetime(value: int) -> int:
	"""Reject malformed consent durations without altering an active session."""
	if type(value) is not int or not 1 <= value <= 300:
		raise ValueError("Invalid consent lifetime")
	return value


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
		self._lock = threading.RLock()

	def begin(self, *, lifetimeSeconds: int = 300) -> LocalSessionDescriptor:
		"""Create a new opt-in session atomically; never open an IPC listener."""
		from .localTestIpc import pipeAddress

		validateLifetime(lifetimeSeconds)
		with self._lock:
			# Credential allocation must succeed before replacing an active session.
			descriptor = LocalSessionDescriptor(
				address=pipeAddress(secrets.token_hex(16)),
				authKey=secrets.token_bytes(32),
			)
			# Prepare the consent token before revoking the previous session.
			# Its random generation may fail independently of pipe credentials.
			newConsent = LocalConsent()
			newConsent.enable(lifetimeSeconds=lifetimeSeconds)
			self._stopLocked()
			self._consent = newConsent
			self._cancelled = threading.Event()
			self._descriptor = descriptor
			return descriptor

	def _activeLocked(self) -> bool:
		if self._descriptor is None or not self._consent.active():
			self._cancelled.set()
			return False
		return not self._cancelled.is_set()

	def active(self) -> bool:
		with self._lock:
			return self._activeLocked()

	def _stopLocked(self) -> None:
		self._cancelled.set()
		self._consent.revoke()
		self._descriptor = None

	def stop(self) -> None:
		"""Revoke pending waits and credentials without starting a listener."""
		with self._lock:
			self._stopLocked()

	def isCurrent(self, descriptor: LocalSessionDescriptor) -> bool:
		"""Reject stale credentials after rotation, revocation, or expiry."""
		with self._lock:
			return self._activeLocked() and self._descriptor is descriptor

	def readStatus(
		self,
		descriptor: LocalSessionDescriptor,
		callback: Callable[[], dict[str, Any]],
	) -> dict[str, Any]:
		"""Run an authorized, read-only status query against this session.

		Never starts a listener, invokes remote connection, or sends input.
		A revoked session must not return a status after callback completion.
		"""
		from .localTestIpc import waitForStatus

		with self._lock:
			if not self._activeLocked() or self._descriptor is not descriptor:
				raise PermissionError("Local status session inactive")
			cancelled = self._cancelled
		result = waitForStatus(callback, cancelled=cancelled)
		with self._lock:
			if not self._activeLocked() or self._descriptor is not descriptor:
				raise PermissionError("Local status session revoked")
		return result

	def cancellationEvent(self) -> threading.Event:
		"""Get the current session's cancellation signal, when still active."""
		with self._lock:
			if not self._activeLocked():
				raise PermissionError("Local status session inactive")
			return self._cancelled

	def descriptor(self) -> LocalSessionDescriptor:
		with self._lock:
			if not self._activeLocked() or self._descriptor is None:
				raise PermissionError("Local status session inactive")
			return self._descriptor
