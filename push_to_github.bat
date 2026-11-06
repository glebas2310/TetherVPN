@echo off
title Push TetherVPN to GitHub
cd /d "%~dp0"

echo ===================================================
echo   Pushing TetherVPN to GitHub...
echo ===================================================
echo.

echo [*] Resetting stale git proxies...
git config --global --unset http.proxy 2>nul
git config --global --unset https.proxy 2>nul

echo [*] Setting git author identity...
git config user.name "glebas2310"
git config user.email "glebas2310@users.noreply.github.com"

echo [*] Initializing git...
git init
git branch -M main
git remote remove origin 2>nul
git remote add origin https://github.com/glebas2310/TetherVPN.git

echo [*] Adding files...
git add .
git commit -m "TetherVPN: Two-mode APK (Server & Client) and updated PC app" 2>nul

echo.
echo [*] Uploading to https://github.com/glebas2310/TetherVPN.git ...
echo (If a browser window opens, please sign in to GitHub to authorize)
echo.
git push -u origin main --force

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ===================================================
    echo [SUCCESS] Pushed to GitHub successfully!
    echo Check APK build progress at:
    echo https://github.com/glebas2310/TetherVPN/actions
    echo ===================================================
) else (
    echo.
    echo ===================================================
    echo [ERROR] Push failed. Check the error message above.
    echo ===================================================
)

echo.
pause
