@echo off
chcp 65001 >nul
title Сборка Every Proxy Switcher в .EXE
cd /d "%~dp0"

echo =======================================================
echo  Сборка Every Proxy Switcher в .EXE (PyInstaller)
echo =======================================================

echo  Запуск PyInstaller...
python build_exe.py
pause

