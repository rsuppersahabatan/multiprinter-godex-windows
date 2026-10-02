#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
godex_bridge.py — port Python dari Godex.exe (print server TCP -> printer Windows).

Fungsi inti (identik dengan aslinya):
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
    * Port Prn           bind 0.0.0.0:9100, buffer sampai client disconnect,
                         lalu cetak sebagai SATU job RAW
    * Batas baris per job -> "Printer line limit (N) exceeded"

Format Godex.ini (sama persis, file lama bisa dipakai langsung):
    [Printer]  Name, Enabled, Debug
    [Prn]      Addr, Port
    [Cmd]      Addr, Port
    [Log]      Addr, Port

Dependensi: HANYA stdlib + ctypes. Tidak butuh pywin32.
Service Windows opsional (kalau pywin32 terpasang).
"""

from __future__ import annotations

import argparse
import configparser
import ctypes
import ctypes.wintypes as wt
import datetime as dt
import json
import os
import socket
import socketserver
import sys
import threading
import time

APP = "godex_bridge"
IS_WINDOWS = os.name == "nt"

ALLUSERSPROFILE = os.environ.get("ALLUSERSPROFILE") or r"C:\ProgramData"
GODEX_DIR = os.path.join(ALLUSERSPROFILE, "Godex")
DEFAULT_INI = os.path.join(GODEX_DIR, "Godex.ini")
DEFAULT_LOG_DIR = os.path.join(GODEX_DIR, "Logs")

# --------------------------------------------------------------------------
# Konfigurasi
# --------------------------------------------------------------------------

DEFAULTS = {
    "Printer": {"Name": r"\\localhost\GodexG500", "Enabled": "1", "Debug": "0"},
    "Prn": {"Addr": "0.0.0.0", "Port": "9100"},
    "Cmd": {"Addr": "127.0.0.1", "Port": "50000"},
    "Log": {"Addr": "127.0.0.1", "Port": "60000"},
}

FALSEY = {"0", "", "false", "no", "off"}


class Config:
    def __init__(self, ini_path: str, cli: argparse.Namespace):
        cp = configparser.ConfigParser()
        self.ini_path = ini_path
        self.read_ok = False
        if ini_path and os.path.isfile(ini_path):
            try:
                cp.read(ini_path, encoding="utf-8-sig")
                self.read_ok = True
            except Exception as exc:  # noqa: BLE001
                print(f"[{APP}] gagal baca {ini_path}: {exc}", file=sys.stderr)

        # GetPrivateProfileString tidak peduli besar/kecil huruf; configparser
        # peduli. Normalisasi dulu supaya [printer]/name tetap terbaca.
        raw: dict[tuple[str, str], str] = {}
        for sec in cp.sections():
            for key, val in cp.items(sec):
                raw[(sec.strip().lower(), key.strip().lower())] = val.strip()

        def get(section: str, key: str) -> str:
            return raw.get((section.lower(), key.lower()), DEFAULTS[section][key])

        self.printer = get("Printer", "Name")
        self.enabled = get("Printer", "Enabled").strip().lower() not in FALSEY
        self.debug = get("Printer", "Debug").strip().lower() not in FALSEY

        self.prn_addr, self.prn_port = get("Prn", "Addr"), int(get("Prn", "Port"))
        self.cmd_addr, self.cmd_port = get("Cmd", "Addr"), int(get("Cmd", "Port"))
        self.log_addr, self.log_port = get("Log", "Addr"), int(get("Log", "Port"))

        # Override dari command line (setara CMDADDR/CMDPORT/... di versi asli)
        for attr, val in (
            ("prn_addr", cli.prn_addr), ("prn_port", cli.prn_port),
            ("cmd_addr", cli.cmd_addr), ("cmd_port", cli.cmd_port),
            ("log_addr", cli.log_addr), ("log_port", cli.log_port),
            ("printer", cli.printer),
        ):
            if val is not None:
                setattr(self, attr, val)
        if cli.debug:
            self.debug = True
        if cli.no_cmd:
            self.cmd_port = 0
        if cli.no_log:
            self.log_port = 0

        self.line_limit = cli.line_limit
        self.idle_flush = cli.idle_flush

    def describe(self) -> list[str]:
        # Alamat/port tidak di sini: loop bind yang menuliskannya, supaya tidak dobel.
        return [
            f"ini       : {self.ini_path} ({'terbaca' if self.read_ok else 'TIDAK ADA / pakai default'})",
            f"printer   : {self.printer!r}",
            f"enabled   : {self.enabled}   debug: {self.debug}",
        ]


# --------------------------------------------------------------------------
# Logging — pola sama dengan aslinya
#   %ALLUSERSPROFILE%\Godex\Logs\Godex LOG YYYY-MM-DD.txt
#   baris: "HH:MM:SS pesan"
# --------------------------------------------------------------------------

class Log:
    def __init__(self, directory: str, debug: bool = False, echo: bool = True):
        self.dir = directory
        self.debug_on = debug
        self.echo = echo
        self._lock = threading.Lock()
        self._subs: set[socket.socket] = set()
        self._sub_lock = threading.Lock()
        try:
            os.makedirs(self.dir, exist_ok=True)
        except Exception:  # noqa: BLE001
            self.dir = os.path.join(os.environ.get("TEMP", "."), "GodexLogs")
            os.makedirs(self.dir, exist_ok=True)

    def path(self) -> str:
        return os.path.join(self.dir, f"Godex LOG {dt.date.today():%Y-%m-%d}.txt")

    def write(self, message: str) -> None:
        line = f"{dt.datetime.now():%H:%M:%S} {message}"
        with self._lock:
            try:
                with open(self.path(), "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except Exception:  # noqa: BLE001
                pass
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
        """Replay file log hari ini, lalu ikut streaming live."""
        try:
            with open(self.path(), "r", encoding="utf-8", errors="replace") as fh:
                past = fh.read()
            if past:
                conn.sendall(past.encode("utf-8", "replace"))
        except FileNotFoundError:
            pass
        except OSError:
            return
        with self._sub_lock:
            self._subs.add(conn)

    def detach(self, conn: socket.socket) -> None:
        with self._sub_lock:
            self._subs.discard(conn)

    def _broadcast(self, text: str) -> None:
        payload = text.encode("utf-8", "replace")
        with self._sub_lock:
            dead = []
            for conn in self._subs:
                try:
                    conn.sendall(payload)
                except Exception:  # noqa: BLE001
                    dead.append(conn)
            for conn in dead:
                self._subs.discard(conn)
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass


# --------------------------------------------------------------------------
# Raw printing ke spooler Windows (winspool.drv via ctypes)
# --------------------------------------------------------------------------

class DOC_INFO_1W(ctypes.Structure):
    _fields_ = [
        ("pDocName", wt.LPWSTR),
        ("pOutputFile", wt.LPWSTR),
        ("pDatatype", wt.LPWSTR),
    ]


class RawPrinter:
    """Kirim byte mentah ke printer Windows sebagai satu job RAW."""

    def __init__(self, name: str, log: Log, dry_run_dir: str | None = None):
        self.name = name or None
        self.log = log
        self.dry_run_dir = dry_run_dir
        self._spool = None
        if dry_run_dir:
            os.makedirs(dry_run_dir, exist_ok=True)
        if IS_WINDOWS and not dry_run_dir:
            self._spool = ctypes.WinDLL("winspool.drv", use_last_error=True)
            self._bind()

    def _bind(self) -> None:
        s = self._spool
        s.OpenPrinterW.argtypes = [wt.LPWSTR, ctypes.POINTER(wt.HANDLE), ctypes.c_void_p]
        s.OpenPrinterW.restype = wt.BOOL
        s.ClosePrinter.argtypes = [wt.HANDLE]
        s.ClosePrinter.restype = wt.BOOL
        s.StartDocPrinterW.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(DOC_INFO_1W)]
        s.StartDocPrinterW.restype = wt.DWORD
        s.EndDocPrinter.argtypes = [wt.HANDLE]
        s.EndDocPrinter.restype = wt.BOOL
        s.WritePrinter.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)]
        s.WritePrinter.restype = wt.BOOL

    def _winerr(self) -> str:
        code = ctypes.get_last_error()
        return f"WinError {code}: {ctypes.FormatError(code)}"

    def check(self) -> bool:
        """Buka handle printer saja (tidak mencetak). Untuk --self-test."""
        if self.dry_run_dir:
            self.log.write(f"dry-run: printer {self.name!r} tidak dibuka")
            return True
        if not self._spool:
            return False
        handle = wt.HANDLE()
        if not self._spool.OpenPrinterW(self.name, ctypes.byref(handle), None):
            self.log.error(f"OpenPrinter gagal untuk {self.name!r} -> {self._winerr()}")
            return False
        self._spool.ClosePrinter(handle)
        self.log.write(f"OpenPrinter OK untuk {self.name!r}")
        return True

    def send(self, data: bytes, doc_name: str = "Godex EZPL") -> bool:
        if not data:
            self.log.write("job kosong, dilewati")
            return True

        if self.dry_run_dir:
            target = os.path.join(self.dry_run_dir, f"{dt.datetime.now():%Y%m%d-%H%M%S-%f}.bin")
            with open(target, "wb") as fh:
                fh.write(data)
            self.log.write(f"[dry-run] {len(data)} byte ditulis ke {target}")
            return True

        if not self._spool:
            self.log.error("spooler tidak tersedia (bukan Windows?)")
            return False

        handle = wt.HANDLE()
        if not self._spool.OpenPrinterW(self.name, ctypes.byref(handle), None):
            self.log.error(f"OpenPrinter gagal untuk {self.name!r} -> {self._winerr()}")
            return False

        doc = DOC_INFO_1W(doc_name, None, "RAW")
        job_id = 0
        try:
            job_id = self._spool.StartDocPrinterW(handle, 1, ctypes.byref(doc))
            if not job_id:
                self.log.error(f"StartDocPrinter gagal -> {self._winerr()}")
                return False
            buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
            written = wt.DWORD(0)
            if not self._spool.WritePrinter(handle, buf, len(data), ctypes.byref(written)):
                self.log.error(f"WritePrinter gagal -> {self._winerr()}")
                return False
            if written.value != len(data):
                self.log.error(f"WritePrinter hanya menulis {written.value}/{len(data)} byte")
            return True
        finally:
            if job_id:
                self._spool.EndDocPrinter(handle)
            self._spool.ClosePrinter(handle)


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

class State:
    def __init__(self, cfg: Config, log: Log, printer: RawPrinter, stop_event: threading.Event):
        self.cfg = cfg
        self.log = log
        self.printer = printer
        self.stop_event = stop_event
        self.started_at = time.time()
        self.enabled = cfg.enabled
        self.jobs_ok = 0
        self.jobs_failed = 0
        self.bytes_in = 0
        self._lock = threading.Lock()

    def bump(self, ok: bool, nbytes: int) -> None:
        with self._lock:
            if ok:
                self.jobs_ok += 1
            else:
                self.jobs_failed += 1
            self.bytes_in += nbytes

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "enabled": self.enabled,
                "printer": self.cfg.printer,
                "jobs_ok": self.jobs_ok,
                "jobs_failed": self.jobs_failed,
                "bytes_in": self.bytes_in,
                "uptime_s": int(time.time() - self.started_at),
                "prn": f"{self.cfg.prn_addr}:{self.cfg.prn_port}",
            }


# --------------------------------------------------------------------------
# TCP servers
# --------------------------------------------------------------------------

class BaseServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, handler, state: State):
        self.state = state
        host, port = addr
        super().__init__((host, port), handler, bind_and_activate=False)
        self.server_bind()
        self.server_activate()


class PrnHandler(socketserver.BaseRequestHandler):
    """Terima byte mentah sampai client menutup koneksi, lalu cetak 1 job RAW."""

    def handle(self) -> None:
        st: State = self.server.state
        peer = self.client_address[0]
        st.log.write(f"Client connected from {peer}")
        self.request.settimeout(st.cfg.idle_flush or None)

        chunks: list[bytes] = []
        total = lines = 0
        limit = st.cfg.line_limit
        exceeded = False

        try:
            while True:
                try:
                    chunk = self.request.recv(65536)
                except socket.timeout:
                    if chunks:
                        break
                    continue
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                lines += chunk.count(b"\n")
                if limit and lines > limit:
                    exceeded = True
                    st.log.write(f"Printer line limit ({limit}) exceeded")
                    break
        except OSError as exc:
            st.log.write(f"Client exception occurred: {exc}")
        finally:
            try:
                self.request.close()
            except Exception:  # noqa: BLE001
                pass

        data = b"".join(chunks)
        st.log.debug(f"EZPL received: {len(data)} byte dari {peer} ({lines} baris)")

        if exceeded:
            st.bump(False, total)
        elif not st.enabled:
            st.log.write("Printer disabled - job dibuang")
            st.bump(False, total)
        else:
            try:
                ok = st.printer.send(data)
            except Exception as exc:  # noqa: BLE001
                st.log.write(f"Printer exception occurred: {exc}")
                ok = False
            st.bump(ok, total)
            st.log.write(f"job {'OK' if ok else 'GAGAL'} ({len(data)} byte)")
        st.log.write(f"Client disconnected from {peer}")


class LogHandler(socketserver.BaseRequestHandler):
    """Replay log hari ini, lalu streaming live."""

    def handle(self) -> None:
        st: State = self.server.state
        st.log.attach(self.request)
        try:
            while self.request.recv(1024):
                pass
        except OSError:
            pass
        finally:
            st.log.detach(self.request)


class CmdHandler(socketserver.BaseRequestHandler):
    """Line-based, tanpa banner. QUIT dan STOP sama seperti versi asli."""

    def handle(self) -> None:
        st: State = self.server.state
        buf = b""
        try:
            while True:
                chunk = self.request.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if self.dispatch(st, line.decode("utf-8", "replace").strip()):
                        return
        except OSError:
            pass

    def dispatch(self, st: State, cmd: str) -> bool:
        """Return True kalau koneksi harus ditutup."""
        word = cmd.split(" ", 1)[0].upper()
        if word == "QUIT":                       # <- asli
            self.reply("Closing")
            return True
        if word == "STOP":                       # <- asli
            self.reply("Stopping")
            st.log.write("Stopping godex_bridge")
            st.stop_event.set()
            return True
        # tambahan (tidak ada di versi asli, murni untuk diagnostik)
        if word == "STATUS":
            self.reply(json.dumps(st.snapshot(), indent=2))
        elif word == "ENABLE":
            st.enabled = True
            self.reply("enabled=1")
        elif word == "DISABLE":
            st.enabled = False
            self.reply("enabled=0")
        elif word == "PRINTER":
            self.reply(f"printer={st.cfg.printer}")
        elif word == "HELP":
            self.reply("QUIT | STOP | STATUS | ENABLE | DISABLE | PRINTER | HELP")
        elif word:
            self.reply(f"perintah tidak dikenal: {word}")
        return False

    def reply(self, text: str) -> None:
        try:
            self.request.sendall((text + "\r\n").encode())
        except OSError:
            pass


# --------------------------------------------------------------------------
# Bootstrap
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=APP,
        description="Port Python dari Godex.exe: TCP -> printer Windows (RAW/EZPL).",
    )
    p.add_argument("-c", "--config", default=None,
                   help=f"path .ini (default: {DEFAULT_INI} kalau ada, else Godex.ini)")
    p.add_argument("--printer", help="override nama printer")
    p.add_argument("--prn-addr")
    p.add_argument("--prn-port", type=int)
    p.add_argument("--cmd-addr")
    p.add_argument("--cmd-port", type=int)
    p.add_argument("--log-addr")
    p.add_argument("--log-port", type=int)
    p.add_argument("--no-cmd", action="store_true", help="matikan listener Cmd")
    p.add_argument("--no-log", action="store_true", help="matikan listener Log")
    p.add_argument("--log-dir", default=DEFAULT_LOG_DIR)
    p.add_argument("--no-console", action="store_true")
    p.add_argument("--line-limit", type=int, default=20000,
                   help="batas baris per job (0 = tanpa batas)")
    p.add_argument("--idle-flush", type=int, default=0,
                   help="detik diam sebelum job dianggap selesai (0 = tunggu disconnect)")
    p.add_argument("--dry-run", metavar="DIR",
                   help="jangan cetak; tulis job ke folder ini (untuk uji coba)")
    p.add_argument("--self-test", action="store_true",
                   help="baca config, buka printer, bind port, lalu keluar")
    p.add_argument("--debug", action="store_true")
    return p


def resolve_ini(path: str | None) -> str:
    if path:
        return path if os.path.isabs(path) else os.path.abspath(path)
    if os.path.isfile(DEFAULT_INI):
        return DEFAULT_INI
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "Godex.ini")


def main(argv: list[str] | None = None, stop_event: threading.Event | None = None) -> int:
    cli = build_parser().parse_args(argv)
    cfg = Config(resolve_ini(cli.config), cli)

    log = Log(cli.log_dir, debug=cfg.debug, echo=not cli.no_console)
    printer = RawPrinter(cfg.printer, log, dry_run_dir=cli.dry_run)
    stop_event = stop_event or threading.Event()
    state = State(cfg, log, printer, stop_event)

    log.write("Reading parameters from configuration file")
    for line in cfg.describe():
        log.debug(line)
    log.debug(f"log dir @{log.dir}")

    failures: list[str] = []
    servers: list[BaseServer] = []
    plan = [
        ("Cmd", cfg.cmd_addr, cfg.cmd_port, CmdHandler),
        ("Log", cfg.log_addr, cfg.log_port, LogHandler),
        ("Prn", cfg.prn_addr, cfg.prn_port, PrnHandler),
    ]
    for label, addr, port, handler in plan:
        if not port:
            log.write(f"{label} server dimatikan")
            continue
        try:
            servers.append(BaseServer((addr, port), handler, state))
            log.write(f"{label} @{addr}:{port}")
        except OSError as exc:
            msg = f"Unable to bind {label} access on address {addr} at port {port}: {exc}"
            log.error(msg)
            failures.append(msg)
            if label == "Prn" and not cli.self_test:
                log.write("Service aborted")
                for s in servers:
                    s.server_close()
                return 1

    if cli.self_test:
        if not printer.check():
            failures.append(f"printer {cfg.printer!r} tidak bisa dibuka")
        for s in servers:
            s.server_close()
        if failures:
            log.write(f"self-test SELESAI - {len(failures)} masalah ditemukan")
            for msg in failures:
                log.write("  - " + msg)
            return 1
        log.write("self-test OK - config, printer, dan port semuanya siap")
        return 0

    log.write(f"Starting {APP}")
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()

    try:
        while not stop_event.is_set() and any(t.is_alive() for t in threads):
            time.sleep(0.5)
    except KeyboardInterrupt:
        log.write("End session requested")
    finally:
        log.write("Stopping servers")
        for s in servers:
            s.shutdown()
            s.server_close()
    log.write("stopped")
    return 0


# --------------------------------------------------------------------------
# Mode service Windows (opsional, butuh pywin32)
# --------------------------------------------------------------------------

def run_as_service(name: str = "Godex") -> None:  # pragma: no cover
    try:
        import servicemanager
        import win32service
        import win32serviceutil
    except ImportError:
        raise SystemExit(
            "pywin32 belum terpasang. Jalankan sebagai aplikasi biasa, atau:\n"
            "  pip install pywin32\n"
            "  python godex_bridge.py install --startup auto\n"
            "Alternatif tanpa menyentuh Python di jalur service: bungkus dengan NSSM."
        )

    class BridgeService(win32serviceutil.ServiceFramework):
        _svc_name_ = name
        _svc_display_name_ = f"{name} Print Bridge"
        _svc_description_ = "TCP to Windows printer bridge (port Python dari Godex.exe)."

        def __init__(self, args):
            super().__init__(args)
            self._stop = threading.Event()

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            self._stop.set()

        def SvcDoRun(self):
            servicemanager.LogInfoMsg(f"{name} service started")
            main([], stop_event=self._stop)
            servicemanager.LogInfoMsg(f"{name} service stopped")

    win32serviceutil.HandleCommandLine(BridgeService)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in {"install", "remove", "start", "stop", "restart", "update"}:
        run_as_service()
    else:
        sys.exit(main())
