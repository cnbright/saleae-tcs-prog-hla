"""Replay Saleae SPI CSV or transaction JSON without installing Saleae.

python replay.py input.csv --direction MISO --output decoded.json
"""
import argparse
import csv
import json
from pathlib import Path
from types import SimpleNamespace
from protocol import Decoder, direction_score


def csv_frames(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        required = {'type', 'start_time', 'duration', 'mosi', 'miso'}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError('Expected Saleae SPI data-table CSV columns: ' + ', '.join(sorted(required)))
        for line, row in enumerate(reader, 2):
            data = {}
            for key in ('mosi', 'miso'):
                text = row[key].strip()
                if text:
                    value = int(text, 16)
                    if not 0 <= value <= 255:
                        raise ValueError('Line {}: SPI must be 8-bit'.format(line))
                    data[key] = bytes([value])
            start = float(row['start_time'])
            yield SimpleNamespace(type=row['type'], start_time=start,
                                  end_time=start + float(row['duration']), data=data)


def csv_transactions(path):
    current = None
    for f in csv_frames(path):
        if f.type == 'enable':
            if current is not None:
                raise ValueError('Nested CS enable')
            current = {'start_s': f.start_time, 'mosi': [], 'miso': []}
        elif f.type == 'result':
            if current is None:
                raise ValueError('SPI word outside CS')
            for key in ('mosi', 'miso'):
                current[key].append(f.data.get(key))
        elif f.type == 'disable':
            if current is None:
                raise ValueError('CS disable without enable')
            for key in ('mosi', 'miso'):
                values = current[key]
                current[key] = b''.join(values) if all(v is not None for v in values) else None
            current['end_s'] = f.end_time
            if current['mosi'] or current['miso']:
                yield current
            current = None
        else:
            raise ValueError('SPI input error: ' + f.type)
    if current is not None:
        raise ValueError('Capture ended inside CS')


def read_transactions(path):
    path = Path(path)
    if path.suffix.lower() == '.csv':
        yield from csv_transactions(path)
    elif path.suffix.lower() == '.json':
        for row in json.loads(path.read_text(encoding='utf-8-sig')):
            yield dict(row, mosi=bytes.fromhex(row['mosi']), miso=bytes.fromhex(row['miso']))
    else:
        raise ValueError('Input must be a Saleae SPI CSV or transaction JSON')


def decode_transactions(rows, direction='Auto'):
    decoder = Decoder()
    host = None if direction == 'Auto' else direction.lower()
    for row in rows:
        if host is None:
            scores = {key: direction_score(row[key]) for key in ('mosi', 'miso')}
            best = max(scores, key=scores.get)
            if scores[best] < 5 or scores['mosi'] == scores['miso']:
                raise ValueError('Ambiguous direction; specify --direction MOSI or MISO')
            host = best
        packet = decoder.decode(row[host], row['miso' if host == 'mosi' else 'mosi'])
        packet.data.update(start_s=row['start_s'], end_s=row['end_s'], host_line=host.upper(), kind=packet.kind)
        if row.get('recovery'):
            packet.data['source_recovery'] = row['recovery']
        yield packet


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--direction', choices=['Auto', 'MOSI', 'MISO'], default='Auto')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    results = [p.data for p in decode_transactions(read_transactions(args.input), args.direction)]
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print('{} transactions, {} with errors'.format(len(results), sum(r['status'] == 'ERROR' for r in results)))


if __name__ == '__main__':
    main()
