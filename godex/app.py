"""Perakitan: baca konfigurasi, siapkan printer, bind listener, lalu jalankan."""

from __future__ import annotations

import argparse
import socketserver
import threading
import time
from dataclasses import dataclass

from .config import Config, Endpoint, load_config
from .logstream import Log
from .paths import APP, DEFAULT_INI, DEFAULT_LOG_DIR, resolve_ini
from .printing import PrinterSlot, RawPrinter, enumerate_printers
from .servers import (
    POLL_INTERVAL_S,
    BridgeServer,
    CmdHandler,
    LogHandler,
    PrnHandler,
    State,
)


@dataclass(frozen=True)
class ListenerSpec:
    """Rencana satu listener TCP.

    ``label`` adalah identitas yang dipakai untuk statistik dan prefix log:
    label printer (mis. "GodexG500") untuk jalur data, atau "Cmd"/"Log" untuk
    kanal kontrol. Ini harus sama persis dengan label printer, karena State
    mencari printer berdasarkan label itu.
    """

    label: str
    endpoint: Endpoint
    handler: type[socketserver.BaseRequestHandler]
    printer: RawPrinter | None = None
    fatal_on_bind_error: bool = False

    @property
    def display(self) -> str:
        """Nama listener untuk pesan bind failure."""
        return f"Prn[{self.label}]" if self.printer else self.label


@dataclass(frozen=True)
class BindFailure:
    """Satu listener yang gagal bind."""

    display: str
    message: str
    fatal: bool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP,
        description="Port Python dari Godex.exe: TCP -> printer Windows (RAW/EZPL).",
    )
    parser.add_argument("-c", "--config", default=None,
                        help=f"path .ini (default: {DEFAULT_INI} kalau ada, else Godex.ini)")
    parser.add_argument("--list-printers", action="store_true",
                        help="tampilkan printer Windows yang terpasang, lalu keluar")
    parser.add_argument("--printer", help="override nama printer pertama")
    parser.add_argument("--prn-addr", help="override alamat listener Prn printer pertama")
    parser.add_argument("--prn-port", type=int, help="override port listener Prn printer pertama")
    parser.add_argument("--cmd-addr")
    parser.add_argument("--cmd-port", type=int)
    parser.add_argument("--log-addr")
    parser.add_argument("--log-port", type=int)
    parser.add_argument("--no-cmd", action="store_true", help="matikan listener Cmd")
    parser.add_argument("--no-log", action="store_true", help="matikan listener Log")
    parser.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    parser.add_argument("--no-console", action="store_true")
    parser.add_argument("--line-limit", type=int, default=20000,
                        help="batas baris per job (0 = tanpa batas)")
    parser.add_argument("--idle-flush", type=int, default=0,
                        help="detik diam sebelum job dianggap selesai "
                             "(0 = tunggu client disconnect)")
    parser.add_argument("--dry-run", metavar="DIR",
                        help="jangan cetak; tulis job ke folder ini (untuk uji coba)")
    parser.add_argument("--self-test", action="store_true",
                        help="baca config, buka printer, bind port, lalu keluar")
    parser.add_argument("--debug", action="store_true")
    return parser


def main(argv: list[str] | None = None, stop_event: threading.Event | None = None) -> int:
    cli = build_parser().parse_args(argv)

    if cli.list_printers:
        return _list_printers()

    config = load_config(resolve_ini(cli.config), cli)
    log = Log(cli.log_dir, debug=config.debug, echo=not cli.no_console)

    log.write("Reading parameters from configuration file")
    for line in config.describe():
        log.debug(line)
    log.debug(f"log dir @{log.dir}")
    for problem in config.validate():
        log.write(f"peringatan: {problem}")

    slots = _build_slots(config, log, cli.dry_run)
    stop_event = stop_event or threading.Event()
    state = State(config, log, stop_event)

    servers, failures = _start_servers(config, state, slots, log)

    if cli.self_test:
        return _run_self_test(log, slots, servers, failures)

    if any(failure.fatal for failure in failures):
        log.write("Service aborted")
        _close_servers(servers)
        return 1

    return _serve(servers, stop_event, log)


def _list_printers() -> int:
    """Tampilkan printer Windows supaya nama di Godex.ini bisa dipastikan benar."""
    printers = enumerate_printers()
    if not printers:
        print("tidak ada printer yang terdeteksi")
        return 1
    print(f"{len(printers)} printer terpasang:")
    for info in printers:
        detail = (f"  share: {info.share_unc}" if info.share
                  else "  (tanpa share - isi Name= dengan nama di atas)")
        print(f"  - {info.name}{detail}")
    return 0


