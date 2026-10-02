"""Log: file harian + echo ke konsol + streaming ke client port Log.

Format mengikuti Godex.exe asli supaya pembaca log yang sudah ada tetap jalan:

    %ALLUSERSPROFILE%\\Godex\\Logs\\Godex LOG YYYY-MM-DD.txt
    baris: "HH:MM:SS pesan"   (tanpa tanggal di baris)
"""

from __future__ import annotations

import datetime as dt
import os
import socket
import threading
from pathlib import Path


def _ensure_dir(directory: Path) -> Path:
    """Buat folder log; kalau tidak bisa, jatuh ke %TEMP%\\GodexLogs."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        return directory
    except OSError:
        fallback = Path(os.environ.get("TEMP", ".")) / "GodexLogs"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


class Log:
    """Tulis log ke file, konsol, dan semua client yang sedang connect."""

    def __init__(self, directory: str | Path, debug: bool = False, echo: bool = True):
        self.dir = _ensure_dir(Path(directory))
        self.debug_on = debug
        self.echo = echo
        self._write_lock = threading.Lock()
        self._sub_lock = threading.Lock()
        self._subs: set[socket.socket] = set()

    def path(self) -> Path:
        return self.dir / f"Godex LOG {dt.date.today():%Y-%m-%d}.txt"

    # -- penulisan -----------------------------------------------------------

    def write(self, message: str) -> None:
        line = f"{dt.datetime.now():%H:%M:%S} {message}"
        with self._write_lock:
            try:
                with self.path().open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass  # log yang gagal ditulis tidak boleh menjatuhkan service
        if self.echo:
            print(line, flush=True)
        self._broadcast(line + "\n")

    def debug(self, message: str) -> None:
        if self.debug_on:
            self.write(message)

    def error(self, message: str) -> None:
        self.write("Error: " + message)

    # -- client port Log -----------------------------------------------------

    def attach(self, conn: socket.socket) -> None:
        """Replay isi log hari ini, lalu ikut streaming live.

        _sub_lock dipegang selama replay supaya baris yang ditulis bersamaan
        tidak hilang. Efek sampingnya: baris yang tepat jatuh di jendela itu
        bisa terkirim dua kali - jauh lebih baik daripada hilang.
        """
        with self._sub_lock:
            try:
                history = self.path().read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                history = ""
            except OSError:
                return
            if history:
                try:
                    conn.sendall(history.encode("utf-8", "replace"))
                except OSError:
                    return
            self._subs.add(conn)

    def detach(self, conn: socket.socket) -> None:
        with self._sub_lock:
            self._subs.discard(conn)

    def _broadcast(self, text: str) -> None:
        """Kirim satu baris ke semua client; buang yang sudah mati."""
        payload = text.encode("utf-8", "replace")
        with self._sub_lock:
            dead = []
            for conn in self._subs:
                try:
                    conn.sendall(payload)
                except OSError:
                    dead.append(conn)
            for conn in dead:
                self._subs.discard(conn)
                try:
                    conn.close()
                except OSError:
                    pass
