"""Listener TCP: Prn (data EZPL), Cmd (kontrol), Log (streaming log).

Port Prn adalah jalur data utama: aplikasi pengirim menembak byte EZPL ke port
itu, dan satu koneksi = satu job cetak. Karena satu host bisa punya lebih dari
satu printer, tiap printer punya listener Prn-nya sendiri - jadi aplikasi cukup
mengganti port untuk memilih printer.
"""

from __future__ import annotations

import json
import socket
import socketserver
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .config import Config, Endpoint, PrinterConfig
from .logstream import Log
from .paths import IS_WINDOWS
from .printing import RawPrinter

RECV_CHUNK = 64 * 1024
CMD_RECV_CHUNK = 4096
DRAIN_CHUNK = 1024

#: Jeda polling loop utama sebelum cek ulang stop_event.
POLL_INTERVAL_S = 0.5


@dataclass
class PrinterStats:
    jobs_ok: int = 0
    jobs_failed: int = 0
    bytes_in: int = 0


class State:
    """Kondisi bersama semua listener: log, statistik, dan saklar enable."""

    def __init__(self, config: Config, log: Log, stop_event: threading.Event):
        self.config = config
        self.log = log
        self.stop_event = stop_event
        self.started_at = time.time()
        self.master_enabled = True
        self._stats: dict[str, PrinterStats] = {
            printer.label: PrinterStats() for printer in config.printers
        }
        self._lock = threading.Lock()

    # -- printer -------------------------------------------------------------

    def printer(self, label: str) -> PrinterConfig | None:
        for printer in self.config.printers:
            if printer.label == label:
                return printer
        return None

    def accepts_jobs(self, label: str) -> bool:
        """Job diterima hanya kalau saklar utama DAN printer-nya aktif.

        Label yang tidak dikenal berarti ada salah wiring (mis. label listener
        dipakai padahal yang diminta label printer). Itu bukan kondisi normal,
        jadi dibikin berisik - kalau diam-diam dianggap "disabled", semua job
        hilang tanpa jejak.
        """
        printer = self.printer(label)
        if printer is None:
            self.log.error(
                f"label printer tidak dikenal: {label!r} - job dibuang "
                f"(label yang ada: {', '.join(p.label for p in self.config.printers)})"
            )
            return False
        return self.master_enabled and printer.enabled

    def set_master_enabled(self, value: bool) -> None:
        self.master_enabled = value

    def set_printer_enabled(self, label: str, value: bool) -> bool:
        """Return False kalau label tidak dikenal."""
        printer = self.printer(label)
        if printer is None:
            return False
        printer.enabled = value
        return True

    # -- statistik -----------------------------------------------------------

    def record(self, label: str, ok: bool, nbytes: int) -> None:
        with self._lock:
            stats = self._stats.setdefault(label, PrinterStats())
            if ok:
                stats.jobs_ok += 1
            else:
                stats.jobs_failed += 1
            stats.bytes_in += nbytes

    def snapshot(self) -> dict:
        """Ringkasan untuk perintah STATUS."""
        with self._lock:
            printers = []
            for printer in self.config.printers:
                stats = self._stats.get(printer.label, PrinterStats())
                printers.append({
                    "label": printer.label,
                    "name": printer.name or "(printer default Windows)",
                    "prn": str(printer.endpoint),
                    "enabled": printer.enabled and self.master_enabled,
                    "jobs_ok": stats.jobs_ok,
                    "jobs_failed": stats.jobs_failed,
                    "bytes_in": stats.bytes_in,
                })
            return {
                "enabled": self.master_enabled,
                "uptime_s": int(time.time() - self.started_at),
                "line_limit": self.config.line_limit,
                "printers": printers,
            }


class BridgeServer(socketserver.ThreadingTCPServer):
    """TCP server yang membawa konteks bersama + printer yang dilayaninya."""

    daemon_threads = True
    # Di Windows SO_REUSEADDR membolehkan proses LAIN ikut bind ke port yang
    # sudah dipakai - dua instance bisa sama-sama "berhasil" dan job tercampur
    # tanpa error apa pun. Karena itu di Windows kita pakai SO_EXCLUSIVEADDRUSE
    # (lihat server_bind), supaya instance kedua gagal keras.
    allow_reuse_address = not IS_WINDOWS

    def __init__(self, endpoint: Endpoint, handler, *, state: State,
                 printer: RawPrinter | None = None, label: str = ""):
        self.state = state
        self.printer = printer
        self.label = label or str(endpoint)
        super().__init__((endpoint.addr, endpoint.port), handler, bind_and_activate=False)
        try:
            self.server_bind()
            self.server_activate()
        except BaseException:
            # Jangan tinggalkan socket setengah jadi kalau bind/activate gagal.
            self.server_close()
            raise

    def server_bind(self) -> None:
        if IS_WINDOWS:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _close_quietly(sock: socket.socket) -> None:
    try:
        sock.close()
    except OSError:
        pass


@dataclass
class _Job:
    data: bytes
    total_bytes: int
    line_count: int
    exceeded: bool


