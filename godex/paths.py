"""Lokasi file & folder yang dipakai godex_bridge.

Semua path mengikuti Godex.exe asli supaya .ini dan log yang sudah ada tetap
terpakai tanpa perubahan:

    %ALLUSERSPROFILE%\\Godex\\Godex.ini
    %ALLUSERSPROFILE%\\Godex\\Logs\\Godex LOG YYYY-MM-DD.txt
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP = "godex_bridge"
IS_WINDOWS = os.name == "nt"

ALLUSERSPROFILE = Path(os.environ.get("ALLUSERSPROFILE") or r"C:\ProgramData")
GODEX_DIR = ALLUSERSPROFILE / "Godex"
DEFAULT_INI = GODEX_DIR / "Godex.ini"
DEFAULT_LOG_DIR = GODEX_DIR / "Logs"


def is_frozen() -> bool:
    """True kalau dijalankan sebagai .exe hasil PyInstaller."""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Folder tempat aplikasi berada (bukan folder paket).

    Saat dibungkus PyInstaller, ``__file__`` menunjuk ke folder ekstraksi
    sementara (_MEIPASS) yang dibersihkan begitu proses keluar - jadi Godex.ini
    di situ tidak ada gunanya. Yang benar: folder tempat .exe berada.
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    # godex/paths.py -> naik dua level = root proyek
    return Path(__file__).resolve().parent.parent


#: Godex.ini yang ikut didistribusikan, di sebelah script / .exe.
BUNDLED_INI = app_dir() / "Godex.ini"


def resolve_ini(path: str | None) -> Path:
    """Tentukan file .ini yang dipakai.

    Urutan: path dari CLI -> %ALLUSERSPROFILE%\\Godex\\Godex.ini (kalau ada)
    -> Godex.ini yang ikut di folder aplikasi.
    """
    if path:
        return Path(path).expanduser().resolve()
    if DEFAULT_INI.is_file():
        return DEFAULT_INI
    return BUNDLED_INI
