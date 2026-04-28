$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot
python -m pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --onefile --windowed --name Roblox-Account-Manager-Python ram_py.py
