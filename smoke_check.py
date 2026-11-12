"""Check bundled resources and native regex loading using only synthetic files."""
import hashlib
from pathlib import Path
import tempfile
import core


def run(resource_root):
    resources = Path(resource_root)
    for relative in ('assets/list-pick.png', 'assets/list-pick.ico', '演示资料/清单.txt'):
        if not (resources / relative).is_file():
            raise RuntimeError('打包资源缺失：' + relative)
    source = resources / '演示资料' / '源文件'
    if not source.is_dir():
        raise RuntimeError('演示源文件夹缺失。')
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest()
              for path in source.rglob('*') if path.is_file()}
    if len(before) != 6:
        raise RuntimeError('虚构演示的文件数量异常。')
    result = core.scan(str(source), r'\.txt$', mode='regex')
    suffix = core.scan(str(source), '*.txt', mode='extension')
    if not result['complete'] or not suffix['complete']:
        raise RuntimeError('打包后的模式扫描未完成。')
    candidates = result['rows'][0]['candidates']
    if len(candidates) != 6 or candidates != suffix['rows'][0]['candidates']:
        raise RuntimeError('打包后的正则/后缀结果不一致。')
    result['rows'][0].update(status='selected', selected_candidates=candidates[:2])
    with tempfile.TemporaryDirectory(prefix='list-pick-bundle-check-') as temporary:
        exported = core.export_result(result, temporary)
        if (exported['copied'], exported['failed'], exported['report_error']) != (2, 0, ''):
            raise RuntimeError('打包后的复制/报告验证失败。')
        output = Path(exported['output_dir'])
        for candidate in candidates[:2]:
            path = Path(candidate)
            copied = output / path.relative_to(source)
            if hashlib.sha256(copied.read_bytes()).hexdigest() != before[path]:
                raise RuntimeError('打包后的复制内容不一致。')
    after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before}
    if before != after:
        raise RuntimeError('演示原文件发生变化。')
