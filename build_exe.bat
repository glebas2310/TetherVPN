@echo off
chcp 65001 >nul
title Сборка TetherVPN в .EXE
cd /d "%~dp0"

echo =======================================================
echo  Сборка TetherVPN в .EXE (PyInstaller)
echo =======================================================

echo  Запуск PyInstaller...
python build_exe.py
pause

