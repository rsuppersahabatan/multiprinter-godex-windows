"""Pembacaan konfigurasi Godex.ini.

Dua bentuk yang didukung.

A. Banyak printer (disarankan) - satu section ``[Printer:<label>]`` per printer,
   masing-masing punya listener Prn sendiri::

       [Printer:GodexG500]
       Name=\\\\localhost\\GodexG500
       Enabled=1
       Addr=0.0.0.0
       Port=9100

       [Printer:GodexRT730]
       Name=\\\\localhost\\GodexRT730
       Addr=0.0.0.0
       Port=9101

B. Satu printer (format Godex.exe asli) - ``[Printer]`` + ``[Prn]``::

       [Printer]
       Name=\\\\localhost\\GodexG500
       Enabled=1
       Debug=0

       [Prn]
       Addr=0.0.0.0
       Port=9100

Kalau ada section ``[Printer:<label>]``, bentuk A yang dipakai dan
``[Printer]``/``[Prn]`` diabaikan (dicatat sebagai peringatan).
"""

from __future__ import annotations

import argparse
import configparser
from dataclasses import dataclass, field
from pathlib import Path

#: Nilai yang dianggap "mati". GetPrivateProfileString mengembalikan string,
#: jadi 0/off/false/no semua harus dikenali - bukan cuma "0".
FALSEY = frozenset({"0", "", "false", "no", "off"})

DEFAULT_PRINTER_NAME = r"\\localhost\GodexG500"
DEFAULT_PRN_ADDR, DEFAULT_PRN_PORT = "0.0.0.0", 9100
DEFAULT_CMD_ADDR, DEFAULT_CMD_PORT = "127.0.0.1", 50000
DEFAULT_LOG_ADDR, DEFAULT_LOG_PORT = "127.0.0.1", 60000

#: Label default untuk printer dari format lama (tidak punya nama section).
LEGACY_LABEL = "default"

MULTI_PREFIX = "printer:"
LEGACY_PRINTER_SECTION = "printer"
LEGACY_PRN_SECTION = "prn"
CMD_SECTION = "cmd"
LOG_SECTION = "log"


@dataclass(frozen=True)
class Endpoint:
    """Pasangan alamat:port untuk satu listener TCP."""

    addr: str
    port: int

    @property
    def active(self) -> bool:
        """Port 0 berarti listener sengaja dimatikan."""
        return self.port > 0

    def __str__(self) -> str:
        return f"{self.addr}:{self.port}"


@dataclass
class PrinterConfig:
    """Satu printer Windows beserta listener Prn yang melayaninya."""

    label: str
    name: str
    endpoint: Endpoint
    enabled: bool = True
    debug: bool = False

    def __str__(self) -> str:
        flags = [flag for flag, on in (("disabled", not self.enabled), ("debug", self.debug)) if on]
        suffix = f" ({', '.join(flags)})" if flags else ""
        return f"{self.label}: {self.name or '(printer default Windows)'} @{self.endpoint}{suffix}"


