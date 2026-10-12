# RDAccess: Remote Desktop Accessibility for NVDA
# Copyright 2026
# License: GNU General Public License version 2.0 or later
"""Read-only, secret-free status model for future opt-in AI test-session IPC.

This module deliberately has no listener, file writer, connection command,
or keyboard injection. A caller must already be executing inside NVDA.
"""
from __future__ import annotations

from typing import Any


def snapshot(bridge: Any | None) -> dict[str, Any]:
	"""Return conservative transport/presentation state without exposing secrets.

	A functioning transport does not prove a physical braille display received
	a message. The Windows NVDA client owns the actual control state.
	"""
	result: dict[str, Any] = {
		"schema_version": 1,
		"bridge_attached": bridge is not None,
		"transport_connected": "unverified",
		"remote_control_active": "unverified",
		"semantic_braille_negotiated": False,
		"temporary_message_supported": False,
		"physical_braille": "unverified",
	}
	if bridge is None or getattr(bridge, "_terminated", False):
		return result

	transport = getattr(bridge, "transport", None)
	client = getattr(bridge, "_client", None)
	if transport is None:
		return result
	connected = getattr(transport, "connected", None)
	if type(connected) is bool:
		result["transport_connected"] = connected
	if client is not None:
		leader = getattr(client, "leaderTransport", None)
		if leader is not transport:
			result["transport_connected"] = False
			return result
		sending = getattr(client, "sendingKeys", None)
		if type(sending) is bool and result["transport_connected"] is True:
			result["remote_control_active"] = sending
	if result["transport_connected"] is not True:
		return result

	# The legacy callback-manager backend cannot independently confirm that
	# Windows has keyboard control. Do not manufacture that signal.
	negotiated = getattr(bridge, "_negotiated", False) is True
	result["semantic_braille_negotiated"] = negotiated
	result["temporary_message_supported"] = negotiated
	return result
