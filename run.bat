@echo off
chcp 65001 >nul
title Every Proxy Switcher
cd /d "%~dp0"

echo =======================================================
echo  Запуск Every Proxy Switcher (Windows 11 Fluent Dark)
echo =======================================================

python proxy_switcher.py
pause