@dataclass
class Config:
    """Isi Godex.ini setelah dibaca dan ditimpa opsi command line."""

    ini_path: Path
    read_ok: bool
    printers: list[PrinterConfig]
    cmd: Endpoint
    log: Endpoint
    line_limit: int
    idle_flush: int
    warnings: list[str] = field(default_factory=list)

    @property
    def debug(self) -> bool:
        """Debug dianggap aktif kalau salah satu printer memintanya."""
        return any(p.debug for p in self.printers)

    def describe(self) -> list[str]:
        """Baris ringkas untuk log saat start.

        Alamat/port tiap listener tidak diulang di sini - loop bind yang
        menuliskannya, supaya tidak dobel.
        """
        lines = [
            f"ini       : {self.ini_path} "
            f"({'terbaca' if self.read_ok else 'TIDAK ADA / pakai default'})",
        ]
        lines += [f"printer   : {p}" for p in self.printers]
        lines.append(f"cmd       : {self.cmd if self.cmd.active else 'dimatikan'}")
        lines.append(f"log       : {self.log if self.log.active else 'dimatikan'}")
        lines.append(
            f"job limit : {self.line_limit or 'tanpa batas'} baris, "
            f"idle flush {self.idle_flush}s"
        )
        return lines

    def validate(self) -> list[str]:
        """Cari hal-hal yang pasti bikin gagal/salah saat runtime."""
        problems = list(self.warnings)

        if not self.printers:
            problems.append("tidak ada printer yang dikonfigurasi")

        port_owner: dict[int, str] = {}
        for printer in self.printers:
            if not printer.endpoint.active:
                continue
            taken = port_owner.get(printer.endpoint.port)
            if taken:
                problems.append(
                    f"port Prn {printer.endpoint.port} dipakai dua printer: "
                    f"{taken} dan {printer.label}"
                )
            port_owner[printer.endpoint.port] = printer.label

        for name, endpoint in (("Cmd", self.cmd), ("Log", self.log)):
            if endpoint.active and endpoint.port in port_owner:
                problems.append(
                    f"port {endpoint.port} ({name}) bentrok dengan printer "
                    f"{port_owner[endpoint.port]}"
                )

        if self.printers and not any(p.enabled for p in self.printers):
            problems.append(
                "semua printer berstatus Enabled=0 - semua job akan dibuang"
            )

        return problems


class _Ini:
    """Pembacaan .ini yang tidak peduli besar/kecil huruf.

    GetPrivateProfileString (yang dipakai Godex.exe) case-insensitive,
    configparser tidak. Karena itu semua section & key di-lowercase dulu,
    supaya ``[printer]``/``name`` tetap terbaca.
    """

    def __init__(self, path: Path, warnings: list[str]):
        self.warnings = warnings
        self.sections: list[str] = []
        self.read_ok = False
        self._raw: dict[tuple[str, str], str] = {}

        if not path or not path.is_file():
            return

        # RawConfigParser, bukan ConfigParser: nilai yang memuat '%' jangan
        # dianggap sintaks interpolation (nama printer bisa saja memuatnya).
        parser = configparser.RawConfigParser()
        try:
            parser.read(path, encoding="utf-8-sig")
        except (configparser.Error, OSError) as exc:
            warnings.append(f"gagal baca {path}: {exc}")
            return

        self.read_ok = True
        for section in parser.sections():
            self.sections.append(section)
            for key, value in parser.items(section):
                self._raw[(section.strip().lower(), key.strip().lower())] = value.strip()

    def raw(self, section: str, key: str) -> str | None:
        """Nilai apa adanya; None kalau key tidak ada. String kosong dianggap ada."""
        return self._raw.get((section.lower(), key.lower()))

    def text(self, section: str, key: str, fallback: str) -> str:
        """Nilai non-kosong; kosong atau tidak ada -> fallback."""
        value = self.raw(section, key)
        return fallback if value is None or value == "" else value

    def flag(self, section: str, key: str, fallback: bool) -> bool:
        value = self.raw(section, key)
        return fallback if value is None else value.lower() not in FALSEY

    def number(self, section: str, key: str, fallback: int) -> int:
        value = self.raw(section, key)
        if value is None or value == "":
            return fallback
        try:
            return int(value)
        except ValueError:
            self.warnings.append(
                f"{section}.{key}={value!r} bukan angka - dipakai {fallback}"
            )
            return fallback

    def has(self, section: str) -> bool:
        return any(s.strip().lower() == section.lower() for s in self.sections)

    def printer_labels(self) -> list[str]:
        """Label dari semua section [Printer:<label>], mengikuti urutan file."""
        labels = []
        for section in self.sections:
            name = section.strip()
            if name.lower().startswith(MULTI_PREFIX):
                label = name[len(MULTI_PREFIX):].strip()
                if label:
                    labels.append(label)
        return labels


