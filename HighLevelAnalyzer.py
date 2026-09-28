"""Saleae Logic 2 adapter: configure SPI as 8 bits, with CS enabled."""
from saleae.analyzers import HighLevelAnalyzer, AnalyzerFrame, ChoicesSetting
from protocol import Decoder, direction_score, SPECIAL_LABELS


def display_type(packet):
    return {'TCS_REQUEST': 'TCS Request', 'TCS_INLINE': 'TCS Request',
            'TCS_RESPONSE': 'TCS Replay', 'PROG': 'PROG'}.get(packet.kind, 'Special')


def display_data(packet):
    """Only user-facing fields go to Logic; diagnostics stay in the decoder."""
    keys = ('summary', 'transaction', 'rw', 'payload',
            'request_transaction', 'echo_command', 'special')
    visible = {key: packet.data[key] for key in keys if key in packet.data}
    identity = packet.data.get('address' if packet.kind == 'PROG' else 'command')
    if identity is not None:
        visible['CMDID'] = identity
    # Text is independent of Logic's global number radix (often hexadecimal).
    for key in ('transaction', 'request_transaction'):
        if key in visible:
            visible[key] = str(visible[key])
    if 'special' in visible:
        visible['special'] = SPECIAL_LABELS[visible['special']]
    elif packet.kind == 'UNKNOWN':
        visible['special'] = 'Unknown SPI sequence'
    return visible


class Hla(HighLevelAnalyzer):
    direction = ChoicesSetting(['Auto', 'MOSI', 'MISO'], label='Host request line')
    display = ChoicesSetting(['Transactions', 'Fields'], label='Display')
    result_types = {name: {'format': '{{data.summary}}'}
                    for name in ('TCS Request', 'TCS Replay', 'PROG', 'Special')}
    MAX_BYTES = 1048576

    def __init__(self):
        self.decoder = Decoder()
        self.words = []
        self.active = False
        self.bad = False
        self.locked_direction = None
        self.reported_no_cs = False

    @staticmethod
    def _error(frame, message):
        return AnalyzerFrame('Special', frame.start_time, frame.end_time,
                             {'summary': 'ERROR: ' + message, 'special': 'SPI input error'})

    def decode(self, frame):
        if frame.type == 'enable':
            result = self._error(frame, 'Missing CS disable; previous transaction discarded') if self.active else None
            if self.active:
                self.decoder.invalidate()
            self.active, self.bad, self.words = True, False, []
            return result
        if frame.type == 'error':
            self.bad = True
            self.decoder.invalidate()
            return self._error(frame, 'SPI low-level error; check CPOL/CPHA and capture integrity')
        if frame.type == 'result':
            if not self.active:
                if not self.reported_no_cs:
                    self.reported_no_cs = True
                    return self._error(frame, 'No CS enable: select the SPI Enable channel; partial transaction ignored')
                return None
            if self.bad:
                return None
            for key in ('mosi', 'miso'):
                value = frame.data.get(key)
                if value is not None and (not isinstance(value, bytes) or len(value) != 1):
                    self.bad = True
                    self.decoder.invalidate()
                    return self._error(frame, 'SPI must use 8 bits per transfer')
            if len(self.words) >= self.MAX_BYTES:
                self.bad = True
                self.words = []
                self.decoder.invalidate()
                return self._error(frame, 'Transaction exceeds 1 MiB limit')
            self.words.append(frame)
            return None
        if frame.type != 'disable':
            return None
        if not self.active:
            return None
        self.active = False
        words, self.words = self.words, []
        if not words or self.bad:
            return None
        streams = {}
        for key in ('mosi', 'miso'):
            values = [w.data.get(key) for w in words]
            streams[key] = b''.join(values) if all(v is not None for v in values) else None
        requested = self.direction if isinstance(self.direction, str) else 'Auto'
        if requested == 'Auto':
            if self.locked_direction is None:
                scores = {k: direction_score(v) for k, v in streams.items()}
                best = max(scores, key=scores.get)
                other = 'miso' if best == 'mosi' else 'mosi'
                # Lock only on CRC-verified headers or an exact special transaction.
                if scores[best] >= 5 and scores[best] > scores[other]:
                    self.locked_direction = best
                else:
                    return self._error(frame, 'Direction unknown/ambiguous; choose MOSI or MISO explicitly')
            host = self.locked_direction
        else:
            host = requested.lower()
        tx, rx = streams[host], streams['miso' if host == 'mosi' else 'mosi']
        if tx is None:
            self.decoder.invalidate()
            return self._error(frame, 'Host request channel is missing')
        packet = self.decoder.decode(tx, rx)
        packet.data['host_line'] = host.upper()
        visible = display_data(packet)
        if self.display == 'Fields' and packet.fields and not packet.data.get('inline'):
            result = []
            for first, stop, name, value in sorted(packet.fields):
                if name in ('LEN', 'ErrCode') or 'CRC' in name:
                    continue
                if name == 'CMD':
                    value = packet.data['command'].removeprefix('0x')
                if 0 <= first < stop <= len(words):
                    details = dict(visible, field=name, value=value, summary=name + '=' + value)
                    result.append(AnalyzerFrame(display_type(packet), words[first].start_time, words[stop - 1].end_time, details))
            if packet.errors or packet.warnings:
                details = dict(visible, summary='Invalid transaction' if packet.errors else 'Unverified transaction')
                result.append(AnalyzerFrame(display_type(packet), frame.start_time, frame.end_time, details))
            return result or None
        # Inline polling is full duplex: use one row to avoid overlapping time bubbles.
        return AnalyzerFrame(display_type(packet), words[0].start_time, words[-1].end_time, visible)
