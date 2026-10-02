"""Lokasi file & folder yang dipakai godex_bridge.

Semua path mengikuti Godex.exe asli supaya .ini dan log yang sudah ada tetap
terpakai tanpa perubahan:

    %ALLUSERSPROFILE%\\Godex\\Godex.ini
    %ALLUSERSPROFILE%\\Godex\\Logs\\Godex LOG YYYY-MM-DD.txt
"""

from __future__ import annotations

import os
from pathlib import Path

APP = "godex_bridge"
IS_WINDOWS = os.name == "nt"

ALLUSERSPROFILE = Path(os.environ.get("ALLUSERSPROFILE") or r"C:\ProgramData")
GODEX_DIR = ALLUSERSPROFILE / "Godex"
DEFAULT_INI = GODEX_DIR / "Godex.ini"
DEFAULT_LOG_DIR = GODEX_DIR / "Logs"

# Godex.ini di sebelah paket ini (dua level di atas godex/paths.py).
BUNDLED_INI = Path(__file__).resolve().parent.parent / "Godex.ini"


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
