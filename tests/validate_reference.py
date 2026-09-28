r"""End-to-end replay and source-backed assertions; run from project root.

python -m tests.validate_reference --reference /path/to/reference
"""
import argparse
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest

from protocol import Decoder
from replay import csv_frames, decode_transactions, read_transactions
from tests.saleae_stub import load_hla


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def summarize(path, packets):
    return dict(source=str(path), sha256=sha(path), transactions=len(packets),
                kinds=dict(Counter(p.kind for p in packets)),
                statuses=dict(Counter(p.data['status'] for p in packets)),
                error_counts=dict(Counter(e.split(':')[0] for p in packets for e in p.errors)),
                warning_counts=dict(Counter(e for p in packets for e in p.warnings)),
                crc_counts=dict(Counter(k + ':' + v for p in packets for k, v in p.data.items()
                                        if k.endswith('_crc_status'))),
                special_counts=dict(Counter(p.data['special'] for p in packets if p.kind == 'SPECIAL')),
                anomalies=[dict(p.data) for p in packets if p.errors or p.warnings])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path('validation'))
    args = parser.parse_args()
    root, output = args.reference, args.output
    output.mkdir(exist_ok=True)
    cases = [
        ('analysis_output/仅上电-上位机未连接.json', 22),
        ('analysis_output/上位机-工程模式-数据采集.json', 38),
        ('analysis_output/产测数据.json', 1253),
        ('prog按钮.csv', 22),
        ('产测-多频点.csv', 1275),
    ]
    report = {'datasets': [], 'assertions': [], 'gui_verified': False}

    def check(condition, label):
        if not condition:
            raise AssertionError(label)
        report['assertions'].append(label)

    source_summary = json.loads((root/'analysis_output/summary.json').read_text(encoding='utf-8'))
    for relative, count in cases:
        path = root / relative
        packets = list(decode_transactions(read_transactions(path)))
        check(len(packets) == count, path.stem + ': transaction count')
        stats = summarize(path, packets)
        check(not any(p.kind == 'UNKNOWN' for p in packets), path.stem + ': all transactions classified')
        if path.suffix == '.json':
            original = root / (path.stem + '.kvdat')
            check(sha(original) == source_summary[path.stem]['sha256'], path.stem + ': raw capture SHA256 unchanged')
            stats['raw_capture_sha256'] = sha(original)
            expected_crc = source_summary[path.stem]['crc']
            for key, expected in [('header_crc', expected_crc['tcs_header_ok'] + expected_crc.get('prog_header_ok', 0)),
                                  ('data_crc', expected_crc.get('tcs_write_crc_ok', 0) + expected_crc.get('prog_data_ok', 0)),
                                  ('response_crc', expected_crc['tcs_response_crc_ok'] + expected_crc.get('tcs_data_crc_ok', 0))]:
                check(sum(p.data.get(key + '_status') == 'OK' for p in packets) == expected,
                      path.stem + ': ' + key + ' count matches reference')
            recovered = [p.data['transaction'] - 1 for p in packets if p.data.get('source_recovery')]
            check(recovered == source_summary[path.stem]['recovered'], path.stem + ': recovered records explicitly tracked')
            stats['reconstructed_source_indices_zero_based'] = recovered
            if path.stem == '产测数据':
                check(stats['error_counts'] == {'ECHO_MISMATCH': 15, 'ZERO_RESPONSE_NOT_ACK': 15,
                      'RX_LENGTH_MISMATCH': 3, 'REQUEST_INVALID': 3}, 'production: known zero ACKs and source reconstruction length discrepancies')
                check(stats['special_counts'] == {'ENTER_PROG': 2, 'DIE_ANALOG': 2, 'DIE_DIGITAL': 2},
                      'production: all three special sequences')
            else:
                check(stats['statuses'] == {'OK': count}, path.stem + ': no protocol anomalies')
        elif path.name == 'prog按钮.csv':
            check(stats['error_counts'] == {'RESPONSE_CRC_BAD': 9, 'ECHO_MISMATCH': 9}, 'PROG button: nine invalid ACKs detected')
            check(packets[2].data['payload'] == '00 03 00 00' and packets[9].data['payload'] == '02 02 00 00',
                  'PROG button: observed boot transition')
            check(packets[2].data['mode'] == 'BOOT_STATE_3' and packets[9].data['mode'] == 'PROG_CONFIRMED',
                  'PROG button: read opcode alone does not imply PROG mode')
        else:
            manifest = json.loads((root/'analysis_output/multifreq_lane_manifest.json').read_text(encoding='utf-8'))
            check(stats['sha256'] == manifest['sha256'], 'multifrequency: source SHA256 matches independent report')
            check(sum(p.data.get('header_crc_status') == 'OK' for p in packets) == 706,
                  'multifrequency: 675 TCS + 31 PROG valid headers')
            check([p.data['transaction'] - 1 for p in packets if 'DATA_CRC_BAD' in p.errors] == [138],
                  'multifrequency: PROG data CRC error at original index 138')
            check([p.data['transaction'] - 1 for p in packets if 'RESPONSE_CRC_BAD' in p.errors] == [1118],
                  'multifrequency: inline response CRC error at original index 1118')
            check(stats['error_counts'] == {'DATA_CRC_BAD': 1, 'ECHO_MISMATCH': 15,
                  'ZERO_RESPONSE_NOT_ACK': 15, 'RESPONSE_CRC_BAD': 1}, 'multifrequency: all known errors reproduced')
        # Replay the actual adapter byte by byte, not only the protocol core.
        if path.suffix == '.csv':
            for display in ('Transactions', 'Fields'):
                hla = load_hla(display=display)
                results = []
                for frame in csv_frames(path):
                    result = hla.decode(frame)
                    if result:
                        results.extend(result if isinstance(result, list) else [result])
                check(not hla.active, path.stem + ': ' + display + ' final CS closed')
                check(hla.locked_direction == 'miso', path.stem + ': ' + display + ' reversed direction detected')
                check(all(a.end_time <= b.start_time for a, b in zip(results, results[1:])),
                      path.stem + ': ' + display + ' timestamps do not overlap')
                if display == 'Transactions':
                    check(len(results) == count, path.stem + ': one HLA row per CS')
                    from HighLevelAnalyzer import display_data
                    check(all(result.data == display_data(packet)
                              for result, packet in zip(results, packets)), path.stem + ': HLA visible fields match core')
                stats['hla_' + display.lower() + '_rows'] = len(results)
        report['datasets'].append(stats)
        print(path.name, count, stats['statuses'])

    # Additional raw file is not part of the prior validated JSON corpus.
    # Use the existing capture loader, but retain original observed MOSI where
    # that script inferred a missing first bit. Never label repair as raw data.
    spec = importlib.util.spec_from_file_location('reference_spi', root/'analyze_spi.py')
    reference = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reference)
    path = root/'编程模式读取写入.kvdat'
    meta, rows = reference.decode(path)
    decoder = Decoder()
    packets = []
    for row in rows:
        tx = bytes.fromhex(row['observed_mosi'] if row['recovery'] else row['mosi'])
        packet = decoder.decode(tx, bytes.fromhex(row['miso']))
        packet.data['source_partial_bits'] = row['partial_bits']
        packets.append(packet)
    check(meta['transactions'] == 561 and meta['partial'] == 560, 'additional raw capture: 560/561 transactions contain partial bytes')
    check(all(p.data['status'] != 'OK' for p in packets), 'additional damaged raw capture: none mislabeled valid')
    report['damaged_raw_capture'] = summarize(path, packets)
    report['damaged_raw_capture']['loader_metadata'] = meta
    report['damaged_raw_capture']['loader_sha256'] = sha(root/'analyze_spi.py')
    report['damaged_raw_capture']['note'] = 'Reference loader glitch filtering retained; inferred MOSI replaced by observed bytes. Not a hardware acceptance dataset.'

    result = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.discover('tests', pattern='test_*.py'))
    check(result.wasSuccessful(), 'unit and robustness tests')
    report['unit_tests'] = result.testsRun
    report['result'] = 'PASS: reference assertions and robustness tests; GUI not verified'
    report['source_files'] = {p.name: sha(p) for p in Path('.').glob('*.py')}
    (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('PASS:',len(report['assertions']),'assertions;',result.testsRun,'unit tests')


if __name__ == '__main__':
    main()
