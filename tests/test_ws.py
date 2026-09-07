"""Active wire-level regressions for resumable console reads."""
import base64
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from proxmox_agent_lab import ws


def frame(payload, opcode=2, final=True, width=0):
    first = opcode | (0x80 if final else 0)
    if width:
        return bytes([first, 126 if width == 2 else 127]) + len(payload).to_bytes(width, 'big') + payload
    return bytes([first, len(payload)]) + payload


class WebSocketReadTests(unittest.TestCase):
    def connection(self, chunks, base64_mode=False):
        client = ws.WebSocket.__new__(ws.WebSocket)
        client._recv_buffer = bytearray()
        client._payload_buffer = bytearray()
        client._fragment_opcode = None
        client._fragments = bytearray()
        client._base64 = base64_mode
        client._socket = mock.Mock()
        client._socket.gettimeout.return_value = 20
        client._socket.recv.side_effect = chunks
        return client

    def test_timeout_at_every_frame_boundary_resumes_without_data_loss(self):
        for width, payload in [(0, b'hello'), (2, b'x' * 126), (8, b'y' * 65536)]:
            wire = frame(payload, width=width)
            # Include every header boundary and representative payload splits.
            for split in list(range(1, 3 + width)) + [len(wire) - 1]:
                with self.subTest(width=width, split=split):
                    client = self.connection([wire[:split], TimeoutError(), wire[split:] + frame(b'next')])
                    self.assertEqual(client.read_available(0.01), b'')
                    self.assertEqual(client.read_available(0.01), payload)
                    self.assertEqual(client.recv(), b'next')
                    self.assertEqual(client._socket.settimeout.call_args, mock.call(20))

    def test_base64_fragments_survive_timeout_and_interleaved_ping(self):
        encoded = base64.b64encode(b'console output')
        client = self.connection([
            frame(encoded[:3], opcode=1, final=False),
            frame(b'probe', opcode=9), TimeoutError(),
            frame(encoded[3:], opcode=0),
        ], base64_mode=True)
        self.assertEqual(client.read_available(0.01), b'')
        self.assertEqual(client.recv(), b'console output')
        pong = client._socket.sendall.call_args.args[0]
        self.assertEqual(pong[:2], bytes([0x8a, 0x85]))
        self.assertEqual(bytes(b ^ pong[2 + i % 4] for i, b in enumerate(pong[6:])), b'probe')

    def test_invalid_frames_are_rejected_before_reading_payload(self):
        for wire in [b'\x82\x80', b'\xc2\x00', b'\x83\x00', b'\x09\x00', b'\x89\x7e']:
            with self.subTest(wire=wire):
                client = self.connection([wire])
                with self.assertRaises(ws.WebSocketError):
                    client.recv()
                self.assertEqual(client._socket.recv.call_count, 1)

    def test_invalid_fragment_order_is_rejected(self):
        for wire in [frame(b'a', opcode=0), frame(b'a', final=False) + frame(b'b')]:
            with self.subTest(wire=wire):
                with self.assertRaises(ws.WebSocketError):
                    self.connection([wire]).recv()

    def test_total_fragment_size_is_bounded(self):
        client = self.connection([frame(b'abc', final=False) + frame(b'def', opcode=0)])
        with mock.patch.object(ws, 'MAX_FRAME_SIZE', 5):
            with self.assertRaisesRegex(ws.WebSocketError, 'oversized'):
                client.recv()

    def test_read_exact_keeps_surplus_message_bytes(self):
        client = self.connection([frame(b'ab', final=False) + frame(b'cdef', opcode=0)])
        self.assertEqual(client.read_exact(3), b'abc')
        self.assertEqual(client.read_available(0.01), b'def')
