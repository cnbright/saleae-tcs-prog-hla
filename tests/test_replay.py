import csv
from pathlib import Path
import tempfile
import unittest

from replay import csv_frames, decode_transactions, read_transactions
from tests.saleae_stub import load_hla
from tests.test_protocol import coordinates


class ReplayTests(unittest.TestCase):
    def write_capture(self, path, include_hla=False, include_name=True, error=False):
        tx, rx = coordinates()
        columns = ['type', 'start_time', 'duration', 'mosi', 'miso', 'summary']
        if include_name:
            columns.insert(0, 'name')
        with path.open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, columns, extrasaction='ignore')
            writer.writeheader()
            for transaction in range(2):
                start = transaction * 200
                writer.writerow(dict(name='SPI', type='enable', start_time=start, duration=0))
                for i, (a, b) in enumerate(zip(tx, rx), 1):
                    writer.writerow(dict(name='SPI', type='result', start_time=start+i,
                                         duration=0.5, mosi=hex(a), miso=hex(b)))
                    if include_hla and i == 1:
                        # The old decoder's stale error row must not be replayed.
                        writer.writerow(dict(name='TCS & PROG', type='TCS Request',
                                             start_time=start+i, duration=117,
                                             summary='TCS Request CMD=5105 read 112 bytes [error]'))
                if error:
                    writer.writerow(dict(name='SPI', type='error', start_time=start+118, duration=0))
                writer.writerow(dict(name='SPI', type='disable', start_time=start+120, duration=0))

    def test_spi_only_and_combined_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.csv'
            for include_hla, include_name in ((False, False), (False, True), (True, True)):
                with self.subTest(hla=include_hla, name=include_name):
                    self.write_capture(path, include_hla, include_name)
                    packets = list(decode_transactions(read_transactions(path)))
                    self.assertEqual(len(packets), 2)
                    self.assertTrue(all(p.data['status'] == 'OK' for p in packets))
                    for display in ('Transactions', 'Fields'):
                        hla = load_hla(display=display)
                        results = []
                        for frame in csv_frames(path):
                            result = hla.decode(frame)
                            if result:
                                results.extend(result if isinstance(result, list) else [result])
                        self.assertEqual(len(results), 2)
                        self.assertFalse(hla.active)
                        self.assertLessEqual(results[0].end_time, results[1].start_time)
                        self.assertTrue(all('[error]' not in r.data['summary'] for r in results))

    def test_combined_export_retains_spi_input_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.csv'
            self.write_capture(path, include_hla=True, error=True)
            with self.assertRaisesRegex(ValueError, 'SPI input error: error'):
                list(read_transactions(path))


if __name__ == '__main__':
    unittest.main()
