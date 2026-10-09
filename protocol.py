"""ICNT9268S SPI protocol; independent of the Saleae runtime.

Byte offsets and CRC rules are described in docs/PROTOCOL.md. No damaged bytes are repaired.
"""
from dataclasses import dataclass, field
from command_names import COMMAND_NAMES


def crc16(data):
    value = 0
    for byte in data:
        value ^= byte << 8
        for _ in range(8):
            value = ((value << 1) ^ (0x8005 if value & 0x8000 else 0)) & 0xFFFF
    return value


def hexstr(data):
    return data.hex(' ').upper() if data is not None else '(not captured)'


def tcs_command_id(command):
    """Decimal class:command; class occupies bits 8..12, without RW flags."""
    return '{}:{:02d}'.format((command >> 8) & 0x1F, command & 0xFF)


SPECIALS = {
    b'\xcc\x33\x55\x5a': 'ENTER_PROG',
    b'\x81': 'DIE_DIGITAL',
    b'\x82': 'DIE_ANALOG',
    b'\x83': 'DIE_EXTENDED',
}

SPECIAL_LABELS = {
    'ENTER_PROG': 'Enter PROG mode (CC 33 55 5A)',
    'DIE_DIGITAL': 'Switch to digital DIE (0x81)',
    'DIE_ANALOG': 'Switch to analog DIE (0x82)',
    'DIE_EXTENDED': 'Switch to extended DIE (0x83)',
}

# The firmware preloads a 57-byte compressed coordinate report for 0x5105.
# Hosts may still clock the uncompressed report size; unused RX clocks are 7F.
COMPRESSED_COORDINATE_LENGTH = 57


def direction_score(data):
    if not data:
        return 0
    if data in SPECIALS:
        return 5
    if len(data) >= 7 and data[0] in (0xF0, 0xF1):
        return 10 if crc16(data[:5]) == int.from_bytes(data[5:7], 'little') else 1
    if len(data) >= 9 and data[0] in (0x60, 0x61):
        return 10 if crc16(data[:7]) ^ 0xFFFF == int.from_bytes(data[7:9], 'big') else 1
    return 0


@dataclass
class Packet:
    kind: str
    data: dict = field(default_factory=dict)
    # (inclusive start byte, exclusive stop byte, field name, display value)
    fields: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def add(self, start, stop, name, value):
        if stop > start:
            self.fields.append((start, stop, name, str(value)))

    def finish(self):
        self.data['status'] = 'ERROR' if self.errors else 'WARNING' if self.warnings else 'OK'
        self.data['errors'] = '; '.join(self.errors)
        self.data['warnings'] = '; '.join(self.warnings)
        payload = self.data.get('payload', '')
        self.data['payload_preview'] = payload if len(payload) <= 95 else payload[:95] + ' ...'
        cmd = self.data.get('command_id', '??:??')
        rw = self.data.get('rw', '').lower()
        count = self.data.get('length', '?')
        preview = self.data['payload_preview']
        if self.kind in ('TCS_REQUEST', 'TCS_INLINE'):
            count = self.data.get('requested_length', count)
            text = 'TCS Request CMDID={} {} {} bytes'.format(cmd, rw, count)
            if self.kind == 'TCS_INLINE':
                replay = 'TCS Replay'
                if self.data.get('length') != count:
                    replay += ' {} bytes'.format(self.data['length'])
                text += ' / ' + replay + ' data = ' + preview
            elif payload:
                text += ' data = ' + preview
        elif self.kind == 'TCS_RESPONSE':
            text = 'TCS Replay data = ' + preview if payload else 'TCS Replay ACK'
        elif self.kind == 'PROG':
            text = 'PROG {} {} {} bytes'.format(
                rw, self.data.get('address', '0x??????'), count)
            if payload:
                text += ' = ' + preview
        elif self.kind == 'SPECIAL':
            text = SPECIAL_LABELS[self.data['special']]
        else:
            text = 'Unknown SPI sequence'
        if self.errors:
            text += ' [error]'
        elif self.warnings:
            text += ' [warning]'
        self.data['summary'] = text
        return self


