"""Windows-only security and cancellable byte-pipe operations.

No handles, files, or listeners are created on import. All blocking operations
belong to the session worker, never NVDA's GUI thread. ctypes bindings are private
so they cannot change NVDA/pyserial's shared function prototypes.
"""

from __future__ import annotations

import ctypes as c
import os
import struct
import threading
import time
from concurrent.futures import CancelledError
from contextlib import contextmanager
from ctypes import wintypes as w
from pathlib import Path
from typing import Any

if os.name != "nt":
	raise ImportError("Local test transport requires Windows")

_kernel = c.WinDLL("kernel32", use_last_error=True)
_advapi = c.WinDLL("advapi32", use_last_error=True)
_crypt = c.WinDLL("crypt32", use_last_error=True)
_INVALID = c.c_void_p(-1).value
_OVERLAPPED = 0x40000000
_FIRST_INSTANCE = 0x80000
_REJECT_REMOTE = 8
# FILE_GENERIC_READ | FILE_WRITE_DATA | FILE_WRITE_EA | FILE_WRITE_ATTRIBUTES.
# Excludes FILE_APPEND_DATA (which also grants FILE_CREATE_PIPE_INSTANCE).
_PIPE_RIGHTS = 0x12019B


class Overlapped(c.Structure):
	_fields_ = [
		("Internal", c.c_size_t),
		("InternalHigh", c.c_size_t),
		("Offset", w.DWORD),
		("OffsetHigh", w.DWORD),
		("hEvent", w.HANDLE),
	]


class SecurityAttributes(c.Structure):
	_fields_ = [("length", w.DWORD), ("descriptor", c.c_void_p), ("inherit", w.BOOL)]


class Blob(c.Structure):
	_fields_ = [("size", w.DWORD), ("data", c.c_void_p)]


def _bind(dll: Any, name: str, result: Any, *args: Any) -> Any:
	function = getattr(dll, name)
	function.restype = result
	function.argtypes = args
	return function


_close = _bind(_kernel, "CloseHandle", w.BOOL, w.HANDLE)
_free = _bind(_kernel, "LocalFree", c.c_void_p, c.c_void_p)
_process = _bind(_kernel, "GetCurrentProcess", w.HANDLE)
_event = _bind(_kernel, "CreateEventW", w.HANDLE, c.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR)
_wait = _bind(_kernel, "WaitForSingleObject", w.DWORD, w.HANDLE, w.DWORD)
_cancel = _bind(_kernel, "CancelIoEx", w.BOOL, w.HANDLE, c.c_void_p)
_result = _bind(_kernel, "GetOverlappedResult", w.BOOL, w.HANDLE, c.c_void_p, c.c_void_p, w.BOOL)
_read = _bind(_kernel, "ReadFile", w.BOOL, w.HANDLE, c.c_void_p, w.DWORD, c.c_void_p, c.c_void_p)
_write = _bind(_kernel, "WriteFile", w.BOOL, w.HANDLE, c.c_void_p, w.DWORD, c.c_void_p, c.c_void_p)
_connect = _bind(_kernel, "ConnectNamedPipe", w.BOOL, w.HANDLE, c.c_void_p)
_disconnect = _bind(_kernel, "DisconnectNamedPipe", w.BOOL, w.HANDLE)
_createPipe = _bind(
	_kernel,
	"CreateNamedPipeW",
	w.HANDLE,
	w.LPCWSTR,
	w.DWORD,
	w.DWORD,
	w.DWORD,
	w.DWORD,
	w.DWORD,
	w.DWORD,
	c.c_void_p,
)
_createFile = _bind(
	_kernel,
	"CreateFileW",
	w.HANDLE,
	w.LPCWSTR,
	w.DWORD,
	w.DWORD,
	c.c_void_p,
	w.DWORD,
	w.DWORD,
	w.HANDLE,
)
_openToken = _bind(_advapi, "OpenProcessToken", w.BOOL, w.HANDLE, w.DWORD, c.c_void_p)
_tokenInfo = _bind(_advapi, "GetTokenInformation", w.BOOL, w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p)
_sidString = _bind(_advapi, "ConvertSidToStringSidW", w.BOOL, c.c_void_p, c.c_void_p)
_security = _bind(
	_advapi,
	"ConvertStringSecurityDescriptorToSecurityDescriptorW",
	w.BOOL,
	w.LPCWSTR,
	w.DWORD,
	c.c_void_p,
	c.c_void_p,
)
_protect = _bind(
	_crypt,
	"CryptProtectData",
	w.BOOL,
	c.c_void_p,
	w.LPCWSTR,
	c.c_void_p,
	c.c_void_p,
	c.c_void_p,
	w.DWORD,
	c.c_void_p,
)
_unprotect = _bind(
	_crypt,
	"CryptUnprotectData",
	w.BOOL,
	c.c_void_p,
	c.c_void_p,
	c.c_void_p,
	c.c_void_p,
	c.c_void_p,
	w.DWORD,
	c.c_void_p,
)


