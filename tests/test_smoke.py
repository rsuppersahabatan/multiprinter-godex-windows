"""Smoke test end-to-end untuk godex_bridge.

Menjalankan CLI sungguhan sebagai subprocess dan mengobrol lewat TCP, persis
seperti aplikasi pengirim label. Semua port dipilih dinamis, jadi test ini aman
dijalankan walau Godex.exe asli sedang memakai 9100/50000/60000.

Selalu memakai --dry-run, jadi TIDAK ada yang benar-benar tercetak.

    python tests/test_smoke.py

Yang diuji:
  1. dua printer -> dua listener Prn, job masuk ke printer yang benar
  2. saklar enable per printer dan saklar utama
  3. batas baris per job
  4. Godex.ini format lama ([Printer] + [Prn]) tetap jalan
  5. port ganda -> bind kedua gagal, proses keluar dengan kode 1
  6. --self-test
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "godex_bridge.py"
PY = sys.executable

PAYLOAD_A = b"^XA\n^FO50,50^FDprinter-1^FS\n^XZ\n"
PAYLOAD_B = b"^XA\n^FO50,50^FDprinter-2^FS\n^XZ\n"

FAILURES: list[str] = []


# ---------------------------------------------------------------- utilitas

def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  <- {detail}" if detail else ""),
          flush=True)
    if not ok:
        FAILURES.append(name)


def free_ports(count: int) -> list[int]:
    """Cari `count` port TCP yang bebas di 127.0.0.1."""
    ports: list[int] = []
    while len(ports) < count:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        if port not in ports:
            ports.append(port)
    return ports


def wait_port(port: int, timeout: float = 20.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def write_ini(path: Path, printers: list[tuple[str, str, int]],
              cmd_port: int, log_port: int) -> Path:
    """Tulis Godex.ini bentuk banyak printer."""
    blocks = [
        f"[Printer:{label}]\nName={name}\nEnabled=1\nAddr=127.0.0.1\nPort={port}\n"
        for label, name, port in printers
    ]
    body = "\n".join(blocks)
    path.write_text(
        f"{body}\n[Cmd]\nAddr=127.0.0.1\nPort={cmd_port}\n\n"
        f"[Log]\nAddr=127.0.0.1\nPort={log_port}\n",
        encoding="utf-8",
    )
    return path


def write_legacy_ini(path: Path, name: str, prn_port: int,
                     cmd_port: int, log_port: int) -> Path:
    """Tulis Godex.ini bentuk Godex.exe asli."""
    path.write_text(
        f"[Printer]\nName={name}\nEnabled=1\nDebug=1\n\n"
        f"[Prn]\nAddr=127.0.0.1\nPort={prn_port}\n\n"
        f"[Cmd]\nAddr=127.0.0.1\nPort={cmd_port}\n\n"
        f"[Log]\nAddr=127.0.0.1\nPort={log_port}\n",
        encoding="utf-8",
    )
    return path


class Bridge:
    """Satu proses godex_bridge yang sedang diuji."""

    def __init__(self, ini: Path, workdir: Path, cmd_port: int, extra: tuple = ()):
        self.ini = ini
        self.cmd_port = cmd_port
        self.out = workdir / "out"
        self.logs = workdir / "logs"
        self.extra = list(extra)
        self.proc: subprocess.Popen | None = None

    # -- siklus hidup ------------------------------------------------------

    def start(self) -> "Bridge":
        shutil.rmtree(self.out, ignore_errors=True)
        self.out.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(self.logs, ignore_errors=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        args = [PY, str(BRIDGE), "-c", str(self.ini), "--dry-run", str(self.out),
                "--log-dir", str(self.logs), "--no-console", *self.extra]
        self.proc = subprocess.Popen(args, cwd=str(ROOT), stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True)
        return self

    def stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            self.cmd("STOP")
        except OSError:
            pass
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()

    def wait_exit(self, timeout: float = 20.0) -> int | None:
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        return self.proc.returncode

    def output(self) -> str:
        return self.proc.stdout.read() if self.proc and self.proc.stdout else ""

    # -- interaksi ---------------------------------------------------------

    def send(self, port: int, payload: bytes, timeout: float = 5.0) -> None:
        """Kirim satu job; keluar dari `with` = close = server melihat EOF."""
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.sendall(payload)

    def cmd(self, line: str, timeout: float = 3.0) -> str:
        with socket.create_connection(("127.0.0.1", self.cmd_port), timeout=timeout) as sock:
            sock.sendall(line.encode() + b"\n")
            chunks = [sock.recv(65536)]
            sock.settimeout(0.4)
            try:
                while True:
                    more = sock.recv(65536)
                    if not more:
                        break
                    chunks.append(more)
            except socket.timeout:
                pass
            return b"".join(chunks).decode("utf-8", "replace")

    def status(self) -> dict:
        return json.loads(self.cmd("STATUS").strip())

    def printers(self) -> dict:
        return {p["label"]: p for p in self.status()["printers"]}

    def log_text(self) -> str:
        files = sorted(self.logs.glob("Godex LOG *.txt"))
        return files[-1].read_text(encoding="utf-8", errors="replace") if files else ""

    def wait_log(self, needle: str, timeout: float = 10.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if needle in self.log_text():
                return True
            time.sleep(0.15)
        return False

    def jobs(self, prefix: str = "") -> list[Path]:
        return sorted(self.out.glob(f"{prefix}*.bin"))


def run_cli(*extra: str, timeout: float = 90.0) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(BRIDGE), *extra], cwd=str(ROOT),
                          capture_output=True, text=True, timeout=timeout)


# ---------------------------------------------------------------- test 1-2

def test_two_printers(work: Path) -> None:
    print("\n== 1. dua printer: routing per port ==", flush=True)
    prn1, prn2, cmd_port, log_port = free_ports(4)
    ini = write_ini(work / "two.ini", [("GodexG500", "Godex G500 (dummy)", prn1),
                                       ("GodexRT730", "Godex RT730 (dummy)", prn2)],
                    cmd_port, log_port)
    bridge = Bridge(ini, work, cmd_port)
    try:
        bridge.start()
        check("listener Cmd siap", wait_port(cmd_port))
        check("Prn[GodexG500] bind", bridge.wait_log(f"Prn[GodexG500] @127.0.0.1:{prn1}"))
        check("Prn[GodexRT730] bind", bridge.wait_log(f"Prn[GodexRT730] @127.0.0.1:{prn2}"))

        bridge.send(prn1, PAYLOAD_A)
        bridge.send(prn2, PAYLOAD_B)
        time.sleep(1.5)

        names = [p.name for p in bridge.jobs()]
        check("dua file job terpisah", len(names) == 2, str(names))
        made_a = bridge.jobs("GodexG500-")
        made_b = bridge.jobs("GodexRT730-")
        check(f"job port {prn1} -> GodexG500", len(made_a) == 1, str(names))
        check(f"job port {prn2} -> GodexRT730", len(made_b) == 1, str(names))
        if made_a:
            check("byte printer-1 utuh", made_a[0].read_bytes() == PAYLOAD_A)
        if made_b:
            check("byte printer-2 utuh", made_b[0].read_bytes() == PAYLOAD_B)

        per = bridge.printers()
        check("STATUS memuat 2 printer", len(per) == 2, str(list(per)))
        check("counter printer 1", per["GodexG500"]["jobs_ok"] == 1, str(per.get("GodexG500")))
        check("counter printer 2", per["GodexRT730"]["jobs_ok"] == 1, str(per.get("GodexRT730")))
        check("bytes_in printer 2", per["GodexRT730"]["bytes_in"] == len(PAYLOAD_B),
              str(per.get("GodexRT730")))

        reply = bridge.cmd("PRINTER")
        check("PRINTER menyebut kedua label",
              "GodexG500" in reply and "GodexRT730" in reply, reply.strip())
        check("HELP terdaftar", "QUIT" in bridge.cmd("HELP"))
        check("perintah tak dikenal dibalas", "tidak dikenal" in bridge.cmd("BOGUS"))

        check("DISABLE satu printer", "enabled=0" in bridge.cmd("DISABLE GodexRT730"))
        bridge.send(prn2, PAYLOAD_B)
        time.sleep(1.0)
        check("job ke printer disabled tidak dicetak",
              len(bridge.jobs("GodexRT730-")) == 1,
              str([p.name for p in bridge.jobs("GodexRT730-")]))
        per = bridge.printers()
        check("jobs_failed naik di printer disabled",
              per["GodexRT730"]["jobs_failed"] == 1, str(per.get("GodexRT730")))
        check("printer lain tetap aktif", per["GodexG500"]["enabled"] is True)
        check("ENABLE mengembalikan", "enabled=1" in bridge.cmd("ENABLE GodexRT730"))

        check("DISABLE semua printer", "enabled=0" in bridge.cmd("DISABLE"))
        bridge.send(prn1, PAYLOAD_A)
        time.sleep(0.8)
        check("saklar utama menahan semua job", len(bridge.jobs("GodexG500-")) == 1)
        bridge.cmd("ENABLE")

        check("STOP dibalas", "Stopping" in bridge.cmd("STOP"))
        check("proses keluar sendiri setelah STOP", bridge.wait_exit() == 0,
              f"rc={bridge.proc.returncode} {bridge.output()[-300:]}")
    finally:
        bridge.stop()


def test_line_limit(work: Path) -> None:
    print("\n== 2. batas baris per job ==", flush=True)
    prn1, prn2, cmd_port, log_port = free_ports(4)
    ini = write_ini(work / "limit.ini", [("A", "A (dummy)", prn1), ("B", "B (dummy)", prn2)],
                    cmd_port, log_port)
    bridge = Bridge(ini, work, cmd_port, extra=("--line-limit", "2"))
    try:
        bridge.start()
        check("listener Cmd siap", wait_port(cmd_port))
        bridge.send(prn1, b"baris1\nbaris2\nbaris3\n")
        time.sleep(1.0)
        check("job melebihi limit tidak dicetak", not bridge.jobs(),
              str([p.name for p in bridge.jobs()]))
        check("pesan line limit muncul di log",
              "Printer line limit (2) exceeded" in bridge.log_text())
        bridge.cmd("STOP")
        check("proses keluar sendiri setelah STOP", bridge.wait_exit() == 0)
    finally:
        bridge.stop()


# ---------------------------------------------------------------- test 3

def test_legacy_ini(work: Path) -> None:
    print("\n== 3. Godex.ini format lama tetap jalan ==", flush=True)
    prn, cmd_port, log_port = free_ports(3)
    ini = write_legacy_ini(work / "legacy.ini", "Legacy Printer", prn, cmd_port, log_port)
    bridge = Bridge(ini, work, cmd_port)
    try:
        bridge.start()
        check("listener Cmd siap", wait_port(cmd_port))
        check("Prn[default] bind", bridge.wait_log(f"Prn[default] @127.0.0.1:{prn}"))
        bridge.send(prn, PAYLOAD_A)
        time.sleep(1.0)
        made = bridge.jobs()
        check("job tercetak lewat label 'default'",
              len(made) == 1 and made[0].name.startswith("default-"),
              str([p.name for p in made]))
        per = bridge.printers()
        check("format lama -> 1 printer", len(per) == 1, str(per))
        check("nama printer terbaca dari [Printer]",
              per["default"]["name"] == "Legacy Printer", str(per.get("default")))
        bridge.cmd("STOP")
        check("proses keluar sendiri setelah STOP", bridge.wait_exit() == 0)
    finally:
        bridge.stop()


# ---------------------------------------------------------------- test 4

def test_duplicate_port(work: Path) -> None:
    print("\n== 4. port ganda: bind kedua harus gagal ==", flush=True)
    prn, cmd_port, log_port = free_ports(3)
    ini = write_ini(work / "dup.ini", [("A", "A (dummy)", prn), ("B", "B (dummy)", prn)],
                    cmd_port, log_port)
    bridge = Bridge(ini, work, cmd_port)
    try:
        bridge.start()
        check("port ganda terdeteksi sebagai peringatan",
              bridge.wait_log(f"peringatan: port Prn {prn} dipakai dua printer"))
        check("bind kedua gagal -> exit 1 (tidak jalan setengah hidup)",
              bridge.wait_exit() == 1, f"rc={bridge.proc.returncode}")
        check("pesan bind failure menyebut endpoint",
              "Unable to bind Prn[B]" in bridge.log_text(), bridge.log_text()[-400:])
        check("service aborted dicatat", "Service aborted" in bridge.log_text())
    finally:
        bridge.stop()


# ---------------------------------------------------------------- test 5

def test_self_test(work: Path) -> None:
    print("\n== 5. --self-test ==", flush=True)
    prn1, prn2, cmd_port, log_port = free_ports(4)
    good = write_ini(work / "good.ini", [("A", "A (dummy)", prn1), ("B", "B (dummy)", prn2)],
                     cmd_port, log_port)
    result = run_cli("-c", str(good), "--self-test", "--dry-run", str(work / "out"),
                     "--log-dir", str(work / "logs"))
    check("self-test lolos di port bebas + dry-run", result.returncode == 0,
          (result.stdout or "")[-500:])

    # Port yang sengaja diduduki dulu -> self-test harus gagal keras.
    prn_busy, cmd_busy, log_busy = free_ports(3)
    guard = socket.socket()
    try:
        guard.bind(("127.0.0.1", prn_busy))
        guard.listen(1)
        busy = write_ini(work / "busy.ini", [("A", "A (dummy)", prn_busy)],
                         cmd_busy, log_busy)
        result = run_cli("-c", str(busy), "--self-test", "--dry-run", str(work / "out"),
                         "--log-dir", str(work / "logs"))
        check("self-test gagal kalau port Prn sudah dipakai", result.returncode == 1,
              f"rc={result.returncode}")
        check("pesan bind failure memuat nama printer",
              "Unable to bind Prn[A]" in (result.stdout or ""), (result.stdout or "")[-400:])
    finally:
        guard.close()

    # Nama printer yang tidak ada -> OpenPrinter harus ketahuan saat self-test.
    prn_x, cmd_x, log_x = free_ports(3)
    bogus = write_ini(work / "bogus.ini",
                      [("A", r"\\localhost\DefinitelyNotAShare_9f3a", prn_x)],
                      cmd_x, log_x)
    result = run_cli("-c", str(bogus), "--self-test", "--log-dir", str(work / "logs"))
    check("self-test menangkap nama printer yang salah", result.returncode == 1,
          f"rc={result.returncode}")
    check("pesan menyebut printer tidak bisa dibuka",
          "tidak bisa dibuka" in (result.stdout or ""), (result.stdout or "")[-500:])


# ---------------------------------------------------------------- main

def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="godex-smoke-"))
    try:
        test_two_printers(work)
        test_line_limit(work)
        test_legacy_ini(work)
        test_duplicate_port(work)
        test_self_test(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"GAGAL: {len(FAILURES)}")
        for name in FAILURES:
            print("  - " + name)
        return 1
    print("SEMUA TEST LULUS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
