@echo off
chcp 65001 >nul
title TetherVPN - Отправка на GitHub для сборки APK
cd /d "%~dp0"

echo =====================================================================
echo  TetherVPN: Автоматическая отправка проекта в GitHub
echo  После отправки GitHub Actions автоматически соберет готовый APK!
echo =====================================================================
echo.

set DEFAULT_URL=https://github.com/glebas2310/TetherVPN.git
echo По умолчанию: %DEFAULT_URL%
set /p REPO_URL="Вставьте ссылку на репозиторий (или нажмите Enter для значения по умолчанию): "

if "%REPO_URL%"=="" (
    set REPO_URL=%DEFAULT_URL%
)

echo.
echo [*] Инициализация Git...
if not exist ".git" (
    git init
    git branch -M main
)

echo [*] Добавление файлов проекта...
git add .
git commit -m "TetherVPN: Minimal HTTP proxy for Samsung Galaxy S23"

echo [*] Настройка удаленного репозитория...
git remote remove origin 2>nul
git remote add origin %REPO_URL%

echo [*] Отправка в GitHub (git push)...
git push -u origin main --force

echo.
echo =====================================================================
echo [OK] Проект TetherVPN успешно отправлен в GitHub!
echo.
echo Перейдите во вкладку Actions в вашем репозитории:
echo %REPO_URL%/actions
echo.
echo GitHub уже запустил сборку: [Build TetherVPN APK]
echo Через ~1.5-2 минуты готовый APK появится в разделе Artifacts!
echo =====================================================================
pause
