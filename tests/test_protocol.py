import json
from pathlib import Path
import random
import unittest
from protocol import Decoder, crc16
from tests.saleae_stub import Frame, load_hla


def request(cmd=0x5003, n=4, payload=None):
    data = bytes([0xF1 if payload is None else 0xF0]) + cmd.to_bytes(2, 'little') + n.to_bytes(2, 'little')
    data += crc16(data).to_bytes(2, 'little')
    if payload is not None:
        data += payload + crc16(payload).to_bytes(2, 'little')
    return data


def response(cmd=0x5003, payload=b'\xdb\x92\x00\x00', err=0):
    data = payload + bytes([err]) + cmd.to_bytes(2, 'little')
    return data + crc16(data).to_bytes(2, 'little')


def prog(payload=b'\x02\x02\x00\x00', read=True, address=0x070010):
    head = bytes([0x61 if read else 0x60]) + address.to_bytes(3, 'big') + len(payload).to_bytes(3, 'big')
    head += (crc16(head) ^ 0xFFFF).to_bytes(2, 'big') + b'\x00' * 4
    body = payload + (crc16(payload) ^ 0xFFFF).to_bytes(2, 'big') + b'\x00\x0a\x0a'
    return (head + b'\x00' * len(body), b'\x00' * 13 + body) if read else (head + body, b'\x00' * 13 + body)


def feed(hla, tx, rx=None, swap=False):
    result = []
    frames = [Frame('enable', 0, 0)]
    for i, byte in enumerate(tx):
        data = {'mosi': bytes([byte])}
        if rx is not None:
            data['miso'] = rx[i:i + 1]
        if swap:
            data = {('miso' if k == 'mosi' else 'mosi'): v for k, v in data.items()}
        frames.append(Frame('result', i + 1, i + 1.5, data))
    frames.append(Frame('disable', len(tx) + 2, len(tx) + 2))
    for f in frames:
        out = hla.decode(f)
        if out:
            result.extend(out if isinstance(out, list) else [out])
    return result


class ProtocolTests(unittest.TestCase):
    def test_known_crc_vectors(self):
        # CRC-16/BUYPASS check value, plus captured TCS and PROG headers.
        self.assertEqual(crc16(b'123456789'), 0xFEE8)
        self.assertEqual(crc16(bytes.fromhex('f1 03 50 04 00')), 0x0AB8)
        self.assertEqual(crc16(bytes.fromhex('61 07 00 10 00 00 04')) ^ 0xFFFF, 0xB0F7)

    def test_tcs_read_response(self):
        d = Decoder()
        self.assertEqual(d.decode(request()).data['status'], 'OK')
        p = d.decode(b'\xff' * 9, response())
        self.assertEqual(p.errors, [])
        self.assertEqual(p.data['request_transaction'], 1)
        self.assertEqual(p.data['payload'], 'DB 92 00 00')

    def test_tcs_write(self):
        d = Decoder()
        p = d.decode(request(0x3109, 1, b'\x00'))
        self.assertEqual(p.data['data_crc_status'], 'OK')
        self.assertFalse(d.decode(b'\xff' * 5, response(0x3109, b'')).errors)

    def test_zero_ack_not_success(self):
        d = Decoder(); d.decode(request(0x3112, 1, b'\x01'))
        p = d.decode(b'\xff' * 5, b'\x00' * 5)
        self.assertEqual(p.data['response_crc_status'], 'OK')
        self.assertIn('ZERO_RESPONSE_NOT_ACK', p.errors)
        self.assertIn('ECHO_MISMATCH', p.errors)

    def test_response_error_code(self):
        d = Decoder(); d.decode(request())
        self.assertIn('DEVICE_ERRCODE', d.decode(b'\xff' * 9, response(err=1)).errors)

    def test_inline_polling(self):
        rx = response(0x5106, bytes(range(64)))
        tx = request(0x5106, 64) + b'\xff' * (len(rx) - 7)
        p = Decoder().decode(tx, rx)
        self.assertEqual(p.kind, 'TCS_INLINE')
        self.assertFalse(p.errors)

    def test_prog_read_and_write(self):
        for read in (True, False):
            p = Decoder().decode(*prog(read=read))
            self.assertFalse(p.errors)
            self.assertEqual(p.data['ack'], 'OK')

    def test_mode_requires_valid_state_read(self):
        d = Decoder()
        self.assertEqual(d.decode(bytes.fromhex('cc33555a')).data['mode'], 'PROG_REQUESTED')
        tx, rx = prog()
        self.assertEqual(d.decode(tx, rx).data['mode'], 'PROG_CONFIRMED')
        self.assertEqual(Decoder().decode(*prog(payload=b'\x00\x03\x00\x00')).data['mode'], 'BOOT_STATE_3')

    def test_specials_exact_match(self):
        d = Decoder()
        self.assertEqual(d.decode(b'\x81').data['die'], 'DIGITAL')
        self.assertEqual(d.decode(b'\x82').data['die'], 'ANALOG')
        self.assertEqual(d.decode(b'\x83').data['die'], 'EXTENDED')
        self.assertEqual(d.mode, 'UNKNOWN')
        self.assertEqual(d.decode(b'\x81\x00').kind, 'UNKNOWN')
        self.assertEqual(d.decode(b'\x83\x00').kind, 'UNKNOWN')

    def test_prog_bad_ack(self):
        tx, rx = prog()
        for index in (-1, -2):
            bad = bytearray(rx); bad[index] = 0xE0
            self.assertIn('ACK_E0', Decoder().decode(tx, bytes(bad)).errors)

    def test_crc_corruption(self):
        tx, rx = prog()
        bad = bytearray(rx); bad[13] ^= 1
        self.assertIn('DATA_CRC_BAD', Decoder().decode(tx, bytes(bad)).errors)
        bad = bytearray(request()); bad[5] ^= 1
        self.assertIn('HEADER_CRC_BAD', Decoder().decode(bytes(bad)).errors)

    def test_bad_request_taints_ack(self):
        d = Decoder(); bad = bytearray(request()); bad[5] ^= 1; d.decode(bytes(bad))
        self.assertIn('REQUEST_INVALID', d.decode(b'\xff' * 9, response()).errors)

    def test_lengths_and_orphan(self):
        d = Decoder(); d.decode(request())
        self.assertTrue(d.decode(b'\xff' * 5, response(payload=b'')).errors)
        self.assertIn('ORPHAN_RESPONSE', Decoder().decode(b'\xff' * 9, response()).errors)
        self.assertTrue(Decoder().decode(*[x[:-1] for x in prog()]).errors)

    def test_intervening_special_clears_request(self):
        d = Decoder(); d.decode(request()); d.decode(b'\x82')
        self.assertIn('ORPHAN_RESPONSE', d.decode(b'\xff' * 9, response()).errors)

    def test_fuzz_and_all_truncations(self):
        rng = random.Random(2468)
        for i in range(1500):
            tx = bytes(rng.randrange(256) for _ in range(rng.randrange(80)))
            Decoder().decode(tx, bytes(len(tx)))
        for tx, rx in (prog(), (request(0x3109, 1, b'\x00'), b'\x00' * 10)):
            for i in range(len(tx)):
                self.assertTrue(Decoder().decode(tx[:i], rx[:i]).errors)


