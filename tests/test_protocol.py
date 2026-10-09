import json
from pathlib import Path
import random
import unittest
from protocol import Decoder, crc16, tcs_command_id
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


def coordinates(payload=bytes(57), n=112):
    rx = response(0x5105, payload)
    tx = request(0x5105, n) + bytes(n - 7) + b'\x7f' * 5
    return tx, rx + b'\x7f' * (len(tx) - len(rx))


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
    def test_command_id_class_and_command_are_decimal(self):
        for raw, expected in ((0x5105, '17:05'), (0x3105, '17:05'),
                              (0x5003, '16:03'), (0x510A, '17:10'),
                              (0x311A, '17:26'), (0x5FFF, '31:255')):
            with self.subTest(raw=raw):
                self.assertEqual(tcs_command_id(raw), expected)

    def test_command_id_display_preserves_wire_validation(self):
        for cmd, payload in ((0x5107, None), (0x3107, b'\x01')):
            with self.subTest(cmd=cmd):
                d = Decoder()
                p = d.decode(request(cmd, 1, payload))
                self.assertEqual(p.data['status'], 'OK')
                self.assertEqual(p.data['command_id'], '17:07')
                self.assertEqual(p.data['command'], '0x{:04X}'.format(cmd))
                self.assertIn('CMDID=17:07', p.data['summary'])
                reply_payload = b'\x01' if payload is None else b''
                rx = response(cmd, reply_payload)
                p = d.decode(b'\xff' * len(rx), rx)
                self.assertEqual(p.data['status'], 'OK')
                self.assertEqual(p.data['command_id'], '17:07')
                self.assertEqual(p.data['echo_command_id'], '17:07')
        d = Decoder(); d.decode(request(0x5107, 1))
        p = d.decode(b'\xff' * 6, response(0x3107, b'\x01'))
        self.assertEqual(p.data['command_id'], p.data['echo_command_id'])
        self.assertIn('ECHO_MISMATCH', p.errors)
        self.assertEqual(p.data['response_crc_status'], 'OK')

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

    def test_inline_compressed_coordinates(self):
        # Captured empty report: 57 data bytes, 00 05 51 E5 9F, then 55 x 7F.
        tx, rx = coordinates()
        self.assertEqual(tx[:7], bytes.fromhex('f1 05 51 70 00 aa 4a'))
        self.assertEqual(rx[57:62], bytes.fromhex('00 05 51 e5 9f'))
        d = Decoder()
        for i in range(1, 4):
            p = d.decode(tx, rx)
            self.assertEqual(p.kind, 'TCS_INLINE')
            self.assertEqual(p.data['status'], 'OK')
            self.assertEqual(p.data['requested_length'], 112)
            self.assertEqual(p.data['length'], 57)
            self.assertEqual(p.data['payload'], ' '.join(['00'] * 57))
            self.assertEqual(p.data['response_padding_length'], 55)
            self.assertEqual(p.data['response_crc_status'], 'OK')
            self.assertEqual(p.data['request_transaction'], i)
            self.assertIsNone(d.pending)
            self.assertFalse(p.warnings)
            self.assertIn('read 112 bytes / TCS Replay 57 bytes', p.data['summary'])
        self.assertEqual(d.decode(request()).data['status'], 'OK')

    def test_coordinates_full_and_split_responses(self):
        # Payload may contain 7F bytes and a command echo inside the data.
        for n in (57, 112, 128):
            payload = b'\x7f' * 57 + b'\x00\x05\x51' + b'\x7f' * n
            payload = payload[:n]
            p = Decoder().decode(*coordinates(payload, n))
            self.assertEqual(p.data['status'], 'OK')
            self.assertEqual(p.data['payload'], payload.hex(' ').upper())
            self.assertNotIn('response_padding_length', p.data)
        d = Decoder()
        self.assertEqual(d.decode(request(0x5105, 57)).kind, 'TCS_REQUEST')
        p = d.decode(b'\xff' * 62, response(0x5105, bytes(57)))
        self.assertEqual(p.data['status'], 'OK')
        self.assertEqual(p.data['request_transaction'], 1)

    def test_coordinates_corruption_is_not_hidden(self):
        tx, rx = coordinates()
        for index, error in ((0, 'RESPONSE_CRC_BAD'), (60, 'RESPONSE_CRC_BAD'),
                             (58, 'ECHO_MISMATCH'), (57, 'DEVICE_ERRCODE'),
                             (62, 'RESPONSE_CRC_BAD')):
            with self.subTest(index=index):
                bad = bytearray(rx); bad[index] ^= 1
                self.assertIn(error, Decoder().decode(tx, bytes(bad)).errors)
        bad = bytearray(tx); bad[5] ^= 1
        p = Decoder().decode(bytes(bad), rx)
        self.assertIn('HEADER_CRC_BAD', p.errors)
        self.assertIn('REQUEST_INVALID', p.errors)
        self.assertIn('RESPONSE_MISSING_OR_TRUNCATED', Decoder().decode(tx).errors)
        self.assertIn('RX_LENGTH_MISMATCH', Decoder().decode(tx, rx[:-1]).errors)
        self.assertTrue(Decoder().decode(tx, bytes(len(tx))).errors)

    def test_short_coordinate_rule_does_not_apply_to_other_commands(self):
        _, rx = coordinates()
        for cmd in (0x5106, 0x5003):
            tx = request(cmd, 112) + bytes(110)
            p = Decoder().decode(tx, rx)
            self.assertTrue(p.errors)
            self.assertNotIn('response_padding_length', p.data)
        tx, rx = coordinates()
        for i in range(len(tx)):
            if i == 7:
                # A complete header alone is also a valid split-CS request.
                continue
            with self.subTest(length=i):
                self.assertTrue(Decoder().decode(tx[:i], rx[:i]).errors)

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
    def test_command_id_in_requests_responses_and_fields(self):
        for display in ('Transactions', 'Fields'):
            for cmd, expected, payload in ((0x510A, '17:10', None),
                                           (0x310A, '17:10', b'\x01')):
                with self.subTest(display=display, cmd=cmd):
                    h = load_hla(display=display)
                    out = feed(h, request(cmd, 1, payload))
                    self.assertTrue(all(r.data['CMDID'] == expected for r in out))
                    if display == 'Fields':
                        field = next(r for r in out if r.data['field'] == 'CMDID')
                        self.assertEqual(field.data['value'], expected)
                        self.assertEqual(field.data['summary'], 'CMDID=' + expected)
                    else:
                        self.assertIn('CMDID=' + expected, out[0].data['summary'])
                    rx = response(cmd, b'\x01' if payload is None else b'')
                    out = feed(h, b'\xff' * len(rx), rx)
                    self.assertTrue(all(r.data['CMDID'] == expected for r in out))
                    self.assertTrue(all(r.data['echo_command'] == expected for r in out))
                    if display == 'Fields':
                        field = next(r for r in out if r.data['field'] == 'ECHO_CMD')
                        self.assertEqual(field.data['value'], expected)
        out = feed(load_hla(), *prog())
        self.assertEqual(out[0].data['CMDID'], '0x070010')

    def test_coordinates_both_directions_and_displays(self):
        tx, rx = coordinates()
        for swap in (False, True):
            for display in ('Transactions', 'Fields'):
                with self.subTest(swap=swap, display=display):
                    h = load_hla(display=display)
                    for i in range(1, 3):
                        out = feed(h, tx, rx, swap)
                        self.assertEqual(len(out), 1)
                        self.assertEqual(out[0].type, 'TCS Request')
                        self.assertEqual(out[0].data['transaction'], str(i))
                        self.assertEqual(out[0].data['CMDID'], '17:05')
                        self.assertEqual(out[0].data['echo_command'], '17:05')
                        self.assertEqual(out[0].data['payload'], ' '.join(['00'] * 57))
                        self.assertNotIn('[error]', out[0].data['summary'])
                        self.assertNotIn('[warning]', out[0].data['summary'])
                    self.assertEqual(h.locked_direction, 'miso' if swap else 'mosi')

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
