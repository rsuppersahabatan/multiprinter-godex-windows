# godex_bridge

Print server TCP untuk printer label Windows — pengganti `Godex.exe`.

Aplikasi pengirim menembak byte **EZPL** ke sebuah port TCP, dan bridge ini
mencetaknya ke antrean printer Windows sebagai **satu job RAW** (tanpa dialog
driver, tanpa rendering). Satu host bisa melayani **beberapa printer
sekaligus** — tiap printer punya port sendiri.

- Tanpa Python di mesin target (versi `.exe`)
- Hanya stdlib + `ctypes` — tidak butuh driver Godex
- Kompatibel dengan `Godex.ini` lama dan siap menggantikan service `Godex.exe`

---

## 1. Mulai cepat

```
godex_bridge.exe --list-printers      :: 1. cari nama printer yang benar
notepad Godex.ini                     :: 2. isi nama printer + port
godex_bridge.exe --self-test          :: 3. pastikan config, printer, dan port siap
godex_bridge.exe                      :: 4. jalankan
```

`--self-test` adalah langkah yang paling sering menyelamatkan: ia membaca
config, membuka tiap printer (buka-tutup, **tidak** mencetak), dan bind semua
port. Kalau ada yang salah, ia menyebutkan yang mana.

---

## 2. Konfigurasi `Godex.ini`

### 2.1 Dua printer (bentuk yang disarankan)

```ini
[Printer:GodexG500]
Name=\\localhost\GodexG500
Enabled=1
Debug=0
Addr=0.0.0.0
Port=9100

[Printer:GodexRT730]
Name=\\localhost\GodexRT730
Enabled=1
Debug=0
Addr=0.0.0.0
Port=9101

[Cmd]
Addr=127.0.0.1
Port=50000

[Log]
Addr=127.0.0.1
Port=60000
```

Aplikasi pengirim memilih printer dengan **mengganti port**: `9100` → printer
pertama, `9101` → printer kedua. Tidak ada perubahan protokol, karena port 9100
memang tidak punya header yang bisa dipakai memilih tujuan.

Label setelah tanda `:` (di sini `GodexG500` dan `GodexRT730`) adalah identitas
printer. Label itu dipakai di log, di perintah `ENABLE`/`DISABLE <label>`, dan
di keluaran `STATUS`. Bebas dinamai apa saja.

| Kunci | Arti |
|---|---|
| `Name` | Nama printer Windows, atau share dengan format `\\localhost\<NamaShare>`. **Kosongkan untuk memakai printer default Windows.** |
| `Enabled` | `0` = tolak semua job untuk printer ini (service tetap jalan) |
| `Debug` | `1` = log verbose (berlaku untuk semua printer) |
| `Addr` | Alamat listener EZPL. `0.0.0.0` = terima dari seluruh jaringan; isi IP LAN tertentu untuk membatasi |
| `Port` | Port EZPL printer ini |

### 2.2 Satu printer (format `Godex.exe` lama)

Format lama **tetap jalan apa adanya** — `Godex.ini` yang sudah ada di lapangan
tidak perlu diubah:

```ini
[Printer]
Name=\\localhost\GodexG500
Enabled=1
Debug=0

[Prn]
Addr=0.0.0.0
Port=9100
```

Aturannya: kalau ada section `[Printer:<label>]`, bentuk itulah yang dipakai dan
`[Printer]`/`[Prn]` diabaikan (dicatat sebagai peringatan di log). Kalau tidak
ada, bentuk lama yang dipakai.

### 2.3 Di mana file `.ini` dicari

Urutannya:

1. path dari `-c <path>`
2. `%ALLUSERSPROFILE%\Godex\Godex.ini` → biasanya `C:\ProgramData\Godex\Godex.ini`
3. `Godex.ini` di sebelah `godex_bridge.exe`

> **Perhatikan:** kalau `C:\ProgramData\Godex\Godex.ini` ada, **file itu** yang
> dipakai, bukan yang di sebelah `.exe`. Ini disengaja supaya perilakunya sama
> dengan `Godex.exe` — jadi kalau suntingan Anda tidak berefek, cek file itu.

---

## 3. Menjalankan sebagai service Windows

`pywin32` sudah ikut dibundel di dalam `.exe`, jadi:

```
godex_bridge.exe install --startup auto
godex_bridge.exe start
godex_bridge.exe stop
godex_bridge.exe remove
```

Service akan otomatis start saat Windows menyala.

