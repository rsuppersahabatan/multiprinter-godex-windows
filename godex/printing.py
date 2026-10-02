"""Kirim byte mentah ke printer Windows sebagai satu job RAW.

Spooler diakses lewat ctypes (winspool.drv) supaya tidak perlu pywin32.
Datatype WAJIB "RAW": kalau tidak, spooler menjalankan byte EZPL lewat driver
rendering dan hasilnya halaman kosong.

argtypes/restype juga wajib dipasang - tanpa itu ctypes memotong handle 64-bit
jadi 32-bit dan OpenPrinter gagal dengan WinError 6.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import datetime as dt
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .config import PrinterConfig
from .logstream import Log
from .paths import IS_WINDOWS

RAW_DATATYPE = "RAW"
DEFAULT_DOC_NAME = "Godex EZPL"

PRINTER_ENUM_LOCAL = 2
PRINTER_ENUM_CONNECTIONS = 4

_ENUM_LEVEL_2 = 2


class PrinterError(RuntimeError):
    """Gagal berkomunikasi dengan spooler atau printer."""


def _win_error() -> str:
    """Pesan terakhir dari Win32 (mis. 'WinError 1801: ...')."""
    try:
        code = ctypes.get_last_error()
        return f"WinError {code}: {ctypes.FormatError(code)}"
    except (AttributeError, OSError, ValueError):
        return "WinError tidak tersedia"


class _DocInfo1W(ctypes.Structure):
    _fields_ = [
        ("pDocName", wt.LPWSTR),
        ("pOutputFile", wt.LPWSTR),
        ("pDatatype", wt.LPWSTR),
    ]


class _PrinterInfo2W(ctypes.Structure):
    """PRINTER_INFO_2W - level 2 memberi nama printer *dan* nama share."""

    _fields_ = [
        ("pServerName", wt.LPWSTR),
        ("pPrinterName", wt.LPWSTR),
        ("pShareName", wt.LPWSTR),
        ("pPortName", wt.LPWSTR),
        ("pDriverName", wt.LPWSTR),
        ("pComment", wt.LPWSTR),
        ("pLocation", wt.LPWSTR),
        ("pDevMode", ctypes.c_void_p),
        ("pSepFile", wt.LPWSTR),
        ("pPrintProcessor", wt.LPWSTR),
        ("pDatatype", wt.LPWSTR),
        ("pParameters", wt.LPWSTR),
        ("pSecurityDescriptor", ctypes.c_void_p),
        ("Attributes", wt.DWORD),
        ("Priority", wt.DWORD),
        ("DefaultPriority", wt.DWORD),
        ("StartTime", wt.DWORD),
        ("UntilTime", wt.DWORD),
        ("Status", wt.DWORD),
        ("cJobs", wt.DWORD),
        ("AveragePPM", wt.DWORD),
    ]


def _load_spooler() -> ctypes.WinDLL:
    """Muat winspool.drv beserta signature semua fungsi yang dipakai."""
    spool = ctypes.WinDLL("winspool.drv", use_last_error=True)
    spool.OpenPrinterW.argtypes = [wt.LPWSTR, ctypes.POINTER(wt.HANDLE), ctypes.c_void_p]
    spool.OpenPrinterW.restype = wt.BOOL
    spool.ClosePrinter.argtypes = [wt.HANDLE]
    spool.ClosePrinter.restype = wt.BOOL
    spool.StartDocPrinterW.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(_DocInfo1W)]
    spool.StartDocPrinterW.restype = wt.DWORD
    spool.EndDocPrinter.argtypes = [wt.HANDLE]
    spool.EndDocPrinter.restype = wt.BOOL
    spool.WritePrinter.argtypes = [
        wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)
    ]
    spool.WritePrinter.restype = wt.BOOL
    return spool


@dataclass(frozen=True)
class PrinterInfo:
    """Satu printer yang terpasang di Windows."""

    name: str
    share: str

    @property
    def share_unc(self) -> str:
        return rf"\\localhost\{self.share}" if self.share else ""


def enumerate_printers() -> list[PrinterInfo]:
    """Daftar printer yang terpasang di Windows.

    Dipakai ``--list-printers`` untuk mencari nama yang benar sebelum diisi ke
    Godex.ini. Salah ketik nama/share adalah penyebab OpenPrinter gagal yang
    paling sering (WinError 1801).
    """
    if not IS_WINDOWS:
        return []

    spool = ctypes.WinDLL("winspool.drv", use_last_error=True)
    spool.EnumPrintersW.argtypes = [
        wt.DWORD, wt.LPWSTR, wt.DWORD, ctypes.c_void_p,
        wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD),
    ]
    spool.EnumPrintersW.restype = wt.BOOL

    flags = PRINTER_ENUM_LOCAL | PRINTER_ENUM_CONNECTIONS
    needed = wt.DWORD(0)
    returned = wt.DWORD(0)
    # Panggilan pertama hanya untuk tahu berapa byte yang dibutuhkan.
    spool.EnumPrintersW(flags, None, _ENUM_LEVEL_2, None, 0,
                        ctypes.byref(needed), ctypes.byref(returned))
    if not needed.value:
        return []

    buffer = (ctypes.c_ubyte * needed.value)()
    if not spool.EnumPrintersW(flags, None, _ENUM_LEVEL_2, ctypes.byref(buffer),
                               needed.value, ctypes.byref(needed), ctypes.byref(returned)):
        return []

    entries = ctypes.cast(buffer, ctypes.POINTER(_PrinterInfo2W))
    return [
        PrinterInfo(entries[i].pPrinterName or "", entries[i].pShareName or "")
        for i in range(returned.value)
    ]


@dataclass
class PrinterSlot:
    """Pasangan konfigurasi printer dengan sink keluaran yang sudah siap."""

    config: PrinterConfig
    sink: "RawPrinter"

    @property
    def label(self) -> str:
        return self.config.label


class RawPrinter:
    """Satu printer Windows, plus mode dry-run untuk uji tanpa printer."""

    def __init__(self, config: PrinterConfig, log: Log, dry_run_dir: str | Path | None = None):
        self.config = config
        self.log = log
        self.dry_run_dir = Path(dry_run_dir) if dry_run_dir else None
        self._spool = None

        if self.dry_run_dir:
            self.dry_run_dir.mkdir(parents=True, exist_ok=True)
        elif IS_WINDOWS:
            self._spool = _load_spooler()

    @property
    def label(self) -> str:
        return self.config.label

    @property
    def target(self) -> str | None:
        """Nama untuk OpenPrinter. None = printer default Windows."""
        return self.config.name or None

    # -- self-test -----------------------------------------------------------

    def check(self) -> bool:
        """Buka lalu tutup handle printer, tanpa mencetak. Untuk --self-test."""
        if self.dry_run_dir:
            self.log.write(
                f"[{self.label}] dry-run: printer {self.config.name!r} tidak dibuka"
            )
            return True
        try:
            with self._open():
                pass
        except PrinterError as exc:
            self.log.error(f"[{self.label}] {exc}")
            return False
        self.log.write(f"[{self.label}] OpenPrinter OK untuk {self.config.name!r}")
        return True

    # -- pengiriman job ------------------------------------------------------

    def send(self, data: bytes, doc_name: str = DEFAULT_DOC_NAME) -> bool:
        """Kirim satu job. Return True kalau byte diterima spooler."""
        if not data:
            self.log.write(f"[{self.label}] job kosong, dilewati")
            return True
        if self.dry_run_dir:
            return self._write_dry_run(data)
        return self._send_to_spooler(data, doc_name)

    def _write_dry_run(self, data: bytes) -> bool:
        stamp = f"{dt.datetime.now():%Y%m%d-%H%M%S-%f}"
        target = self.dry_run_dir / f"{self.label}-{stamp}.bin"
        target.write_bytes(data)
        self.log.write(f"[{self.label}] [dry-run] {len(data)} byte -> {target}")
        return True

    def _send_to_spooler(self, data: bytes, doc_name: str) -> bool:
        try:
            with self._open() as handle:
                return self._write_job(handle, data, doc_name)
        except PrinterError as exc:
            self.log.error(f"[{self.label}] {exc}")
            return False

    def _write_job(self, handle: wt.HANDLE, data: bytes, doc_name: str) -> bool:
        # Label ikut masuk nama job supaya kelihatan di antrean printer kalau
        # ada dua printer yang dipakai bersamaan.
        doc = _DocInfo1W(f"{doc_name} [{self.label}]", None, RAW_DATATYPE)
        job_id = self._spool.StartDocPrinterW(handle, 1, ctypes.byref(doc))
        if not job_id:
            self.log.error(f"[{self.label}] StartDocPrinter gagal -> {_win_error()}")
            return False

        try:
            # Buffer c_ubyte, bukan c_char_p: payload EZPL/ZPL bisa memuat NUL.
            buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
            written = wt.DWORD(0)
            if not self._spool.WritePrinter(handle, buffer, len(data), ctypes.byref(written)):
                self.log.error(f"[{self.label}] WritePrinter gagal -> {_win_error()}")
                return False
            if written.value != len(data):
                self.log.error(
                    f"[{self.label}] WritePrinter hanya menulis "
                    f"{written.value}/{len(data)} byte"
                )
            return True
        finally:
            self._spool.EndDocPrinter(handle)

    @contextmanager
    def _open(self) -> Iterator[wt.HANDLE]:
        """Buka handle printer; selalu ditutup lagi."""
        if not self._spool:
            raise PrinterError("spooler tidak tersedia (bukan Windows?)")
        handle = wt.HANDLE()
        if not self._spool.OpenPrinterW(self.target, ctypes.byref(handle), None):
            raise PrinterError(
                f"OpenPrinter gagal untuk {self.config.name!r} -> {_win_error()}"
            )
        try:
            yield handle
        finally:
            self._spool.ClosePrinter(handle)
