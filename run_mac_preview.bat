@echo off
chcp 65001 >nul
title TetherVPN — macOS Client (Windows Preview)
cd /d "%~dp0"
echo ========================================================
echo   TetherVPN — macOS Client (Режим предпросмотра)
echo ========================================================
echo.
echo Запуск интерактивного интерфейса macOS-клиента...
python proxy_switcher_mac.py
if errorlevel 1 (
    echo.
    echo [!] Проверка и установка customtkinter...
    pip install customtkinter
    python proxy_switcher_mac.py
)
