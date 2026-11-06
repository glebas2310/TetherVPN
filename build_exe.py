"""
Скрипт автоматической сборки TetherVPN в один исполняемый файл (.exe)
с помощью PyInstaller.

Параметры сборки:
- --onefile: один автономный .exe
- --windowed: без фонового черного окна консоли (GUI Tkinter)
- --uac-admin: встраивание UAC манифеста с запросом прав Администратора при запуске
- --icon: фирменная иконка приложения app_icon.ico
"""

import os
import subprocess
import sys

# Обеспечиваем безопасный вывод в консоль Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def ensure_pyinstaller() -> bool:
    """Проверяет наличие PyInstaller и устанавливает его при необходимости."""
    try:
        import PyInstaller  # noqa: F401
        print("[OK] PyInstaller уже установлен в системе.")
        return True
    except ImportError:
        print("[!] PyInstaller не найден. Запуск автоматической установки через pip...")
        ret = subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"])
        return ret.returncode == 0


def build_executable():
    """Запускает компиляцию проекта в единый .exe файл."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    main_script = os.path.join(base_dir, "proxy_switcher.py")
    icon_file = os.path.join(base_dir, "app_icon.ico")

    if not os.path.exists(main_script):
        print(f"[ERROR] Файл {main_script} не найден!")
        sys.exit(1)

    print("\n" + "=" * 60)
    print(" [*] СБОРКА TETHERVPN В .EXE (PYINSTALLER)")
    print("=" * 60)

    if not ensure_pyinstaller():
        print("[X] Не удалось установить PyInstaller. Проверьте подключение к интернету.")
        sys.exit(1)

    # Формируем команду для PyInstaller
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--windowed",
        "--name",
        "TetherVPN",
        "--uac-admin",  # Автоматический запрос прав администратора при запуске .exe
        "--collect-all",
        "customtkinter",  # Встраивание всех json-тем и ассетов CustomTkinter
        "--clean",
    ]

    if os.path.exists(icon_file):
        cmd.extend(["--icon", icon_file])
        print(f"[OK] Встраивание иконки: {icon_file}")

    cmd.append(main_script)

    print(f"\n[i] Выполнение команды:\n{' '.join(cmd)}\n")
    proc = subprocess.run(cmd, cwd=base_dir)

    if proc.returncode == 0:
        exe_path = os.path.join(base_dir, "dist", "TetherVPN.exe")
        print("\n" + "=" * 60)
        print(" [OK] СБОРКА УСПЕШНО ЗАВЕРШЕНА!")
        print(f" Готовый файл: {exe_path}")
        print("=" * 60)
        print("\nВы можете запускать TetherVPN.exe на любом компьютере с Windows 10/11.")
    else:
        print(f"\n[X] Сборка завершилась с ошибкой (код {proc.returncode}).")
        sys.exit(proc.returncode)


if __name__ == "__main__":
    build_executable()
