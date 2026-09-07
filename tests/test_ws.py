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
        client._read_deadline = None
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

    def test_poll_deadline_survives_continuous_ping_traffic(self):
        import itertools
        client = self.connection([])
        client._socket.recv.side_effect = lambda count: frame(b'ping', opcode=9)
        with mock.patch.object(ws.time, 'monotonic', side_effect=itertools.count(0, 0.1)):
            self.assertEqual(client.read_available(0.5), b'')
        self.assertGreater(client._socket.sendall.call_count, 0)
        self.assertLess(client._socket.recv.call_count, 5)
        self.assertIsNone(client._read_deadline)
        self.assertEqual(client._socket.settimeout.call_args, mock.call(20))

    def test_zero_timeout_returns_buffered_payload_without_socket_reads(self):
        client = self.connection([])
        self.assertEqual(client.read_available(0), b'')
        client._payload_buffer += b'pending'
        self.assertEqual(client.read_available(0), b'pending')
        client._socket.recv.assert_not_called()

    def test_invalid_base64_is_reported_as_a_console_error(self):
        client = self.connection([frame(b'%%%%', opcode=1)], base64_mode=True)
        with self.assertRaisesRegex(ws.WebSocketError, 'invalid base64'):
            client.recv()


class WebSocketHandshakeTests(unittest.TestCase):
    RESPONSE = (b'HTTP/1.1 101 Switching Protocols\r\n'
                b'Upgrade: websocket\r\nConnection: keep-alive, Upgrade\r\n'
                b'Sec-WebSocket-Protocol: binary\r\n'
                b'Sec-WebSocket-Accept: 3SC6TZx4582OZaOogPVxMx5CGS0=\r\n\r\n')

    def open(self, chunks, tls_error=None):
        self.raw = mock.Mock()
        self.wrapped = mock.Mock()
        context = mock.Mock()
        context.wrap_socket.return_value = self.wrapped
        context.wrap_socket.side_effect = tls_error
        def receive(count):
            self.wrapped.sendall.assert_called_once()
            sent = self.wrapped.sendall.call_args.args[0]
            self.assertIn(b'GET /console?port=5900 HTTP/1.1\r\n', sent)
            return next(chunks)
        self.wrapped.recv.side_effect = receive
        with mock.patch.object(ws.ssl, 'create_default_context', return_value=context), \
             mock.patch.object(ws.socket, 'create_connection', return_value=self.raw), \
             mock.patch.object(ws.os, 'urandom', return_value=b'a' * 16):
            return ws.WebSocket('pve.example', 8006, '/console', {'port': '5900'}, {})

    def test_upgrade_preserves_coalesced_application_data(self):
        client = self.open(iter([self.RESPONSE + frame(b'ok')]))
        self.assertEqual(client.recv(), b'ok')
        self.wrapped.close.assert_not_called()
        self.assertEqual(self.wrapped.settimeout.call_args, mock.call(20))

    def test_failed_tls_wrap_closes_raw_socket(self):
        with self.assertRaises(ws.ssl.SSLError):
            self.open(iter([]), ws.ssl.SSLError('fixture'))
        self.raw.close.assert_called_once()

    def test_rejected_handshakes_close_tls_socket(self):
        cases = [self.RESPONSE.replace(b'101 Switching', b'401 Error101'),
                 self.RESPONSE.replace(b'Upgrade: websocket', b'Upgrade: other'),
                 self.RESPONSE.replace(b'Connection: keep-alive, Upgrade', b'Connection: close'),
                 self.RESPONSE.replace(b'3SC6TZx4582OZaOogPVxMx5CGS0=', b'wrong'),
                 self.RESPONSE.replace(b'Protocol: binary', b'Protocol: unknown')]
        for response in cases:
            with self.subTest(response=response), self.assertRaises(ws.WebSocketError):
                self.open(iter([response]))
            self.wrapped.close.assert_called_once()

    def test_unterminated_headers_are_bounded(self):
        with mock.patch.object(ws, 'MAX_HANDSHAKE_SIZE', 64), self.assertRaisesRegex(ws.WebSocketError, 'too large'):
            self.open(iter([b'x' * 64]))
        self.wrapped.close.assert_called_once()
        self.assertEqual(self.wrapped.recv.call_count, 1)

    def test_dribbling_handshake_has_an_overall_deadline(self):
        import itertools
        with mock.patch.object(ws.time, 'monotonic', side_effect=itertools.count(0, 10)), self.assertRaises(TimeoutError):
            self.open(iter([b'HTTP/1.1 101']))
        self.wrapped.close.assert_called_once()