class AdapterTests(unittest.TestCase):
    def test_auto_both_directions(self):
        for swap in (True, False):
            h = load_hla()
            out = feed(h, request(), b'\x00' * 7, swap)
            self.assertEqual(h.locked_direction, 'miso' if swap else 'mosi')
            self.assertEqual(out[0].type, 'TCS Request')
            out = feed(h, b'\xff' * 9, response(), swap)
            self.assertEqual(out[0].type, 'TCS Replay')
            self.assertNotIn('[error]', out[0].data['summary'])
            self.assertTrue({'die', 'err_code', 'status', 'host_line'}.isdisjoint(out[0].data))
            self.assertEqual(out[0].data['transaction'], '2')
        h = load_hla()
        for _ in range(12):
            out = feed(h, b'\x83', b'\x00', swap=True)
        self.assertEqual(h.locked_direction, 'miso')
        self.assertEqual(out[0].type, 'Special')
        self.assertEqual(out[0].data['transaction'], '12')
        self.assertEqual(out[0].data['special'], 'Switch to extended DIE (0x83)')

    def test_missing_miso(self):
        h = load_hla('MOSI'); feed(h, request())
        self.assertIn('[error]', feed(h, b'\xff' * 9)[0].data['summary'])

    def test_bad_word_and_lowlevel_error(self):
        for f in (Frame('result', 1, 2, {'mosi': b'\x01\x02'}), Frame('error', 1, 2)):
            h = load_hla(); h.decode(Frame('enable', 0, 0))
            self.assertEqual(h.decode(f).type, 'Special')
            self.assertIsNone(h.decode(Frame('disable', 3, 3)))

    def test_no_cs(self):
        self.assertEqual(load_hla().decode(Frame('result', 1, 2, {'mosi': b'\xf1'})).type, 'Special')

    def test_fields_nonoverlapping(self):
        for tx, rx in (prog(), (request(0x3109, 1, b'\x00'), b'\x00' * 10)):
            frames = feed(load_hla('MOSI', 'Fields'), tx, rx)
            self.assertTrue(frames)
            for a, b in zip(frames, frames[1:]):
                self.assertLessEqual(a.end_time, b.start_time)

    def test_manifest_entrypoint(self):
        manifest = json.loads(Path('extension.json').read_text())
        self.assertEqual(manifest['extensions']['TCS & PROG']['entryPoint'], 'HighLevelAnalyzer.Hla')
        self.assertEqual(len(__import__('command_names').COMMAND_NAMES), 167)


if __name__ == '__main__':
    unittest.main()
