"""Tests for inactive, bounded Windows local status IPC primitives."""
import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / "addon/globalPlugins/rdAccess/localTestIpc.py"
spec = importlib.util.spec_from_file_location("localTestIpc", SOURCE)
localTestIpc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(localTestIpc)


class LocalIpcTests(unittest.TestCase):
	def test_endpoint_suffix_rejects_predictable_or_malformed_values(self):
		for suffix in ("", "test", "A" * 32, "../" * 12, "g" * 32):
			with self.subTest(suffix=suffix), self.assertRaises(ValueError):
				localTestIpc.pipeAddress(suffix)
		self.assertTrue(localTestIpc.pipeAddress("a" * 32).endswith("a" * 32))

	def test_json_decode_rejects_oversized_and_non_object(self):
		for raw in (b"[]" , b"null", b"{", b"\xff", b" " * 1025):
			with self.subTest(raw=raw[:20]), self.assertRaises(ValueError):
				localTestIpc.decodeJson(raw, limit=1024)

	def test_encode_rejects_large_payload(self):
		with self.assertRaises(ValueError):
			localTestIpc.encodeJson({"text": "x" * 2000}, limit=1024)

	def test_status_client_does_not_open_pipe_with_short_key(self):
		with mock.patch.object(localTestIpc, "Client") as client:
			with self.assertRaises(ValueError):
				localTestIpc.requestStatus(localTestIpc.pipeAddress("a" * 32), b"short")
		client.assert_not_called()

	def test_status_client_sends_only_allowlisted_request(self):
		connection = mock.MagicMock()
		connection.__enter__.return_value = connection
		connection.recv_bytes.return_value = b'{"bridge_attached":false}'
		with mock.patch.object(localTestIpc, "Client", return_value=connection) as client:
			result = localTestIpc.requestStatus(localTestIpc.pipeAddress("a" * 32), b"k" * 32)
		self.assertEqual(result, {"bridge_attached": False})
		client.assert_called_once()
		self.assertEqual(json.loads(connection.send_bytes.call_args.args[0]), {
			"version": 1,
			"action": "status",
		})
		connection.recv_bytes.assert_called_once_with(4096)


if __name__ == "__main__":
	unittest.main()
