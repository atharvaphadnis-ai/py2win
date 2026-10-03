"""py2win - turn Python scripts and projects into Windows applications.

A small, local, telemetry-free CLI wrapper around PyInstaller (EXE) and
WiX Toolset v4+ (MSI). Everything runs on your machine.
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any
from xml.sax.saxutils import quoteattr

__version__ = "1.0.0"

CONFIG_FILE = "py2win.toml"
VENV_DIR = ".py2win-venv"
MARKER = ".py2win"  # written into build/ and dist/ so `clean` only removes our output
ENTRY_CANDIDATES = ("main.py", "app.py", "run.py", "cli.py", "__main__.py")
DEFAULTS: dict[str, Any] = {
    "name": None,
    "entry": None,
    "onefile": False,
    "windowed": False,
    "icon": None,
    "msi": False,
    "hidden_imports": [],
    "add_data": [],
    "version": "1.0.0",
    "manufacturer": None,
}

VERBOSE = False
LINE = "─" * 40


class Py2WinError(Exception):
    """A user-facing error. `title` is printed in caps, `body` underneath."""

    def __init__(self, title: str, body: str = "", code: int = 1) -> None:
        super().__init__(title)
        self.title, self.body, self.code = title, body, code


# ───────────────────────────── output helpers ─────────────────────────────

def _supports_color() -> bool:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return False
    if os.name == "nt":
        os.system("")  # enables ANSI escape processing on Windows 10+ consoles
    return True


COLOR = _supports_color()


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def ok(msg: str) -> None:
    print(f"{c('[OK]', '32')}   {msg}")


def warn(msg: str) -> None:
    print(f"{c('[WARN]', '33')} {msg}")


def fail(msg: str) -> None:
    print(f"{c('[FAIL]', '31')} {msg}")


def step(i: int, n: int, msg: str) -> None:
    print(f"[{i}/{n}] {msg}")


def header() -> None:
    print()
    print(c("py2win", "1"))
    print(LINE)
    print()


def human_size(path: Path) -> str:
    total = path.stat().st_size if path.is_file() else sum(
        f.stat().st_size for f in path.rglob("*") if f.is_file()
    )
    size = float(total)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


# ───────────────────────────── environment ─────────────────────────────

def project_python(root: Path) -> str:
    """Python used for builds: the project's build venv if present, else this one."""
    venv_py = root / VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(venv_py) if venv_py.exists() else sys.executable