def _build_slots(config: Config, log: Log, dry_run: str | None) -> list[PrinterSlot]:
    """Siapkan satu sink keluaran per printer yang dikonfigurasi."""
    return [
        PrinterSlot(printer, RawPrinter(printer, log, dry_run))
        for printer in config.printers
    ]


def _listener_plan(config: Config, slots: list[PrinterSlot], log: Log) -> list[ListenerSpec]:
    """Urutan bind: kanal kontrol dulu, baru jalur data.

    Kalau bind Prn gagal, service dibatalkan - jadi lebih enak kalau Cmd dan
    Log sudah ada duluan supaya pesan errornya sempat terkirim ke client log.
    """
    plan: list[ListenerSpec] = []

    if config.cmd.active:
        plan.append(ListenerSpec("Cmd", config.cmd, CmdHandler))
    else:
        log.write("Cmd server dimatikan")

    if config.log.active:
        plan.append(ListenerSpec("Log", config.log, LogHandler))
    else:
        log.write("Log server dimatikan")

    for slot in slots:
        if slot.config.endpoint.active:
            # Satu listener Prn per printer: ini yang bikin host dengan dua
            # printer bisa melayani keduanya sekaligus.
            plan.append(ListenerSpec(
                label=slot.label,
                endpoint=slot.config.endpoint,
                handler=PrnHandler,
                printer=slot.sink,
                # Printer yang tidak bisa bind = service tidak layak jalan.
                # Lebih baik mati dengan pesan jelas daripada jalan setengah
                # hidup dan diam-diam menolak semua job.
                fatal_on_bind_error=True,
            ))
        else:
            log.write(f"Prn[{slot.label}] dimatikan")

    return plan


def _start_servers(config: Config, state: State, slots: list[PrinterSlot],
                   log: Log) -> tuple[list[BridgeServer], list[BindFailure]]:
    servers: list[BridgeServer] = []
    failures: list[BindFailure] = []

    for spec in _listener_plan(config, slots, log):
        try:
            server = BridgeServer(spec.endpoint, spec.handler, state=state,
                                  printer=spec.printer, label=spec.label)
        except OSError as exc:
            message = (f"Unable to bind {spec.display} access on address "
                       f"{spec.endpoint.addr} at port {spec.endpoint.port}: {exc}")
            log.error(message)
            failures.append(BindFailure(spec.display, message, spec.fatal_on_bind_error))
            continue
        servers.append(server)
        log.write(f"{spec.display} @{spec.endpoint}")

    return servers, failures


def _close_servers(servers: list[BridgeServer]) -> None:
    """Tutup socket saja.

    Jangan pakai shutdown() di sini: shutdown() menunggu loop serve_forever
    berhenti, dan itu menggantung kalau thread-nya belum pernah dijalankan
    (mis. saat bind gagal atau self-test).
    """
    for server in servers:
        server.server_close()


def _stop_servers(servers: list[BridgeServer], log: Log) -> None:
    """Hentikan server yang serve_forever-nya memang sedang jalan."""
    log.write("Stopping servers")
    for server in servers:
        server.shutdown()
        server.server_close()


def _run_self_test(log: Log, slots: list[PrinterSlot],
                   servers: list[BridgeServer], failures: list[BindFailure]) -> int:
    problems = [failure.message for failure in failures]
    for slot in slots:
        if not slot.sink.check():
            problems.append(f"printer {slot.config.name!r} tidak bisa dibuka")
    _close_servers(servers)

    if problems:
        log.write(f"self-test SELESAI - {len(problems)} masalah ditemukan")
        for problem in problems:
            log.write("  - " + problem)
        return 1

    log.write("self-test OK - config, printer, dan port semuanya siap")
    return 0


def _serve(servers: list[BridgeServer], stop_event: threading.Event, log: Log) -> int:
    log.write(f"Starting {APP}")
    threads = [threading.Thread(target=server.serve_forever, daemon=True)
               for server in servers]
    for thread in threads:
        thread.start()

    try:
        while not stop_event.is_set() and any(t.is_alive() for t in threads):
            time.sleep(POLL_INTERVAL_S)
    except KeyboardInterrupt:
        log.write("End session requested")
    finally:
        _stop_servers(servers, log)

    log.write("stopped")
    return 0
