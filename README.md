# py2win

**Turn any Python script or project into a Windows application — from one command.**

```bash
py2win app.py --onefile --name MyApp
```

py2win is a tiny, local, open-source CLI built on top of [PyInstaller](https://pyinstaller.org) (for `.exe`) and [WiX Toolset](https://wixtoolset.org) v4+ (for optional `.msi` installers). No cloud, no telemetry, no ads — your code never leaves your machine, and your program is never executed during the build.

## Features

- Single `.py` files, multi-file projects, local packages and imports
- Third-party dependencies and `requirements.txt` (isolated build environment)
- Console or GUI (`--windowed`) apps
- One-file `.exe` or one-folder distribution
- Optional `.msi` installer with Start-menu shortcut
- Custom name, icon and version
- Bundle assets with `--add-data`
- `py2win.toml` project config (CLI flags override it)
- `doctor`, `install`, `clean` commands
- Beginner-friendly errors; `--verbose` for full output

Supported: **Windows 10/11, Python 3.8 – 3.13** (64-bit recommended).

## Installation

```bash
git clone https://github.com/atharvaphadnis-ai/py2win.git
cd py2win
pip install .
```

Then:

```bash
py2win --help
py2win doctor
```

## Quick start

```bash
cd my-project
py2win install      # optional: creates .py2win-venv with PyInstaller + requirements.txt
py2win main.py      # -> dist\main\main.exe
```

## Single-file example

```bash
py2win hello.py --onefile
```
```text
dist\hello.exe
```

## Multi-file example

```text
myapp/
├── main.py          # import config; from utils import helper; from database import Database
├── database.py
├── utils.py
├── config.py
├── requirements.txt
└── assets/logo.png
```
```bash
cd myapp
py2win install
py2win main.py --add-data "assets;assets"
```

Local modules and packages are detected automatically — no need to merge files. Or let py2win find the entry point (`main.py`, `app.py`, `run.py`, `cli.py`):

```bash
py2win build
py2win build --entry main.py
```

## GUI example

```bash
py2win app.py --windowed --icon icon.ico
```

No console window will appear. `.pyw` files are windowed automatically.

## One-file vs one-folder

| Command | Output | Notes |
|---|---|---|
| `py2win app.py` / `--onedir` | `dist\app\app.exe` + files | Starts faster (default) |
| `py2win app.py --onefile` | `dist\app.exe` | Single file, easy to share |

## MSI example

Install WiX v4+ once (requires the .NET SDK):

```bash
dotnet tool install --global wix
```

Then:

```bash
py2win app.py --onefile --name MyApp --msi --app-version 1.2.0
```
```text
dist\MyApp.exe
dist\MyApp-1.2.0.msi
```

If WiX is missing, the EXE still builds and py2win tells you how to enable MSI.

## Configuration

Optional `py2win.toml` in your project folder:

```toml
name = "MyApp"
entry = "main.py"
onefile = true
windowed = false
icon = "icon.ico"
msi = false
version = "1.0.0"
manufacturer = "My Company"
hidden_imports = ["pkg_resources"]
add_data = ["assets;assets", "config.json;."]
```

Command-line flags always win: `py2win build --onedir` overrides `onefile = true`.

## Asset bundling

```bash
py2win app.py --add-data "assets;assets" --add-data "config.json;."
```

At runtime, locate bundled files like this (works in both dev and frozen builds):

```python
import sys, os
BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
logo = os.path.join(BASE, "assets", "logo.png")
```

## Commands

| Command | What it does |
|---|---|
| `py2win app.py` | Build an executable from `app.py` |
| `py2win build` | Build the project (auto-detect entry) |
| `py2win install` | Create `.py2win-venv` and install PyInstaller + `requirements.txt` |
| `py2win doctor` | Check Windows, Python, pip, PyInstaller, WiX, entry file |
| `py2win clean` | Remove py2win's `build\`, `dist\` and generated `.spec` (`--all` also removes `.py2win-venv`) |

Flags: `--name --icon --onefile --onedir --windowed --console --msi --app-version --hidden-import --add-data --entry --verbose --version`

## Troubleshooting

- **Missing dependency `xyz`** → `py2win install` (or `pip install xyz`).
- **Module not found only at runtime** (dynamic imports, plugins) → `--hidden-import xyz`.
- **File not found at runtime** → bundle it with `--add-data` and use `sys._MEIPASS` (see above).
- **Access is denied** → close the running `.exe`, then `py2win clean`.
- **Antivirus flags the exe** → common false positive for one-file builds; try `--onedir`.
- **Anything else** → `py2win doctor`, then rerun with `--verbose`.

## Development

```bash
git clone https://github.com/YOUR_USERNAME/py2win.git
cd py2win
pip install -e .
py2win --version
```

Everything lives in `py2win.py` (standard library only, plus PyInstaller at build time). PRs welcome.

## License

MIT — see [LICENSE](LICENSE).