def run(cmd: list[str], cwd: Path | None = None, capture: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a command safely (no shell). Streams output when --verbose."""
    if VERBOSE:
        print(c("$ " + subprocess.list2cmdline(cmd), "2"))
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=None if (VERBOSE or not capture) else subprocess.PIPE,
            stderr=subprocess.STDOUT if not VERBOSE and capture else None,
        )
    except FileNotFoundError as exc:
        raise Py2WinError("COMMAND NOT FOUND", f"Could not run: {cmd[0]}\n{exc}") from exc


def has_module(python: str, module: str) -> bool:
    r = subprocess.run([python, "-c", f"import {module}"], capture_output=True)
    return r.returncode == 0


def module_version(python: str, module: str) -> str:
    r = subprocess.run(
        [python, "-c", f"import {module};print(getattr({module},'__version__',''))"],
        capture_output=True, text=True,
    )
    return r.stdout.strip()


def find_wix() -> str | None:
    """Return path to WiX v4+ `wix` CLI if installed."""
    exe = shutil.which("wix")
    if exe:
        return exe
    home = Path.home() / ".dotnet" / "tools" / ("wix.exe" if os.name == "nt" else "wix")
    return str(home) if home.exists() else None


def find_wix3() -> bool:
    return bool(shutil.which("candle") and shutil.which("light"))


# ───────────────────────────── config ─────────────────────────────

def load_config(root: Path) -> dict[str, Any]:
    path = root / CONFIG_FILE
    if not path.exists():
        return {}
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:  # pragma: no cover
        try:
            import tomli as tomllib  # type: ignore
        except ModuleNotFoundError:
            raise Py2WinError(
                "CANNOT READ py2win.toml",
                "Python 3.11+ is needed to read py2win.toml, or install tomli:\n\n    pip install tomli",
            )
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise Py2WinError("INVALID py2win.toml", str(exc))
    # accept both hidden-imports / hidden_imports spellings
    return {k.replace("-", "_"): v for k, v in data.items()}


def merge_options(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    opts = dict(DEFAULTS)
    opts.update(load_config(root))
    # CLI overrides config
    if args.entry:
        opts["entry"] = args.entry
    if args.name:
        opts["name"] = args.name
    if args.icon:
        opts["icon"] = args.icon
    if args.onefile:
        opts["onefile"] = True
    if args.onedir:
        opts["onefile"] = False
    if args.windowed:
        opts["windowed"] = True
    if args.console:
        opts["windowed"] = False
    if args.msi:
        opts["msi"] = True
    if args.app_version:
        opts["version"] = args.app_version
    opts["hidden_imports"] = list(opts.get("hidden_imports") or []) + (args.hidden_import or [])
    opts["add_data"] = list(opts.get("add_data") or []) + (args.add_data or [])
    return opts


# ───────────────────────────── entry detection ─────────────────────────────

def detect_entry(root: Path, interactive: bool = True) -> Path:
    found = [root / n for n in ENTRY_CANDIDATES if (root / n).is_file()]
    if len(found) == 1:
        return found[0]
    if not found:
        py_files = sorted(p for p in root.glob("*.py") if p.name != "setup.py")
        withmain = [p for p in py_files if "__main__" in p.read_text("utf-8", "ignore")]
        found = withmain if withmain else py_files
        if len(found) == 1:
            return found[0]
    if not found:
        raise Py2WinError(
            "NO ENTRY FILE FOUND",
            "Could not find main.py, app.py, run.py or cli.py here.\n\n"
            "Tell py2win which file to start with:\n\n    py2win build --entry yourfile.py",
        )
    if not interactive or not sys.stdin.isatty():
        names = ", ".join(p.name for p in found)
        raise Py2WinError(
            "MULTIPLE ENTRY FILES FOUND",
            f"Candidates: {names}\n\nChoose one with:\n\n    py2win build --entry {found[0].name}",
        )
    print("Multiple possible entry files found:\n")
    for i, p in enumerate(found, 1):
        print(f"  {i}) {p.name}")
    while True:
        choice = input(f"\nSelect entry file [1-{len(found)}]: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(found):
            return found[int(choice) - 1]
        print("Please enter a number from the list.")


# ───────────────────────────── build ─────────────────────────────

def normalize_add_data(spec: str, root: Path) -> str:
    """Accept 'src;dest' (Windows) or 'src:dest' and validate src exists."""
    if ";" in spec:
        src, dest = spec.split(";", 1)
    elif re.match(r"^[A-Za-z]:[\\/]", spec) and spec.count(":") >= 2:
        src, dest = spec.rsplit(":", 1)
    elif ":" in spec and not re.match(r"^[A-Za-z]:[\\/]", spec):
        src, dest = spec.split(":", 1)
    else:
        src = spec
        dest = Path(spec).name if Path(spec).is_dir() else "."
    src_path = (root / src).resolve() if not Path(src).is_absolute() else Path(src)
    if not src_path.exists():
        raise Py2WinError("ASSET NOT FOUND", f"--add-data source does not exist:\n\n    {src}")
    return f"{src_path}{os.pathsep}{dest or '.'}"


def find_project_packages(root: Path) -> list[str]:
    """Top-level local modules/packages - passed as hidden imports so dynamic
    imports (importlib / __import__) of local code are still bundled."""
    names: list[str] = []
    skip = {VENV_DIR, "build", "dist", "venv", ".venv", "env", ".git", "__pycache__"}
    for p in root.iterdir():
        if p.name in skip or p.name.startswith("."):
            continue
        if p.is_file() and p.suffix == ".py" and p.stem.isidentifier():
            names.append(p.stem)
        elif p.is_dir() and (p / "__init__.py").exists() and p.name.isidentifier():
            names.append(p.name)
    return names


def explain_failure(output: str) -> Py2WinError:
    m = re.search(r"No module named '([\w.]+)'", output)
    if m:
        mod = m.group(1).split(".")[0]
        return Py2WinError(
            "BUILD FAILED",
            f"Missing dependency:\n{mod}\n\nInstall it with:\n\n    py2win install\n\nor:\n\n    pip install {mod}",
        )
    if "PermissionError" in output or "Access is denied" in output:
        return Py2WinError(
            "BUILD FAILED",
            "Windows denied access to an output file.\n"
            "Close the running .exe (or antivirus scan) and try again,\nor run:\n\n    py2win clean",
        )
    if re.search(r"icon", output, re.I) and re.search(r"error|not found|invalid", output, re.I):
        return Py2WinError("BUILD FAILED", "The icon could not be used. Use a valid .ico file (or install Pillow to convert .png).")
    tail = "\n".join(line for line in output.strip().splitlines()[-12:])
    return Py2WinError("BUILD FAILED", f"PyInstaller reported an error:\n\n{tail}\n\nRun again with --verbose for full output.")


def build(root: Path, opts: dict[str, Any], entry: Path) -> Path:
    python = project_python(root)
    name = opts["name"] or entry.stem
    if not re.fullmatch(r"[\w .\-]+", name):
        raise Py2WinError("INVALID NAME", f"'{name}' contains characters Windows cannot use in a file name.")
    total = 6 if opts["msi"] else 5

    header()
    print(f"Project: {name}")
    print(f"Entry:   {entry.relative_to(root) if entry.is_relative_to(root) else entry}")
    print(f"Mode:    {'One File' if opts['onefile'] else 'One Folder'}, {'Windowed' if opts['windowed'] else 'Console'}")
    print()

    step(1, total, "Checking Python...")
    if sys.platform != "win32":
        warn("Not running on Windows - PyInstaller builds for the current OS, so no .exe will be produced.")
    if sys.version_info < (3, 8):
        raise Py2WinError("UNSUPPORTED PYTHON", "py2win needs Python 3.8 or newer.")

    step(2, total, "Checking dependencies...")
    if not has_module(python, "PyInstaller"):
        raise Py2WinError(
            "PYINSTALLER NOT INSTALLED",
            "py2win uses PyInstaller to build executables.\n\nInstall it with:\n\n"
            "    py2win install\n\nor:\n\n    pip install pyinstaller",
        )

    step(3, total, "Analyzing project...")
    cmd = [
        python, "-m", "PyInstaller", str(entry),
        "--noconfirm", "--clean",
        "--name", name,
        "--distpath", str(root / "dist"),
        "--workpath", str(root / "build"),
        "--specpath", str(root / "build"),
        "--paths", str(root),
        "--paths", str(entry.parent),
        "--onefile" if opts["onefile"] else "--onedir",
        "--windowed" if opts["windowed"] else "--console",
    ]
    if not VERBOSE:
        cmd += ["--log-level", "WARN"]
    if opts["icon"]:
        icon = (root / opts["icon"]).resolve()
        if not icon.exists():
            raise Py2WinError("ICON NOT FOUND", f"Could not find icon file:\n\n    {opts['icon']}")
        cmd += ["--icon", str(icon)]
    hidden = set(opts["hidden_imports"]) | (set(find_project_packages(root)) - {entry.stem})
    for mod in sorted(hidden):
        cmd += ["--hidden-import", mod]
    for spec in opts["add_data"]:
        cmd += ["--add-data", normalize_add_data(spec, root)]

    step(4, total, "Building executable...")
    result = run(cmd, cwd=root)
    if result.returncode != 0:
        raise explain_failure(result.stdout or "")

    step(5, total, "Finalizing...")
    for d in ("build", "dist"):
        (root / d).mkdir(exist_ok=True)
        (root / d / MARKER).write_text("generated by py2win\n", encoding="utf-8")
    exe_name = name + (".exe" if sys.platform == "win32" else "")
    target = root / "dist" / exe_name if opts["onefile"] else root / "dist" / name / exe_name
    if not target.exists():
        raise Py2WinError("BUILD FAILED", f"PyInstaller finished but {target} was not created.\nRun with --verbose.")

    print()
    print(c("BUILD SUCCESSFUL", "1;32"))
    print(f"\nExecutable:\n{target.relative_to(root)}")
    print(f"\nSize:\n{human_size(target if opts['onefile'] else target.parent)}")

    if opts["msi"]:
        step(6, total, "Creating MSI installer...")
        build_msi(root, opts, name, target)
    print()
    return target


# ───────────────────────────── MSI (WiX v4+) ─────────────────────────────

def _wid(prefix: str, text: str) -> str:
    """Stable, valid WiX identifier (<=72 chars)."""
    return prefix + uuid.uuid5(uuid.NAMESPACE_URL, text).hex[:24]


def make_wxs(name: str, version: str, manufacturer: str, source_dir: Path, exe_name: str, icon: Path | None) -> str:
    upgrade = uuid.uuid5(uuid.NAMESPACE_DNS, f"py2win.{name}")
    lines: list[str] = []
    comp_refs: list[str] = []

    def walk(folder: Path, indent: str) -> None:
        for f in sorted(folder.iterdir()):
            rel = f.relative_to(source_dir).as_posix()
            if f.is_dir():
                lines.append(f'{indent}<Directory Id="{_wid("d", rel)}" Name={quoteattr(f.name)}>')
                walk(f, indent + "  ")
                lines.append(f"{indent}</Directory>")
            else:
                cid = _wid("c", rel)
                comp_refs.append(cid)
                lines.append(f'{indent}<Component Id="{cid}" Guid="{uuid.uuid5(upgrade, rel)}">')
                lines.append(f'{indent}  <File Id="{_wid("f", rel)}" Source={quoteattr(str(f))} KeyPath="yes" />')
                lines.append(f"{indent}</Component>")

    walk(source_dir, "        ")
    icon_xml = ""
    icon_attr = ""
    if icon:
        icon_xml = f'    <Icon Id="AppIcon.ico" SourceFile={quoteattr(str(icon))} />\n    <Property Id="ARPPRODUCTICON" Value="AppIcon.ico" />\n'
        icon_attr = ' Icon="AppIcon.ico"'
    exe_file_id = _wid("f", exe_name)
    refs = "\n".join(f'      <ComponentRef Id="{r}" />' for r in comp_refs)
    return f"""<Wix xmlns="http://wixtoolset.org/schemas/v4/wxs">
  <Package Name={quoteattr(name)} Manufacturer={quoteattr(manufacturer)} Version="{version}"
           UpgradeCode="{upgrade}" Scope="perMachine">
    <MajorUpgrade DowngradeErrorMessage="A newer version of {name} is already installed." />
    <MediaTemplate EmbedCab="yes" />
{icon_xml}    <StandardDirectory Id="ProgramFiles64Folder">
      <Directory Id="INSTALLFOLDER" Name={quoteattr(name)}>
{chr(10).join(lines)}
      </Directory>
    </StandardDirectory>
    <StandardDirectory Id="ProgramMenuFolder">
      <Component Id="StartMenuShortcut" Guid="{uuid.uuid5(upgrade, 'shortcut')}">
        <Shortcut Id="AppShortcut" Name={quoteattr(name)} Target="[#{exe_file_id}]" WorkingDirectory="INSTALLFOLDER"{icon_attr} />
        <RegistryValue Root="HKCU" Key="Software\\py2win\\{name}" Name="installed" Type="integer" Value="1" KeyPath="yes" />
      </Component>
    </StandardDirectory>
    <Feature Id="Main" Title={quoteattr(name)}>
{refs}
      <ComponentRef Id="StartMenuShortcut" />
    </Feature>
  </Package>
</Wix>
"""


def build_msi(root: Path, opts: dict[str, Any], name: str, exe: Path) -> Path | None:
    wix = find_wix()
    if not wix or sys.platform != "win32":
        print()
        print(c("MSI BUILD UNAVAILABLE", "1;33"))
        print(f"\npy2win successfully created:\n\n{exe.relative_to(root)}\n")
        print("To enable MSI generation, install WiX Toolset v4 or newer:\n")
        print("    dotnet tool install --global wix\n")
        print("and run:\n\n    py2win build --msi")
        return None
    version = str(opts.get("version") or "1.0.0")
    if not re.fullmatch(r"\d+(\.\d+){0,3}", version):
        raise Py2WinError("INVALID VERSION", f"MSI version must look like 1.0.0, got '{version}'.")
    manufacturer = opts.get("manufacturer") or name
    icon = (root / opts["icon"]).resolve() if opts.get("icon") else None

    # Stage files: one-file -> just the exe; one-folder -> the whole folder.
    if opts["onefile"]:
        stage = root / "build" / "msi-stage"
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True)
        shutil.copy2(exe, stage / exe.name)
    else:
        stage = exe.parent
    wxs = root / "build" / f"{name}.wxs"
    wxs.write_text(make_wxs(name, version, manufacturer, stage, exe.name, icon), encoding="utf-8")
    msi = root / "dist" / f"{name}-{version}.msi"
    result = run([wix, "build", str(wxs), "-arch", "x64", "-o", str(msi)], cwd=root)
    if result.returncode != 0:
        out = (result.stdout or "").strip().splitlines()
        raise Py2WinError(
            "MSI BUILD FAILED",
            f"The EXE was built ({exe.relative_to(root)}), but WiX failed:\n\n"
            + "\n".join(out[-10:]) + "\n\nRun with --verbose for details.",
        )
    print(c("\nMSI CREATED", "1;32"))
    print(f"\nInstaller:\n{msi.relative_to(root)}\n\nSize:\n{human_size(msi)}")
    return msi


# ───────────────────────────── commands ─────────────────────────────

def cmd_build(args: argparse.Namespace, script: str | None = None) -> int:
    root = Path.cwd().resolve()
    opts = merge_options(root, args)
    if script:
        opts["entry"] = script
    if opts["entry"]:
        entry = (root / opts["entry"]).resolve()
        if not entry.is_file():
            raise Py2WinError("FILE NOT FOUND", f"Could not find:\n\n    {opts['entry']}")
        if entry.suffix.lower() not in (".py", ".pyw"):
            raise Py2WinError("NOT A PYTHON FILE", f"{entry.name} is not a .py file.")
        if opts["entry"] and not args.windowed and not args.console and entry.suffix.lower() == ".pyw":
            opts["windowed"] = True
    else:
        entry = detect_entry(root)
    build(root, opts, entry)
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    root = Path.cwd().resolve()
    venv = root / VENV_DIR
    header()
    if not venv.exists():
        print(f"Creating build environment in {VENV_DIR}\\ ...")
        r = run([sys.executable, "-m", "venv", str(venv)])
        if r.returncode != 0:
            raise Py2WinError("COULD NOT CREATE VIRTUAL ENVIRONMENT", (r.stdout or "")[-800:])
    python = project_python(root)
    print("Upgrading pip...")
    run([python, "-m", "pip", "install", "--upgrade", "pip", "-q"])
    print("Installing PyInstaller...")
    r = run([python, "-m", "pip", "install", "pyinstaller", "-q"])
    if r.returncode != 0:
        raise Py2WinError("INSTALL FAILED", "Could not install PyInstaller:\n\n" + (r.stdout or "")[-800:])
    req = root / "requirements.txt"
    if req.exists():
        print("Installing requirements.txt...")
        r = run([python, "-m", "pip", "install", "-r", str(req)])
        if r.returncode != 0:
            raise Py2WinError("INSTALL FAILED", "pip could not install requirements.txt:\n\n" + (r.stdout or "")[-1200:])
    else:
        warn("No requirements.txt found - only PyInstaller was installed.")
    print()
    print(c("INSTALL SUCCESSFUL", "1;32"))
    print(f"\nBuild environment: {VENV_DIR}\\\npy2win will use it automatically for builds.\n")
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    root = Path.cwd().resolve()
    removed: list[str] = []
    for d in ("build", "dist"):
        p = root / d
        if p.is_dir():
            if (p / MARKER).exists():
                shutil.rmtree(p)
                removed.append(d + "\\")
            else:
                warn(f"Skipped {d}\\ (not created by py2win)")
    for spec in root.glob("*.spec"):
        # only remove specs PyInstaller generated (they start with the standard header)
        head = spec.read_text("utf-8", "ignore")[:200]
        if "-*- mode: python ; coding: utf-8 -*-" in head:
            spec.unlink()
            removed.append(spec.name)
    if args.all and (root / VENV_DIR).is_dir():
        shutil.rmtree(root / VENV_DIR)
        removed.append(VENV_DIR + "\\")
    if removed:
        for r in removed:
            ok(f"Removed {r}")
    else:
        print("Nothing to clean.")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    root = Path.cwd().resolve()
    python = project_python(root)
    problems = 0
    header()
    if sys.platform == "win32":
        ok(f"Windows detected ({platform.release()})")
    else:
        warn(f"Not Windows ({platform.system()}) - .exe files can only be built on Windows")
    v = sys.version_info
    if v >= (3, 8):
        ok(f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        fail(f"Python {v.major}.{v.minor} (3.8+ required)")
        problems += 1
    if python != sys.executable:
        ok(f"Build environment: {VENV_DIR}")
    if has_module(python, "pip"):
        ok("pip available")
    else:
        fail("pip not available")
        problems += 1
    if has_module(python, "PyInstaller"):
        ok(f"PyInstaller {module_version(python, 'PyInstaller')} available")
    else:
        fail("PyInstaller not installed  ->  py2win install")
        problems += 1
    if find_wix():
        ok("WiX Toolset (v4+) available - MSI builds enabled")
    elif find_wix3():
        warn("WiX v3 found, but py2win needs WiX v4+  ->  dotnet tool install --global wix")
    else:
        warn("WiX Toolset not installed (only needed for --msi)")
    ok(f"Current directory: {root}")
    if (root / CONFIG_FILE).exists():
        try:
            load_config(root)
            ok(f"{CONFIG_FILE} found")
        except Py2WinError as e:
            fail(f"{CONFIG_FILE}: {e.body}")
            problems += 1
    if (root / "requirements.txt").exists():
        ok("requirements.txt found")
    else:
        warn("No requirements.txt")
    entry = (load_config(root) if (root / CONFIG_FILE).exists() else {}).get("entry")
    if entry:
        (ok if (root / entry).is_file() else fail)(f"{entry} {'found' if (root / entry).is_file() else 'missing'}")
    else:
        found = [n for n in ENTRY_CANDIDATES if (root / n).is_file()]
        if len(found) == 1:
            ok(f"{found[0]} found")
        elif found:
            warn(f"Multiple entry candidates: {', '.join(found)} (use --entry)")
        else:
            warn("No main.py / app.py / run.py / cli.py found")
    print()
    return 1 if problems else 0


# ───────────────────────────── CLI ─────────────────────────────

def add_build_options(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("build options")
    g.add_argument("--entry", help="entry Python file (project mode)")
    g.add_argument("--name", help="application / executable name")
    g.add_argument("--icon", help="path to .ico icon")
    mode = g.add_mutually_exclusive_group()
    mode.add_argument("--onefile", action="store_true", help="single self-contained .exe")
    mode.add_argument("--onedir", action="store_true", help="folder with .exe + files (default)")
    win = g.add_mutually_exclusive_group()
    win.add_argument("--windowed", "--gui", action="store_true", help="GUI app, no console window")
    win.add_argument("--console", action="store_true", help="console app (default)")
    g.add_argument("--msi", action="store_true", help="also build an MSI installer (needs WiX v4+)")
    g.add_argument("--app-version", help="version used for the MSI (default 1.0.0)")
    g.add_argument("--hidden-import", action="append", metavar="MODULE", help="force-include a module (repeatable)")
    g.add_argument("--add-data", action="append", metavar="SRC;DEST", help='bundle files, e.g. "assets;assets" (repeatable)')


def make_parser() -> argparse.ArgumentParser:
    epilog = (
        "examples:\n"
        "  py2win app.py\n"
        "  py2win app.py --onefile --name MyApp\n"
        "  py2win app.py --windowed --icon icon.ico\n"
        '  py2win main.py --add-data "assets;assets"\n'
        "  py2win build --entry main.py --msi\n"
        "  py2win install | doctor | clean"
    )
    parser = argparse.ArgumentParser(
        prog="py2win",
        description="Turn Python scripts and projects into Windows applications (.exe / .msi).",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"py2win {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="show full build output and tracebacks")
    parser.add_argument("target", nargs="?", help="a .py file, or: build | install | clean | doctor")
    add_build_options(parser)
    parser.add_argument("--all", action="store_true", help="with clean: also remove the build environment")
    return parser


def main(argv: list[str] | None = None) -> int:
    global VERBOSE
    parser = make_parser()
    args = parser.parse_args(argv)
    VERBOSE = args.verbose
    try:
        target = args.target
        if target is None:
            parser.print_help()
            return 0
        if target == "build":
            return cmd_build(args)
        if target == "install":
            return cmd_install(args)
        if target == "clean":
            return cmd_clean(args)
        if target == "doctor":
            return cmd_doctor(args)
        if target.lower().endswith((".py", ".pyw")) or Path(target).is_file():
            return cmd_build(args, script=target)
        parser.error(f"unknown command or file: {target}")
        return 2
    except Py2WinError as e:
        print()
        print(c(e.title, "1;31"))
        if e.body:
            print()
            print(e.body)
        print()
        return e.code
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    except Exception as e:  # unexpected
        if VERBOSE:
            raise
        print(f"\n{c('UNEXPECTED ERROR', '1;31')}\n\n{type(e).__name__}: {e}\n\nRun again with --verbose for details.\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