def load_config(ini_path: Path, cli: argparse.Namespace) -> Config:
    """Baca .ini lalu timpa dengan opsi command line."""
    warnings: list[str] = []
    ini = _Ini(ini_path, warnings)

    config = Config(
        ini_path=ini_path,
        read_ok=ini.read_ok,
        printers=_read_printers(ini, warnings),
        cmd=Endpoint(
            ini.text(CMD_SECTION, "addr", DEFAULT_CMD_ADDR),
            ini.number(CMD_SECTION, "port", DEFAULT_CMD_PORT),
        ),
        log=Endpoint(
            ini.text(LOG_SECTION, "addr", DEFAULT_LOG_ADDR),
            ini.number(LOG_SECTION, "port", DEFAULT_LOG_PORT),
        ),
        line_limit=cli.line_limit,
        idle_flush=cli.idle_flush,
        warnings=warnings,
    )
    _apply_cli_overrides(config, cli)
    return config


def _read_printers(ini: _Ini, warnings: list[str]) -> list[PrinterConfig]:
    labels = ini.printer_labels()
    if not labels:
        return [_legacy_printer(ini)]

    if ini.has(LEGACY_PRINTER_SECTION) or ini.has(LEGACY_PRN_SECTION):
        warnings.append(
            "ada [Printer:<label>] sekaligus [Printer]/[Prn] - "
            "yang dipakai hanya [Printer:<label>]"
        )

    return [_named_printer(ini, label) for label in labels]


def _named_printer(ini: _Ini, label: str) -> PrinterConfig:
    section = MULTI_PREFIX + label
    return PrinterConfig(
        label=label,
        name=_printer_name(ini, section),
        endpoint=Endpoint(
            ini.text(section, "addr", DEFAULT_PRN_ADDR),
            ini.number(section, "port", DEFAULT_PRN_PORT),
        ),
        enabled=ini.flag(section, "enabled", True),
        debug=ini.flag(section, "debug", False),
    )


def _legacy_printer(ini: _Ini) -> PrinterConfig:
    """Format Godex.exe asli: [Printer] untuk nama, [Prn] untuk alamat/port."""
    return PrinterConfig(
        label=LEGACY_LABEL,
        name=_printer_name(ini, LEGACY_PRINTER_SECTION),
        endpoint=Endpoint(
            ini.text(LEGACY_PRN_SECTION, "addr", DEFAULT_PRN_ADDR),
            ini.number(LEGACY_PRN_SECTION, "port", DEFAULT_PRN_PORT),
        ),
        enabled=ini.flag(LEGACY_PRINTER_SECTION, "enabled", True),
        debug=ini.flag(LEGACY_PRINTER_SECTION, "debug", False),
    )


def _printer_name(ini: _Ini, section: str) -> str:
    """Nama printer.

    Key tidak ada -> default. Key ada tapi kosong -> string kosong, yang oleh
    spooler diartikan "printer default Windows" (perilaku Godex.exe asli).
    """
    value = ini.raw(section, "name")
    return DEFAULT_PRINTER_NAME if value is None else value


def _apply_cli_overrides(config: Config, cli: argparse.Namespace) -> None:
    """Timpa konfigurasi dengan opsi command line.

    ``--printer``/``--prn-addr``/``--prn-port`` hanya mengenai printer
    pertama - di format lama memang cuma ada satu printer.
    """
    if config.printers:
        first = config.printers[0]
        if cli.printer is not None:
            first.name = cli.printer
        first.endpoint = Endpoint(
            cli.prn_addr if cli.prn_addr is not None else first.endpoint.addr,
            cli.prn_port if cli.prn_port is not None else first.endpoint.port,
        )
        touched = [o for o in (cli.printer, cli.prn_addr, cli.prn_port) if o is not None]
        if touched and len(config.printers) > 1:
            config.warnings.append(
                f"--printer/--prn-addr/--prn-port hanya berlaku untuk printer "
                f"pertama ({first.label})"
            )

    if cli.debug:
        for printer in config.printers:
            printer.debug = True

    config.cmd = Endpoint(
        cli.cmd_addr if cli.cmd_addr is not None else config.cmd.addr,
        0 if cli.no_cmd else (cli.cmd_port if cli.cmd_port is not None else config.cmd.port),
    )
    config.log = Endpoint(
        cli.log_addr if cli.log_addr is not None else config.log.addr,
        0 if cli.no_log else (cli.log_port if cli.log_port is not None else config.log.port),
    )
