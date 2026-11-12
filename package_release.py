"""Package only the reviewed deliverables, excluding local build environments."""
from pathlib import Path
import argparse
import hashlib
import json
import shutil
import zipfile

BASE = Path(__file__).resolve().parent
NAME = '清单取件-0.2.1-Windows试验版'


def write_zip(folder, destination):
    with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(folder.rglob('*')):
            if file.is_file():
                archive.write(file, file.relative_to(folder.parent))
    with zipfile.ZipFile(destination) as archive:
        assert archive.testzip() is None
        return len(archive.namelist())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, default=BASE / 'release')
    args = parser.parse_args()
    outputs = args.output_dir.resolve()
    package = outputs / NAME
    package.mkdir(parents=True, exist_ok=False)
    shutil.copy2(BASE / 'dist' / '清单取件.exe', package / '清单取件.exe')
    shutil.copy2(BASE / '使用说明.txt', package / '使用说明.txt')
    shutil.copy2(BASE / 'LICENSE', package / 'LICENSE')
    shutil.copy2(BASE / 'README.md', package / 'README.md')
    shutil.copytree(BASE / '演示资料', package / '演示资料')
    shutil.copytree(BASE / 'third-party-licenses', package / '第三方许可')
    manifest = []
    for file in sorted(package.rglob('*')):
        if file.is_file():
            sha = hashlib.sha256(file.read_bytes()).hexdigest()
            manifest.append(f'{sha}  {file.relative_to(package).as_posix()}')
    (package / 'SHA256SUMS.txt').write_text('\n'.join(manifest) + '\n', encoding='utf-8')
    zip_path = outputs / (NAME + '.zip')
    file_count = write_zip(package, zip_path)

    source_zip = outputs / '清单取件-0.2.1-源码.zip'
    files = ['app.py', 'core.py', 'list_io.py', 'smoke_check.py', 'test_core.py', 'test_product_flow.py', 'test_list_io.py', 'test_ui_polish.py', 'validate_demo.py', 'validate_scale.py',
             'build.ps1', 'requirements-build.txt', '使用说明.txt', '.gitignore',
             'package_release.py', '开发说明.txt', 'build_assets.py', 'version_info.txt', 'README.md', 'LICENSE']
    with zipfile.ZipFile(source_zip, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name in files:
            archive.write(BASE / name, 'list-pick/' + name)
        for directory in ['演示资料', 'third-party-licenses', 'assets']:
            for file in sorted((BASE / directory).rglob('*')):
                if file.is_file():
                    archive.write(file, 'list-pick/' + file.relative_to(BASE).as_posix())
    with zipfile.ZipFile(source_zip) as archive:
        assert archive.testzip() is None
    print(json.dumps({'package': str(zip_path), 'package_files': file_count,
                      'package_bytes': zip_path.stat().st_size,
                      'exe_bytes': (package / '清单取件.exe').stat().st_size,
                      'source': str(source_zip), 'source_bytes': source_zip.stat().st_size},
                     ensure_ascii=True))


if __name__ == '__main__':
    main()
