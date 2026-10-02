"""Port Python dari Godex.exe: TCP print bridge ke printer Windows (RAW/EZPL).

Isi paket:

    paths       lokasi .ini dan folder log
    config      pembacaan Godex.ini (satu printer atau banyak printer)
    logstream   log file + streaming ke client port Log
    printing    kirim job RAW ke spooler Windows lewat ctypes
    servers     listener Prn / Cmd / Log
    app         CLI, wiring, dan self-test
    service     mode Windows service (opsional, butuh pywin32)
"""

__version__ = "2.0.0"

__all__ = ["__version__"]
