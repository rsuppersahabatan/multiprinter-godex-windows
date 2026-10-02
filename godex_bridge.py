#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""godex_bridge.py - print server TCP -> printer Windows (RAW/EZPL).

Ini entry point saja. Implementasinya ada di paket ``godex/``:

    godex/config.py     pembacaan Godex.ini (satu printer atau banyak printer)
    godex/logstream.py  log file + streaming ke client port Log
    godex/printing.py   kirim job RAW ke spooler Windows lewat ctypes
    godex/servers.py    listener Prn / Cmd / Log
    godex/app.py        CLI, wiring, self-test
    godex/service.py    mode Windows service (opsional, butuh pywin32)

Fungsi inti (identik dengan Godex.exe asli):
    Terima byte mentah di port TCP (default 9100) lalu kirim ke antrean printer
    Windows sebagai satu job RAW (datatype "RAW"). Ini yang dipakai driver
    Godex EZPL: aplikasi kirim perintah EZPL via TCP, spooler meneruskan ke
    printer.

Perilaku yang disamakan dengan Godex.exe asli (hasil verifikasi lapangan):
    * Config dibaca dari  %ALLUSERSPROFILE%\Godex\Godex.ini
    * Log ditulis ke     %ALLUSERSPROFILE%\Godex\Logs\Godex LOG YYYY-MM-DD.txt
    * Format baris log   "HH:MM:SS pesan"  (tanpa tanggal di baris)
    * Port Log           me-replay seluruh isi file log hari itu ke client yang
                         baru connect, lalu lanjut streaming live
    * Port Cmd           line-based, tanpa banner. Perintah yang dikenal:
                           QUIT  -> balas "Closing" lalu tutup koneksi
                           STOP  -> hentikan service
    * Port Prn           buffer sampai client disconnect, lalu cetak sebagai
                         SATU job RAW
    * Batas baris per job -> "Printer line limit (N) exceeded"

Format Godex.ini:
    Banyak printer (disarankan) - satu section per printer, satu port per printer:
        [Printer:<label>]   Name, Enabled, Debug, Addr, Port
    Satu printer (format Godex.exe asli, tetap didukung):
        [Printer]           Name, Enabled, Debug
        [Prn]               Addr, Port
    Selalu ada:
        [Cmd]               Addr, Port
        [Log]               Addr, Port

Dependensi: HANYA stdlib + ctypes. Tidak butuh pywin32.
Service Windows opsional (kalau pywin32 terpasang).
"""

from __future__ import annotations

import os
import sys

# Paket `godex/` harus tetap ketemu walau script dijalankan dari cwd lain -
# mis. oleh Service Control Manager yang cwd-nya C:\Windows\System32.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from godex.app import main  # noqa: E402
from godex.service import SERVICE_COMMANDS, SERVICE_MARKER, run_as_service  # noqa: E402


def _run(argv: list[str]) -> int:
    """Pilih jalur: kelola/host service Windows, atau jalan sebagai aplikasi."""
    if argv and (argv[0] in SERVICE_COMMANDS or SERVICE_MARKER in argv):
        return run_as_service()
    return main(argv)


if __name__ == "__main__":
    sys.exit(_run(sys.argv[1:]))