class PrnHandler(socketserver.BaseRequestHandler):
    """Terima byte mentah sampai client menutup koneksi, lalu cetak 1 job RAW.

    Satu koneksi = satu job. Jangan cetak per baris: hasilnya satu job spooler
    per baris label.
    """

    def handle(self) -> None:
        state: State = self.server.state
        label = self.server.label
        peer = self.client_address[0]
        state.log.write(f"[{label}] Client connected from {peer}")

        try:
            job = self._read_job(state)
        finally:
            _close_quietly(self.request)

        state.log.debug(
            f"[{label}] EZPL received: {len(job.data)} byte dari {peer} "
            f"({job.line_count} baris)"
        )
        self._deliver(state, job)
        state.log.write(f"[{label}] Client disconnected from {peer}")

    def _read_job(self, state: State) -> _Job:
        """Baca sampai client tutup koneksi, idle-timeout, atau kena line limit."""
        config = state.config
        label = self.server.label
        self.request.settimeout(config.idle_flush or None)

        chunks: list[bytes] = []
        total = lines = 0
        exceeded = False

        while True:
            try:
                chunk = self.request.recv(RECV_CHUNK)
            except socket.timeout:
                if chunks:
                    break  # sudah ada data dan client diam -> job dianggap selesai
                continue
            except OSError as exc:
                state.log.write(f"[{label}] Client exception occurred: {exc}")
                break

            if not chunk:
                break

            chunks.append(chunk)
            total += len(chunk)
            lines += chunk.count(b"\n")
            if config.line_limit and lines > config.line_limit:
                exceeded = True
                state.log.write(f"[{label}] Printer line limit ({config.line_limit}) exceeded")
                break

        return _Job(b"".join(chunks), total, lines, exceeded)

    def _deliver(self, state: State, job: _Job) -> None:
        label = self.server.label

        if job.exceeded:
            state.record(label, ok=False, nbytes=job.total_bytes)
            return

        if not state.accepts_jobs(label):
            state.log.write(f"[{label}] Printer disabled - job dibuang")
            state.record(label, ok=False, nbytes=job.total_bytes)
            return

        try:
            ok = self.server.printer.send(job.data)
        except Exception as exc:  # noqa: BLE001 - satu job rusak jangan matikan listener
            state.log.write(f"[{label}] Printer exception occurred: {exc}")
            ok = False

        state.record(label, ok=ok, nbytes=job.total_bytes)
        state.log.write(f"[{label}] job {'OK' if ok else 'GAGAL'} ({len(job.data)} byte)")


class LogHandler(socketserver.BaseRequestHandler):
    """Replay log hari ini, lalu streaming live."""

    def handle(self) -> None:
        state: State = self.server.state
        state.log.attach(self.request)
        try:
            while self.request.recv(DRAIN_CHUNK):
                pass
        except OSError:
            pass
        finally:
            state.log.detach(self.request)


# --------------------------------------------------------------------------
# Kanal kontrol
#
# QUIT dan STOP meniru Godex.exe asli. Sisanya tambahan untuk diagnostik dan
# tidak ada di versi asli.
# --------------------------------------------------------------------------

#: Fungsi perintah: (handler, state, argumen) -> True kalau koneksi ditutup.
CommandFunc = Callable[["CmdHandler", State, str], bool]


def _cmd_quit(handler: "CmdHandler", state: State, argument: str) -> bool:
    handler.reply("Closing")
    return True


def _cmd_stop(handler: "CmdHandler", state: State, argument: str) -> bool:
    handler.reply("Stopping")
    state.log.write("Stopping godex_bridge")
    state.stop_event.set()
    return True


def _cmd_status(handler: "CmdHandler", state: State, argument: str) -> bool:
    handler.reply(json.dumps(state.snapshot(), indent=2))
    return False


def _cmd_enable(handler: "CmdHandler", state: State, argument: str) -> bool:
    return _set_enabled(handler, state, argument, True)


def _cmd_disable(handler: "CmdHandler", state: State, argument: str) -> bool:
    return _set_enabled(handler, state, argument, False)


def _set_enabled(handler: "CmdHandler", state: State, label: str, value: bool) -> bool:
    """Tanpa argumen: saklar utama. Dengan argumen: satu printer saja."""
    if not label:
        state.set_master_enabled(value)
        handler.reply(f"enabled={int(value)}")
    elif state.set_printer_enabled(label, value):
        handler.reply(f"{label}: enabled={int(value)}")
    else:
        handler.reply(f"printer tidak dikenal: {label}")
    return False


def _cmd_printer(handler: "CmdHandler", state: State, argument: str) -> bool:
    for printer in state.config.printers:
        handler.reply(
            f"{printer.label}: name={printer.name or '(default)'} "
            f"prn={printer.endpoint} enabled={int(printer.enabled)}"
        )
    return False


def _cmd_help(handler: "CmdHandler", state: State, argument: str) -> bool:
    handler.reply(" | ".join(sorted(COMMANDS)))
    handler.reply("ENABLE/DISABLE tanpa argumen = semua printer; "
                  "dengan argumen = satu printer, mis. 'DISABLE GodexRT730'")
    return False


COMMANDS: dict[str, CommandFunc] = {
    "QUIT": _cmd_quit,
    "STOP": _cmd_stop,
    "STATUS": _cmd_status,
    "ENABLE": _cmd_enable,
    "DISABLE": _cmd_disable,
    "PRINTER": _cmd_printer,
    "HELP": _cmd_help,
}


class CmdHandler(socketserver.BaseRequestHandler):
    """Kanal kontrol line-based, tanpa banner."""

    def handle(self) -> None:
        state: State = self.server.state
        buffer = b""
        try:
            while True:
                chunk = self.request.recv(CMD_RECV_CHUNK)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    raw_line, buffer = buffer.split(b"\n", 1)
                    line = raw_line.decode("utf-8", "replace").strip()
                    if line and self.dispatch(state, line):
                        return
        except OSError:
            pass

    def dispatch(self, state: State, line: str) -> bool:
        """Jalankan satu perintah. Return True kalau koneksi harus ditutup."""
        word, _, argument = line.partition(" ")
        command = COMMANDS.get(word.upper())
        if command is None:
            self.reply(f"perintah tidak dikenal: {word}")
            return False
        return command(self, state, argument.strip())

    def reply(self, text: str) -> None:
        try:
            self.request.sendall((text + "\r\n").encode())
        except OSError:
            pass
