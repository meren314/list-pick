$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
}
& .\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& .\.venv\Scripts\python.exe -m unittest discover -p 'test_*.py' -v
if ($LASTEXITCODE -ne 0) { throw 'Core tests failed.' }
& .\.venv\Scripts\python.exe validate_demo.py
if ($LASTEXITCODE -ne 0) { throw 'Demo acceptance failed.' }
& .\.venv\Scripts\python.exe app.py --smoke-test
if ($LASTEXITCODE -ne 0) { throw 'GUI startup failed.' }
& .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed --name '清单取件' --icon 'assets\list-pick.ico' --version-file 'version_info.txt' --add-data 'assets;assets' --add-data '演示资料;演示资料' app.py
if ($LASTEXITCODE -ne 0) { throw 'Executable build failed.' }
