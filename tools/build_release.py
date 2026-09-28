"""Build an installable extension ZIP containing only the public allowlist."""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = ('extension.json', 'HighLevelAnalyzer.py', 'protocol.py', 'command_names.py',
         'README.md', 'LICENSE', 'CHANGELOG.md', 'docs/PROTOCOL.md')


def main():
    version = json.loads((ROOT / 'extension.json').read_text(encoding='utf-8'))['version']
    destination = ROOT / 'dist'
    destination.mkdir(exist_ok=True)
    archive = destination / ('TCS_PROG_SPI_v' + version + '.zip')
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        for name in FILES:
            # Fixed timestamp and metadata make builds reproducible.
            entry = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            z.writestr(entry, (ROOT / name).read_bytes())
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None or set(z.namelist()) != set(FILES):
            raise RuntimeError('Release archive verification failed')
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    (destination / 'SHA256SUMS.txt').write_text(checksum + '  ' + archive.name + '\n', encoding='ascii')
    print(archive)
    print('SHA256:', checksum)


if __name__ == '__main__':
    main()
