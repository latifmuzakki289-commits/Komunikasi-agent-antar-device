@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
title chatbrain relay2  [127.0.0.1:20232]

set "PORT=20232"
cd /d "%~dp0"

echo ==================================================
echo   chatbrain relay2   -^>  http://127.0.0.1:%PORT%/v1
echo   folder : %~dp0
echo   log    : relay2.log
echo ==================================================
echo.

where python >nul 2>&1
if errorlevel 1 goto nopython

echo [ok] memulai relay2...
echo      window ini akan diam - itu normal, relay menulis ke relay2.log
echo      tekan Ctrl+C untuk berhenti
echo.

rem Guard port ada di dalam relay2.py (_port_busy). Kalau port sudah dipakai,
rem python keluar dengan kode 1 dan pesannya tampil di bawah ini.
python -u relay2.py
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="1" (
  echo [X] relay2 gagal start. Kalau di atas tertulis "port sudah dipakai",
  echo     berarti masih ada instance lama. Cari PID-nya:
  echo         netstat -ano ^| findstr :%PORT%
  echo     lalu hentikan:
  echo         Stop-Process -Id ^<pid^> -Force
) else (
  echo [relay2 berhenti - exit code %RC%]
)
echo.
pause
exit /b %RC%

:nopython
echo [X] python tidak ditemukan di PATH.
echo.
pause
exit /b 1
