"""Mode Windows service (opsional - butuh pywin32).

Kontrak CLI-nya sama dengan versi sebelumnya::

    python godex_bridge.py install --startup auto
    python godex_bridge.py remove

Kalau pywin32 tidak terpasang, jalankan sebagai aplikasi biasa atau bungkus
dengan NSSM - lihat skill ``windows-raw-print-bridge``.

Catatan penting: nama service default "Godex" sama dengan nama service yang
dipasang Godex.exe asli. Kalau keduanya ada di satu mesin, ganti lewat
variabel lingkungan ``GODEX_SERVICE_NAME`` sebelum menjalankan ``install``.
"""

from __future__ import annotations

import os
import sys
import threading

from .paths import is_frozen

SERVICE_NAME = os.environ.get("GODEX_SERVICE_NAME", "Godex")

SERVICE_COMMANDS = frozenset(
    {"install", "remove", "start", "stop", "restart", "update"}
)

#: Argumen yang ditanam ke ImagePath service. Saat Service Control Manager
#: menjalankan service, argv berisi penanda ini - dari situ kita tahu proses
#: ini harus jalan sebagai service, bukan sebagai aplikasi konsol.
SERVICE_MARKER = "--service"


def run_as_service(argv: list[str] | None = None) -> int:
    """Pasang/kelola service, atau host service kalau dijalankan oleh SCM."""
    try:
        import servicemanager
        import win32service
        import win32serviceutil
    except ImportError:
        print(
            "pywin32 belum terpasang. Jalankan sebagai aplikasi biasa, atau:\n"
            "  pip install pywin32\n"
            "  python godex_bridge.py install --startup auto\n"
            "Alternatif tanpa menyentuh Python di jalur service: bungkus dengan NSSM.",
            file=sys.stderr,
        )
        return 1

    from .app import main

    class BridgeService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = f"{SERVICE_NAME} Print Bridge"
        _svc_description_ = "TCP to Windows printer bridge (port Python dari Godex.exe)."
        _exe_args_ = SERVICE_MARKER
        # Saat dijalankan sebagai .exe, sys.executable sudah exe-nya sendiri.
        # Tanpa ini pywin32 menulis "python.exe script.py" ke ImagePath, padahal
        # di mode frozen tidak ada script .py sama sekali.
        _exe_name_ = sys.executable if is_frozen() else None

        def __init__(self, args):
            super().__init__(args)
            self._stop = threading.Event()

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            self._stop.set()

        def SvcDoRun(self):
            servicemanager.LogInfoMsg(f"{SERVICE_NAME} service started")
            main([], stop_event=self._stop)
            servicemanager.LogInfoMsg(f"{SERVICE_NAME} service stopped")

    argv = list(sys.argv if argv is None else argv)
    if len(argv) > 1 and argv[1] in SERVICE_COMMANDS:
        # install / remove / start / stop / restart / update
        win32serviceutil.HandleCommandLine(BridgeService, argv=argv)
    else:
        # Dijalankan SCM: paksa pywin32 masuk ke jalur "host as service",
        # supaya SvcStop benar-benar dipanggil saat service dihentikan.
        win32serviceutil.HandleCommandLine(BridgeService, argv=[argv[0]])
    return 0
