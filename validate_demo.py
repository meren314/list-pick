"""Independent acceptance against the shipped, fictional sample files."""
from pathlib import Path
from collections import Counter
import csv
import hashlib
import json
import tempfile
from core import scan, export_result


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    base = Path(__file__).resolve().parent
    source = base / '演示资料' / '源文件'
    text = (base / '演示资料' / '清单.txt').read_text(encoding='utf-8-sig')
    originals = {str(p.relative_to(source)): digest(p) for p in source.rglob('*') if p.is_file()}
    result = scan(str(source), text)
    assert result['complete'] is True
    assert result['scanned'] == 6
    counts = Counter(r['status'] for r in result['rows'])
    assert counts == {'unique': 3, 'missing': 1, 'ambiguous': 1, 'duplicate': 1}, counts
    with tempfile.TemporaryDirectory(prefix='list-pick-acceptance-') as temporary:
        exported = export_result(result, temporary)
        assert exported['copied'] == 3 and exported['failed'] == 0
        assert not exported['cancelled'] and not exported['report_error']
        output = Path(exported['output_dir'])
        expected = ['报价/001-报价.txt', '合同/002-合同.txt', '编号/0007-编号.txt']
        for relative in expected:
            assert digest(output / relative) == digest(source / relative), relative
        copied_files = sorted(str(p.relative_to(output)).replace('\\', '/') for p in output.rglob('*.txt'))
        assert copied_files == sorted(expected), copied_files
        with open(exported['report_path'], encoding='utf-8-sig', newline='') as stream:
            report = list(csv.DictReader(stream))
        assert len(report) == 7, report
        assert {str(p.relative_to(source)): digest(p) for p in source.rglob('*') if p.is_file()} == originals
    print(json.dumps({'sample_files': 6, 'input_rows': 6, 'classification': dict(counts),
                      'copied': 3, 'failed': 0, 'source_hashes_unchanged': True,
                      'copied_hashes_match': True, 'csv_records': 7}, ensure_ascii=False))


if __name__ == '__main__':
    main()
