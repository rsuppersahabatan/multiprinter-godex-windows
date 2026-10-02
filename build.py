"""Build `godex_bridge.exe` - satu file, tanpa perlu Python di mesin target.

    python build.py

Hasilnya:

    dist/godex_bridge.exe   aplikasi jadi (satu file)
    dist/Godex.ini          contoh konfigurasi, disalin ke sebelah .exe

Catatan: butuh `pyinstaller` dan (untuk mode service) `pywin32` di lingkungan
build. pywin32 sengaja ikut dibundel supaya `godex_bridge.exe install` jalan
tanpa Python di mesin target.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
WORK = ROOT / "build"

ENTRY = ROOT / "godex_bridge.py"
BUNDLED_INI = ROOT / "Godex.ini"

# pywin32 diimpor di dalam fungsi (jalur mode service), jadi PyInstaller tidak
# selalu menemukannya lewat analisis statis.
HIDDEN_IMPORTS = [
    "win32serviceutil",
    "win32service",
    "servicemanager",
    "win32timezone",
    "pywintypes",
]

# Tidak dipakai aplikasi ini; membuangnya memangkas ukuran .exe.
EXCLUDES = [
    "tkinter",
    "win32com",
    "win32comext",
    "setuptools",
    "pip",
    "numpy",
    "PIL",
    "matplotlib",
    "pytest",
]


def build_args() -> list[str]:
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--noupx",
        "--onefile", "--console",
        "--name", "godex_bridge",
        "--distpath", str(DIST),
        "--workpath", str(WORK),
        "--specpath", str(WORK),
    ]
    for module in HIDDEN_IMPORTS:
        args += ["--hidden-import", module]
    for module in EXCLUDES:
        args += ["--exclude-module", module]
    args.append(str(ENTRY))
    return args


def main() -> int:
    if not ENTRY.is_file():
        print(f"entry point tidak ditemukan: {ENTRY}", file=sys.stderr)
        return 1

    if importlib.util.find_spec("PyInstaller") is None:
        print(
            "pyinstaller belum terpasang. Jalankan:\n"
            "  python -m pip install pyinstaller pywin32",
            file=sys.stderr,
        )
        return 1

    print("Membangun godex_bridge.exe ...", flush=True)
    result = subprocess.run(build_args(), cwd=str(ROOT))
    if result.returncode != 0:
        print(f"build gagal (exit {result.returncode})", file=sys.stderr)
        return result.returncode

    exe = DIST / "godex_bridge.exe"
    if not exe.is_file():
        print(f"build selesai tapi {exe} tidak ada", file=sys.stderr)
        return 1

    # Godex.ini harus berada DI SEBELAH .exe, bukan di dalam bundel: file itu
    # yang diedit pengguna, dan isinya harus terbaca saat runtime.
    shutil.copy2(BUNDLED_INI, DIST / "Godex.ini")

    size_mb = exe.stat().st_size / (1024 * 1024)
    print(f"\nSelesai: {exe}  ({size_mb:.1f} MB)")
    print(f"         {DIST / 'Godex.ini'}  (contoh konfigurasi)")
    print("\nLangkah berikutnya:")
    print(f"  1. {exe.name} --list-printers     cari nama printer yang benar")
    print(f"  2. sunting {DIST / 'Godex.ini'}")
    print(f"  3. {exe.name} --self-test         pastikan config + printer + port siap")
    print(f"  4. {exe.name}                     jalankan")
    return 0


if __name__ == "__main__":
    sys.exit(main())
