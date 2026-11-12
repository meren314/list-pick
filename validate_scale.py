"""Reproducible local scan timing using 10,000 synthetic empty filenames."""
import json
from pathlib import Path
import tempfile
import time
import core


def main():
    with tempfile.TemporaryDirectory(prefix='list-pick-scale-') as directory:
        root = Path(directory)
        for index in range(10000):
            folder = root / f'batch-{index // 1000}'
            folder.mkdir(exist_ok=True)
            (folder / f'invoice_{index:04d}.pdf').touch()
        started = time.perf_counter()
        patterns = '\n'.join(rf'^invoice_{index}[0-9]{{3}}\.pdf$' for index in range(10))
        result = core.scan(str(root), patterns, mode='regex', include_extensions='pdf')
        elapsed = time.perf_counter() - started
        assert result['complete']
        assert result['scanned'] == 10000
        assert len(result['rows']) == 10
        assert all(row['status'] == 'multiple' and len(row['candidates']) == 1000 for row in result['rows'])
        print(json.dumps({'synthetic_files': 10000, 'regex_rules': 10,
                          'matched_files': sum(len(row['candidates']) for row in result['rows']),
                          'scan_seconds_this_machine': round(elapsed, 3),
                          'copied': 0, 'scope': 'Local warm-cache scan only; not a cross-PC guarantee.'}))


if __name__ == '__main__':
    main()