> **Nama service default adalah `Godex`** — sama dengan nama service yang
> dipasang `Godex.exe` asli. Kalau keduanya ada di satu mesin, hentikan dan
> lepas service lama dulu, **atau** pakai nama lain:
>
> ```
> set GODEX_SERVICE_NAME=GodexBridge
> godex_bridge.exe install --startup auto
> ```

Alternatif kalau tidak ingin memakai pywin32: bungkus `godex_bridge.exe` dengan
[NSSM](https://nssm.cc/) — NSSM yang mengurus start/stop/restart-on-crash.

---

## 4. Perintah

| Perintah | Guna |
|---|---|
| `--list-printers` | Daftar printer Windows yang terpasang beserta share-nya, lalu keluar |
| `--self-test` | Baca config, buka printer, bind port, lalu keluar |
| `--dry-run DIR` | **Jangan cetak** — tulis tiap job ke `DIR\<label>-<waktu>.bin` |
| `-c PATH` | Pakai file `.ini` lain |
| `--printer NAME` | Override nama printer pertama |
| `--prn-addr` / `--prn-port` | Override alamat/port listener printer pertama |
| `--cmd-addr` / `--cmd-port` | Override kanal kontrol |
| `--log-addr` / `--log-port` | Override kanal log |
| `--no-cmd` / `--no-log` | Matikan listener kontrol / log |
| `--log-dir DIR` | Folder log (default `%ALLUSERSPROFILE%\Godex\Logs`) |
| `--line-limit N` | Batas baris per job, `0` = tanpa batas (default 20000) |
| `--idle-flush N` | Job dianggap selesai setelah N detik diam — untuk client yang menahan koneksi (default `0` = tunggu disconnect) |
| `--debug` | Log verbose |
| `--no-console` | Jangan cetak log ke konsol |

`--dry-run` adalah cara paling aman menguji tanpa printer: byte yang diterima
ditulis ke file, jadi bisa dibandingkan byte-per-byte dengan yang dikirim.

---

## 5. Kanal kontrol (port `Cmd`)

Line-based, tanpa banner. Cocok dites dengan `telnet` atau `nc`.

| Perintah | Guna |
|---|---|
| `QUIT` | Balas `Closing`, lalu tutup koneksi |
| `STOP` | Hentikan service |
| `STATUS` | Statistik JSON: job sukses/gagal, byte masuk, uptime — per printer |
| `ENABLE` / `DISABLE` | Saklar utama: semua printer |
| `ENABLE <label>` / `DISABLE <label>` | Saklar satu printer saja |
| `PRINTER` | Daftar printer + endpoint-nya |
| `HELP` | Daftar perintah |

Contoh `STATUS` dengan dua printer:

```json
{
  "enabled": true,
  "uptime_s": 3610,
  "line_limit": 20000,
  "printers": [
    {"label": "GodexG500", "name": "Godex G500", "prn": "0.0.0.0:9100",
     "enabled": true, "jobs_ok": 128, "jobs_failed": 0, "bytes_in": 40960},
    {"label": "GodexRT730", "name": "Godex RT730", "prn": "0.0.0.0:9101",
     "enabled": true, "jobs_ok": 57, "jobs_failed": 1, "bytes_in": 18240}
  ]
}
```

`DISABLE <label>` berguna saat satu printer sedang rusak: printer lain tetap
melayani, dan job ke printer yang bermasalah dibuang dengan cepat alih-alih
menumpuk di antrean.

---

## 6. Log

| | |
|---|---|
| Lokasi | `%ALLUSERSPROFILE%\Godex\Logs\` (biasanya `C:\ProgramData\Godex\Logs\`) |
| Nama file | `Godex LOG YYYY-MM-DD.txt` — satu file per hari |
| Format baris | `HH:MM:SS pesan` (tanpa tanggal di baris) |

Formatnya sengaja disamakan dengan `Godex.exe` supaya pembaca log yang sudah ada
tetap bekerja.

Port `Log` (default `60000`) me-replay seluruh isi log hari itu ke client yang
baru connect, lalu melanjutkan streaming live — jadi client langsung dapat
konteks tanpa membaca file.

---

## 7. Troubleshooting

| Gejala | Sebab dan tindakan |
|---|---|
| `OpenPrinter gagal ... WinError 1801` | Nama printer atau share salah. Jalankan `--list-printers` dan salin namanya apa adanya. `\\localhost\Share` hanya resolve kalau share itu benar-benar ada — `GodexG500` dan `Godex G500` (pakai spasi) adalah dua nama berbeda. |
| `WinError 5` | Tidak punya izin, atau printer itu koneksi milik user lain. Service berjalan sebagai akun berbeda (SYSTEM punya daftar printer sendiri). |
| bind gagal, `WinError 10048` | Port sudah dipakai proses lain. Cek dengan `netstat -ano`. Kalau `Godex.exe` lama masih jalan, hentikan dulu service-nya. |
| bind gagal, `WinError 10013` | Port masuk *excluded port range* Windows (Hyper-V/WinNAT). Cek `netsh int ipv4 show excludedportrange protocol=tcp`, lalu ganti port. |
| Service tidak mau start | Jalankan `.exe`-nya langsung dari Command Prompt untuk melihat pesannya. Cek juga apakah port sudah dipakai proses lain. |
| Job masuk tapi kertas tidak keluar | Uji dengan `--dry-run`. Kalau file `.bin` berisi byte yang benar, masalahnya di printer/driver, bukan di bridge. |
| Halaman keluar kosong | Printer menerima byte non-RAW sehingga dirender driver. Pastikan memakai antrean printer yang benar (bukan "Microsoft Print to PDF" atau printer virtual). |
| Satu label tercetak jadi banyak job | Client mengirim per baris, atau koneksinya dibuka-tutup berulang. Satu koneksi = satu job. |
| `Printer line limit (N) exceeded` | Job melebihi `--line-limit`. Naikkan batasnya, atau cek apakah client mengirim sampah. |
| Suntingan `Godex.ini` tidak berefek | Yang dibaca mungkin `C:\ProgramData\Godex\Godex.ini`. Lihat bagian 2.3. |

---

## 8. Build dari source

Butuh Python 3.9+.

```bash
python -m pip install -r requirements-build.txt
python build.py
```

> **Jangan jalankan `python -m build` di folder ini.** `build.py` ada di root,
> dan `sys.path[0]` saat memakai `-m` adalah folder kerja — jadi
> `python -m build` menjalankan `build.py` milik proyek ini, **bukan** tool
> `build` dari PyPI. Menjalankan `python -m pip install build` lebih dulu tidak
> menolong: folder kerja tetap diperiksa lebih dulu daripada site-packages.
> Lagi pula ini bukan paket Python, jadi tidak ada yang perlu di-`build`.

Hasilnya:

```
dist/godex_bridge.exe     aplikasi jadi (satu file)
dist/Godex.ini            contoh konfigurasi
```

Menjalankan langsung dari source (tanpa build):

```bash
python godex_bridge.py --self-test
python tests/test_smoke.py        # smoke test end-to-end, pakai --dry-run
```

`tests/test_smoke.py` memilih port bebas secara dinamis dan selalu memakai
`--dry-run`, jadi aman dijalankan di mesin yang service lamanya masih memegang
9100/50000/60000.

---

## 9. Struktur kode

```
godex_bridge.py        entry point: pilih jalur service atau aplikasi biasa
build.py               skrip build .exe
godex/
  paths.py             lokasi .ini dan folder log
  config.py            pembacaan Godex.ini (satu atau banyak printer)
  logstream.py         log file + konsol + streaming ke client
  printing.py          kirim job RAW ke spooler Windows (ctypes) + --list-printers
  servers.py           State, BridgeServer, handler Prn/Log/Cmd
  app.py               CLI, wiring, self-test
  service.py           mode Windows service (pywin32)
tests/test_smoke.py    smoke test end-to-end
requirements-build.txt dependensi build + lint (dipakai CI)
.github/workflows/     ci.yml (lint + smoke test + build .exe) dan release.yml
```

Beberapa keputusan yang sengaja diambil — jangan "diperbaiki" tanpa alasan:

- **Satu koneksi Prn = satu job.** Byte ditampung sampai client menutup koneksi,
  baru dicetak sebagai satu job. Mencetak per baris menghasilkan satu job spooler
  per baris label.
- **`SO_EXCLUSIVEADDRUSE` di Windows.** `SO_REUSEADDR` di Windows mengizinkan
  proses lain ikut bind ke port yang sudah dipakai, sehingga dua instance bisa
  sama-sama "berhasil" dan job tercampur tanpa error. Karena itu instance kedua
  dibuat gagal keras.
- **Printer yang gagal bind = service berhenti** (exit code 1), bukan jalan
  setengah hidup dan diam-diam menolak semua job.
- **Format log, path `.ini`, dan kosakata perintah `Cmd` disamakan dengan
  `Godex.exe` asli.** Pembaca log dan tooling hilir bergantung padanya.