class Decoder:
    def __init__(self):
        self.pending = None
        self.die = 'UNKNOWN'
        self.mode = 'UNKNOWN'
        self.sequence = 0

    def invalidate(self):
        self.pending = None
        self.die = self.mode = 'UNKNOWN'

    def decode(self, tx, rx=None):
        """Decode exactly one complete CS transaction, in physical byte order."""
        self.sequence += 1
        p = Packet('UNKNOWN', {'transaction': self.sequence, 'tx': hexstr(tx),
                              'rx': hexstr(rx), 'die': self.die, 'mode': self.mode})
        if not tx:
            p.errors.append('EMPTY_TX')
            return p.finish()
        if rx is not None and len(rx) != len(tx):
            # Do not silently shift or pad a captured response.
            p.errors.append('RX_LENGTH_MISMATCH')
            rx = None
        pending, self.pending = self.pending, None
        if all(b == 0xFF for b in tx):
            p.kind = 'TCS_RESPONSE'
            self._response(p, rx, pending)
            return p.finish()
        if pending:
            p.warnings.append('MISSING_RESPONSE_TO_{}'.format(pending['transaction']))
        if tx in SPECIALS:
            p.kind = 'SPECIAL'
            p.data['special'] = SPECIALS[tx]
            if tx == b'\x81':
                self.die = 'DIGITAL'
            elif tx == b'\x82':
                self.die = 'ANALOG'
            elif tx == b'\x83':
                self.die = 'EXTENDED'
            else:
                self.mode = 'PROG_REQUESTED'
            p.data.update(die=self.die, mode=self.mode)
            p.add(0, len(tx), 'SPECIAL', SPECIAL_LABELS[SPECIALS[tx]])
        elif tx[0] in (0xF0, 0xF1):
            self._tcs(p, tx, rx)
        elif tx[0] in (0x60, 0x61):
            self._prog(p, tx, rx)
        else:
            p.warnings.append('UNKNOWN_SEQUENCE')
        return p.finish()

    @staticmethod
    def _crc(p, label, data, offset, payload, endian, xorout=0):
        if data is None or len(data) < offset + 2:
            p.data[label + '_status'] = 'MISSING'
            p.errors.append(label.upper() + '_MISSING')
            return False
        actual = int.from_bytes(data[offset:offset + 2], endian)
        expected = crc16(payload) ^ xorout
        ok = actual == expected
        p.data.update({label: '0x{:04X}'.format(actual), label + '_expected': '0x{:04X}'.format(expected),
                       label + '_status': 'OK' if ok else 'BAD'})
        if not ok:
            p.errors.append(label.upper() + '_BAD')
        p.add(offset, offset + 2, label.upper(), '0x{:04X} {}'.format(actual, 'OK' if ok else 'BAD'))
        return ok

    def _tcs(self, p, tx, rx):
        p.kind = 'TCS_REQUEST'
        read = tx[0] == 0xF1
        p.data.update(rw='READ' if read else 'WRITE', opcode='0x{:02X}'.format(tx[0]))
        if len(tx) < 7:
            p.errors.append('TCS_HEADER_TRUNCATED')
            return
        cmd = int.from_bytes(tx[1:3], 'little')
        n = int.from_bytes(tx[3:5], 'little')
        key = cmd & 0x1FFF
        p.data.update(command='0x{:04X}'.format(cmd), command_id=tcs_command_id(cmd),
                      cmd_id='0x{:02X}'.format(tx[1]),
                      class_id='0x{:02X}'.format(tx[2]), logical_class='0x{:02X}'.format(tx[2] & 31),
                      read_flag=(tx[2] >> 6) & 1, write_flag=(tx[2] >> 5) & 1, length=n,
                      command_name=COMMAND_NAMES.get(key, 'Unknown/reserved'))
        p.add(0, 1, 'RW', p.data['rw'])
        p.add(1, 3, 'CMDID', p.data['command_id'] + ' ' + p.data['command_name'])
        p.add(3, 5, 'LEN', n)
        self._crc(p, 'header_crc', tx, 5, tx[:5], 'little')
        if tx[2] & 0xE0 != (0x40 if read else 0x20):
            p.errors.append('RW_FLAG_MISMATCH')
        # Keep header-only coordinate reads compatible with split-CS replies.
        # Interrupt coordinate reads, like polling reads, return from RX byte 0.
        inline = read and (key == 0x1106 or (key == 0x1105 and len(tx) > 7))
        expected = n + 5 if inline else 7 if read else n + 9
        p.data['expected_transaction_length'] = expected
        if len(tx) != expected:
            p.errors.append('TCS_LENGTH_MISMATCH:{}!={}'.format(len(tx), expected))
        if not read:
            payload = tx[7:min(7 + n, len(tx))]
            p.data['payload'] = hexstr(payload)
            p.add(7, min(7 + n, len(tx)), 'DATA', hexstr(payload))
            self._crc(p, 'data_crc', tx, 7 + n, payload, 'little')
        req = dict(transaction=self.sequence, cmd=cmd, length=n, read=read,
                   valid=not p.errors, die=self.die)
        if inline:
            p.kind = 'TCS_INLINE'
            p.data['inline'] = True
            response_length = None
            short_end = COMPRESSED_COORDINATE_LENGTH + 5
            if (key == 0x1105 and n > COMPRESSED_COORDINATE_LENGTH
                    and rx is not None and len(rx) > short_end
                    and all(byte == 0x7F for byte in rx[short_end:])):
                # Use the firmware's fixed boundary, never rstrip or search for
                # an embedded echo/CRC. Validate the complete short trailer below.
                response_length = COMPRESSED_COORDINATE_LENGTH
                p.data.update(response_padding=hexstr(rx[short_end:]),
                              response_padding_length=len(rx) - short_end)
                rx = rx[:short_end]
            self._response(p, rx, req, response_length)
        else:
            self.pending = req

    def _response(self, p, rx, req, response_length=None):
        if req:
            p.data.update(request_transaction=req['transaction'], command='0x{:04X}'.format(req['cmd']),
                          command_id=tcs_command_id(req['cmd']),
                          command_name=COMMAND_NAMES.get(req['cmd'] & 0x1FFF, 'Unknown/reserved'),
                          rw='READ' if req['read'] else 'WRITE', requested_length=req['length'],
                          cmd_id='0x{:02X}'.format(req['cmd'] & 255),
                          class_id='0x{:02X}'.format(req['cmd'] >> 8),
                          logical_class='0x{:02X}'.format((req['cmd'] >> 8) & 31))
            if not req['valid']:
                p.errors.append('REQUEST_INVALID')
        else:
            p.errors.append('ORPHAN_RESPONSE')
        if rx is None or len(rx) < 5:
            p.errors.append('RESPONSE_MISSING_OR_TRUNCATED')
            return
        n = len(rx) - 5
        echo = int.from_bytes(rx[-4:-2], 'little')
        p.data.update(length=n, payload=hexstr(rx[:n]), err_code='0x{:02X}'.format(rx[-5]),
                      echo_command='0x{:04X}'.format(echo), echo_command_id=tcs_command_id(echo),
                      echo_matches=bool(req and echo == req['cmd']))
        p.add(0, n, 'DATA', hexstr(rx[:n]))
        p.add(n, n + 1, 'ErrCode', p.data['err_code'])
        p.add(n + 1, n + 3, 'ECHO_CMD', p.data['echo_command_id'])
        self._crc(p, 'response_crc', rx, len(rx) - 2, rx[:-2], 'little')
        if req:
            expected = req['length'] if req['read'] else 0
            if response_length is not None:
                expected = response_length
            if n != expected:
                p.errors.append('RESPONSE_LENGTH_MISMATCH:{}!={}'.format(n, expected))
            if echo != req['cmd']:
                p.errors.append('ECHO_MISMATCH')
        if rx[-5]:
            p.errors.append('DEVICE_ERRCODE')
        if not any(rx):
            p.errors.append('ZERO_RESPONSE_NOT_ACK')

    def _prog(self, p, tx, rx):
        p.kind = 'PROG'
        read = tx[0] == 0x61
        p.data.update(rw='READ' if read else 'WRITE', opcode='0x{:02X}'.format(tx[0]))
        if len(tx) < 9:
            p.errors.append('PROG_HEADER_TRUNCATED')
            return
        address = int.from_bytes(tx[1:4], 'big')
        n = int.from_bytes(tx[4:7], 'big')
        p.data.update(address='0x{:06X}'.format(address), length=n, expected_transaction_length=n + 18)
        p.add(0, 1, 'RW', p.data['rw'])
        p.add(1, 4, 'ADDR', p.data['address'])
        p.add(4, 7, 'LEN', n)
        self._crc(p, 'header_crc', tx, 7, tx[:7], 'big', 0xFFFF)
        if len(tx) != n + 18:
            p.errors.append('PROG_LENGTH_MISMATCH:{}!={}'.format(len(tx), n + 18))
        p.add(9, min(13, len(tx)), 'DUMMY', hexstr(tx[9:13]))
        stream = rx if read else tx
        if stream is None:
            p.errors.append('MISO_NOT_CAPTURED')
            return
        payload = stream[13:13 + n]
        p.data['payload'] = hexstr(payload)
        p.add(13, min(13 + n, len(stream)), 'DATA', hexstr(payload))
        self._crc(p, 'data_crc', stream, 13 + n, payload, 'big', 0xFFFF)
        tail = rx[15 + n:18 + n] if rx is not None else None
        p.data['tail'] = hexstr(tail)
        if tail is None or len(tail) != 3:
            p.errors.append('ACK_MISSING')
            p.data['ack'] = 'MISSING'
        elif 0xE0 in tail[-2:]:
            p.errors.append('ACK_E0')
            p.data['ack'] = 'ERROR'
        elif tail[-2:] == b'\x0a\x0a':
            p.data['ack'] = 'OK'
        else:
            # Host code only rejects E0; retain other values without inventing semantics.
            p.data['ack'] = 'UNRECOGNIZED'
            p.warnings.append('ACK_UNRECOGNIZED')
        p.add(15 + n, min(18 + n, len(tx)), 'TAIL_ACK', p.data['tail'] + ' ' + p.data['ack'])
        if read and address == 0x070010 and n >= 2 and not p.errors and p.data['ack'] == 'OK':
            p.data.update(boot_mode=payload[0] & 7, boot_state=payload[1] & 7)
            self.mode = 'PROG_CONFIRMED' if payload[1] & 7 == 2 else 'BOOT_STATE_{}'.format(payload[1] & 7)
            p.data['mode'] = self.mode
        elif not read and address == 0x070008 and payload == b'\xfe' and not p.errors:
            self.mode = 'RESET_REQUESTED'
            p.data['mode'] = self.mode
