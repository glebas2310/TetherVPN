@echo off
chcp 65001 >nul
title TetherVPN
cd /d "%~dp0"

echo =======================================================
echo  Запуск TetherVPN (Windows 11 Fluent Dark)
echo =======================================================

python proxy_switcher.py
pause

