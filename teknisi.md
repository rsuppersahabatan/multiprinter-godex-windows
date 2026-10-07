## Cara Menjalankan Di Windows

```bash
godex_bridge.exe --list-printers      # 1. cari nama printer yang benar
notepad Godex.ini                     # 2. isi nama printer + port
godex_bridge.exe --self-test          # 3. pastikan config, printer, port siap
godex_bridge.exe                      # 4. jalankan
```

`--self-test` itu langkah yang paling sering menyelamatkan: ia membaca config, membuka tiap printer (buka-tutup, **tidak mencetak**), dan bind semua port. Kalau ada yang salah, ia bilang yang mana.

### Mematikan dan melepas `Godex.exe` lama

`Godex.exe` asli terpasang sebagai service Windows dan **otomatis start saat boot**. Selama service itu masih aktif, port 9100/50000/60000 diduduki dan bridge baru tidak bisa bind. Self-test akan melaporkan `WinError 10048` di ketiga port.

> **Semua perintah di section ini butuh Command Prompt / PowerShell admin.**
> Buka: Win + X → "Terminal (Admin)" atau "Command Prompt (Admin)".

#### Langkah 1 — cek apakah `Godex.exe` sedang jalan

```cmd
tasklist | findstr /i Godex
```

Atau cek apakah portnya sibuk:

```cmd
netstat -ano | findstr ":9100 :50000 :60000"
```

Kalau ada PID yang LISTENING, catat PID-nya.

#### Langkah 2 — cek di mana binary service lama

Service "Godex" bisa terdaftar ke binary mana saja. Cek path-nya:

```cmd
sc qc Godex
```

Atau dari PowerShell:

```powershell
(Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\Godex").ImagePath
```

Output-nya biasanya path ke `Godex.exe` lama (mis. `C:\Users\...\Downloads\Godex.exe`). **Ini penting**: kalau service masih terdaftar ke binary lama, `godex_bridge.exe start` hanya akan menjalankan binary lama, bukan bridge yang baru.

#### Langkah 3 — hentikan service

```cmd
sc stop Godex
```

Atau paksa hentikan process-nya kalau service tidak merespons:

```cmd
taskkill /PID <pid-dari-langkah-1> /F
```

#### Langkah 4 — lepas service lama

```cmd
sc delete Godex
```

Ini menghapus registrasi service dari Service Control Manager. Setelah ini, `Godex.exe` tidak akan start otomatis saat boot.

#### Langkah 5 — hapus binary lama (opsional)

Kalau tidak mau `Godex.exe` lama bersisa di disk:

```cmd
del "C:\Users\asus\Downloads\Godex.exe"
```

Sesuaikan path dengan yang ditemukan di Langkah 2.

#### Langkah 6 — tangani config lama

`C:\ProgramData\Godex\Godex.ini` **diutamakan** daripada `Godex.ini` di sebelah `.exe`. Kalau service lama sudah dilepas, file ini masih akan terbaca. Dua opsi:

- **Opsi A — edit file itu** supaya isinya benar (nama printer langsung, bukan UNC path):

  ```cmd
  notepad "C:\ProgramData\Godex\Godex.ini"
  ```

- **Opsi B — hapus file itu** supaya `Godex.ini` di sebelah `.exe` terbaca otomatis:

  ```cmd
  del "C:\ProgramData\Godex\Godex.ini"
  ```

#### Langkah 7 — verifikasi port bebas

```cmd
netstat -ano | findstr ":9100 :9101 :50000 :60000"
```

Kalau tidak ada output, port bebas.

#### Langkah 8 — install service baru (opsional)

Setelah service lama bersih, install bridge baru:

```cmd
cd /d D:\PYTHON-DEV\multiprinter-godex-windows\dist

godex_bridge.exe install --startup auto
godex_bridge.exe start
```

Verifikasi:

```cmd
godex_bridge.exe --self-test
```

Kalau semua port bebas dan printer terbuka, output-nya: `self-test OK - config, printer, dan port semuanya siap`.

### Jalankan sebagai Windows Service

`pywin32` sudah ikut dibundel di `.exe`, jadi:

```bash
godex_bridge.exe install --startup auto
godex_bridge.exe start
godex_bridge.exe stop
godex_bridge.exe remove
```

Service otomatis start saat Windows menyala. **Nama service default `Godex`** — sama dengan service `Godex.exe` asli. Kalau keduanya ada di satu mesin, hentikan service lama dulu, atau pakai nama lain:

```bash
set GODEX_SERVICE_NAME=GodexBridge
godex_bridge.exe install --startup auto
```

## Konfigurasi `Godex.ini`

**Bentuk banyak printer (disarankan):**

```ini
[Printer:GodexG500]
Name=GodexG500
Enabled=1
Addr=0.0.0.0
Port=9100

[Printer:GodexRT730]
Name=\\localhost\GodexRT730
Enabled=1
Addr=0.0.0.0
Port=9101

[Cmd]
Addr=127.0.0.1
Port=50000

[Log]
Addr=127.0.0.1
Port=60000
```

Aplikasi pengirim memilih printer dengan **mengganti port**: `9100` → printer pertama, `9101` → printer kedua.

**Bentuk satu printer (format `Godex.exe` lama)** tetap jalan apa adanya — `[Printer]` + `[Prn]` tanpa label.

**Urutan pencarian `.ini`:**

1. path dari `-c <path>`
2. `C:\ProgramData\Godex\Godex.ini`
3. `Godex.ini` di sebelah `.exe`

> ⚠ Kalau `C:\ProgramData\Godex\Godex.ini` ada, **file itu yang dipakai**, bukan yang di sebelah `.exe`. Ini disengaja supaya sama dengan `Godex.exe`. Kalau suntingan Anda tidak berefek, cek file itu.

## Perintah kontrol (port Cmd, default 50000)

Line-based, tanpa banner. Bisa dites dengan `telnet` atau `nc`:

| Perintah | Guna |
|---|---|
| `STATUS` | Statistik JSON: job sukses/gagal, byte masuk, uptime — per printer |
| `ENABLE` / `DISABLE` | Saklar utama (semua printer) |
| `ENABLE <label>` / `DISABLE <label>` | Saklar satu printer saja |
| `PRINTER` | Daftar printer + endpoint |
| `STOP` | Hentikan service |

`DISABLE <label>` berguna saat satu printer rusak: printer lain tetap melayani, job ke yang bermasalah dibuang cepat.

## Troubleshooting

### `WinError 1801: The printer name is invalid`

Nama printer di `Godex.ini` tidak dikenali spooler. Jalankan `--list-printers` dan salin nama printer apa adanya.

**Catatan penting soal UNC path:** pada beberapa mesin, `\\localhost\<share>` tidak resolve untuk `OpenPrinterW` walau share-nya terdaftar di `--list-printers`. Solusinya: pakai nama printer langsung (tanpa `\\localhost\`).

```ini
; BISA gagal:
Name=\\localhost\GodexG500

; Biasanya aman:
Name=GodexG500
```

### `WinError 10048: port sudah dipakai`

Port 9100/50000/60000 sudah diduduki proses lain — kemungkinan `Godex.exe` lama masih jalan. Lihat section "Melepas service `Godex.exe` lama" di atas.

### Suntingan `Godex.ini` tidak berefek

Yang dibaca mungkin `C:\ProgramData\Godex\Godex.ini` (diutamakan daripada yang di sebelah `.exe`). Lihat urutan pencarian `.ini` di bagian konfigurasi.