def currentUserSid() -> str:
	"""Resolve TokenUser from our process token; no account names or elevation."""
	token = w.HANDLE()
	if not _openToken(_process(), 8, c.byref(token)):
		raise c.WinError(c.get_last_error())
	try:
		size = w.DWORD()
		_tokenInfo(token, 1, None, 0, c.byref(size))
		buffer = c.create_string_buffer(size.value)
		if not _tokenInfo(token, 1, buffer, size, c.byref(size)):
			raise c.WinError(c.get_last_error())
		sid = c.cast(buffer, c.POINTER(c.c_void_p))[0]
		string = c.c_void_p()
		if not _sidString(sid, c.byref(string)):
			raise c.WinError(c.get_last_error())
		try:
			return c.wstring_at(string)
		finally:
			_free(string)
	finally:
		_close(token)


def securitySddl(*, file: bool = False) -> str:
	"""Protected DACL: network deny, exactly one allow ACE for TokenUser."""
	rights = "FA" if file else f"0x{_PIPE_RIGHTS:x}"
	return f"D:P(D;;GA;;;NU)(A;;{rights};;;{currentUserSid()})"


@contextmanager
def securityAttributes(*, file: bool = False):
	descriptor = c.c_void_p()
	if not _security(securitySddl(file=file), 1, c.byref(descriptor), None):
		raise c.WinError(c.get_last_error())
	try:
		yield SecurityAttributes(c.sizeof(SecurityAttributes), descriptor, False)
	finally:
		_free(descriptor)


def protect(data: bytes, *, decrypt: bool = False) -> bytes:
	"""User-scoped DPAPI, with UI forbidden and no machine-scope fallback."""
	buffer = c.create_string_buffer(data)
	source = Blob(len(data), c.cast(buffer, c.c_void_p))
	destination = Blob()
	function = _unprotect if decrypt else _protect
	if not function(c.byref(source), None, None, None, None, 1, c.byref(destination)):
		raise PermissionError("Cannot access local test credentials")
	try:
		return c.string_at(destination.data, destination.size)
	finally:
		_free(destination.data)


def writeProtectedFile(path: Path, data: bytes) -> None:
	"""Create a fresh private file, then atomically replace the discovery name.

	CREATE_NEW never follows an existing link or overwrites an existing object.
	Only ciphertext is written, even before replacement. The caller supplies a
	trusted per-user directory; no directories or inherited ACLs are created.
	"""
	import secrets

	temporary = path.with_name(path.name + "." + secrets.token_hex(16))
	with securityAttributes(file=True) as attributes:
		handle = _createFile(str(temporary), 0x40000000, 0, c.byref(attributes), 1, 0x80, None)
	if handle == _INVALID:
		raise c.WinError(c.get_last_error())
	try:
		try:
			buffer = c.create_string_buffer(data)
			written = w.DWORD()
			if not _write(handle, buffer, len(data), c.byref(written), None) or written.value != len(data):
				raise OSError("Cannot publish local test credentials")
		finally:
			_close(handle)
		os.replace(temporary, path)
	finally:
		temporary.unlink(missing_ok=True)


