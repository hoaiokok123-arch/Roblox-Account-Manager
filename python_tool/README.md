# Roblox Account Manager Python

Python desktop tool based on this repository's Roblox Account Manager workflow.

## Features

- Add/import/export Roblox accounts from `.ROBLOSECURITY` cookies.
- Validate accounts through Roblox account JSON.
- Launch selected accounts into a place, job ID, VIP/private-server link, or follow-user target.
- Launch multiple accounts with a configurable delay.
- Recent and favorite game lists.
- Roblox process watcher with:
  - auto rejoin after Roblox exits,
  - wait for internet before rejoining,
  - optional lost-connection timeout kill,
  - optional low-memory restart rule.

Account cookies are stored locally in `python_tool/data/accounts.json`. That folder is ignored by git.

## Run From Source

```powershell
cd python_tool
python -m pip install -r requirements.txt
python ram_py.py
```

## Build EXE Locally

```powershell
cd python_tool
python -m pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --onefile --windowed --name Roblox-Account-Manager-Python ram_py.py
```

The executable will be created in `python_tool/dist/`.
