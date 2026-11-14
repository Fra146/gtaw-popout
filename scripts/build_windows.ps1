$ErrorActionPreference = "Stop"

python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller
python -m PyInstaller --clean --noconfirm build-windows.spec

Write-Host "Built: dist\GTAW-Tactical-Launcher.exe"