class Pipe:
	"""One non-inheritable handle; worker owns creation, I/O, and close."""

	def __init__(self, handle: int):
		if handle == _INVALID:
			raise c.WinError(c.get_last_error())
		self.handle = handle

	@classmethod
	def server(cls, address: str) -> Pipe:
		with securityAttributes() as attributes:
			return cls(
				_createPipe(
					address,
					3 | _OVERLAPPED | _FIRST_INSTANCE,
					_REJECT_REMOTE,
					1,
					4096,
					4096,
					0,
					c.byref(attributes),
				),
			)

	@classmethod
	def client(cls, address: str, cancelled: threading.Event, deadline: float) -> Pipe:
		while True:
			_check(cancelled, deadline)
			# SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION prevents server
			# impersonation from obtaining a client token usable for access.
			handle = _createFile(address, _PIPE_RIGHTS, 0, None, 3, _OVERLAPPED | 0x110000, None)
			if handle != _INVALID:
				return cls(handle)
			error = c.get_last_error()
			if error not in (2, 231):
				raise c.WinError(error)
			cancelled.wait(0.025)

	def __enter__(self) -> Pipe:
		return self

	def __exit__(self, *_args: object) -> None:
		_close(self.handle)

	def _io(self, function: Any, buffer: Any, size: int, cancelled: threading.Event, deadline: float) -> int:
		_check(cancelled, deadline)
		overlapped = Overlapped()
		overlapped.hEvent = _event(None, True, False, None)
		if not overlapped.hEvent:
			raise c.WinError(c.get_last_error())
		try:
			transferred = w.DWORD()
			if function is _connect:
				success = function(self.handle, c.byref(overlapped))
			else:
				success = function(self.handle, buffer, size, c.byref(transferred), c.byref(overlapped))
			if not success:
				error = c.get_last_error()
				if function is _connect and error == 535:  # Client beat ConnectNamedPipe.
					return 0
				if error != 997:
					raise c.WinError(error)
				try:
					while _wait(overlapped.hEvent, 25) == 258:
						_check(cancelled, deadline)
					if not _result(self.handle, c.byref(overlapped), c.byref(transferred), False):
						raise c.WinError(c.get_last_error())
				finally:
					# Cancellation must complete before the kernel can lose access
					# to this OVERLAPPED and its buffer. Named-pipe cancellation is
					# kernel-local; this completion wait runs only on the worker.
					_cancel(self.handle, c.byref(overlapped))
					_result(self.handle, c.byref(overlapped), c.byref(transferred), True)
			_check(cancelled, deadline)
			return transferred.value
		finally:
			_close(overlapped.hEvent)

	def accept(self, cancelled: threading.Event, deadline: float) -> None:
		self._io(_connect, None, 0, cancelled, deadline)

	def disconnect(self) -> None:
		_disconnect(self.handle)

	def read(self, size: int, cancelled: threading.Event, deadline: float) -> bytes:
		buffer = c.create_string_buffer(size)
		chunks = bytearray()
		while len(chunks) < size:
			count = self._io(_read, buffer, size - len(chunks), cancelled, deadline)
			if count == 0:
				raise EOFError("Local test pipe closed")
			chunks.extend(buffer.raw[:count])
		return bytes(chunks)

	def receive(self, limit: int, cancelled: threading.Event, deadline: float) -> bytes:
		size = struct.unpack("<I", self.read(4, cancelled, deadline))[0]
		if not 0 < size <= limit:
			raise ValueError("Invalid local test frame size")
		return self.read(size, cancelled, deadline)

	def send(self, data: bytes, cancelled: threading.Event, deadline: float) -> None:
		data = struct.pack("<I", len(data)) + data
		buffer = c.create_string_buffer(data)
		count = self._io(_write, buffer, len(data), cancelled, deadline)
		if count != len(data):
			raise OSError("Incomplete local test write")


def _check(cancelled: threading.Event, deadline: float) -> None:
	if cancelled.is_set():
		raise CancelledError("Local test session revoked")
	if time.monotonic() >= deadline:
		raise TimeoutError("Local test I/O timed out")
