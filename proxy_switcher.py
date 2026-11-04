"""
Every Proxy Switcher — Редизайн интерфейса (Windows 11 Fluent Dark на CustomTkinter).
Утилита автоматизации сетевых настроек Windows 10/11 для работы через Every Proxy
и быстрого возврата к домашнему интернету.

Сохранена вся архитектура и системная логика:
- Проверка и повышение привилегий UAC
- Системный прокси (реестр Internet Settings + WinInet API)
- Системный WinHTTP прокси (netsh winhttp)
- Пользовательские переменные окружения (setx + HKCU\\Environment + WM_SETTINGCHANGE)
- Глобальный прокси Git (git config --global)
- Маскировка раздачи/тетеринга (Hop Limit TTL=65 / 128)
- Очистка кэша DNS (ipconfig /flushdns)
- ICMP Ping и проверка TCP-порта Every Proxy
"""

from __future__ import annotations

import argparse
import ctypes
import ipaddress
import os
import re
import shutil
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import winreg
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Callable, Optional

# Безопасный вывод в консоль Windows при любых кодировках
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ------------------------------------------------------------------------------
# Проверка наличия CustomTkinter с базовым fallback на Tkinter
# ------------------------------------------------------------------------------
try:
    import customtkinter as ctk
    CUSTOMTKINTER_AVAILABLE = True
except ImportError:
    ctk = None
    CUSTOMTKINTER_AVAILABLE = False

try:
    import tkinter as tk
    from tkinter import messagebox, simpledialog, ttk
    from tkinter.scrolledtext import ScrolledText
    TKINTER_AVAILABLE = True
except ImportError:
    TKINTER_AVAILABLE = False


# ==============================================================================
# 1. ПРОВЕРКА И ПОВЫШЕНИЕ ПРАВ АДМИНИСТРАТОРА (UAC)
# ==============================================================================

def is_admin() -> bool:
    """Проверяет, запущен ли текущий процесс с правами администратора Windows."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def elevate_privileges() -> bool:
    """
    Перезапускает скрипт или скомпилированный .exe с повышением прав через диалог UAC.
    Возвращает True, если процесс был запущен, и завершает текущий процесс.
    """
    if is_admin():
        return True

    try:
        if getattr(sys, "frozen", False):
            # Запуск из PyInstaller .exe
            executable = sys.executable
            params = " ".join([f'"{arg}"' for arg in sys.argv[1:]])
        else:
            # Запуск из исходного Python-скрипта
            executable = sys.executable
            script_args = [os.path.abspath(sys.argv[0])] + sys.argv[1:]
            params = " ".join([f'"{arg}"' for arg in script_args])

        # SW_SHOWNORMAL = 1
        ret = ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            executable,
            params,
            None,
            1
        )
        if ret > 32:
            sys.exit(0)
        else:
            return False
    except Exception as exc:
        print(f"[!] Ошибка запроса прав администратора: {exc}")
        return False


# ==============================================================================
# 2. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И СЕТЕВЫЕ ОПЕРАЦИИ
# ==============================================================================

def decode_bytes(data: bytes) -> str:
    """Безопасно декодирует байты вывода консоли Windows (UTF-8, OEM cp866, ANSI cp1251)."""
    if not data:
        return ""
    for enc in ("utf-8", "cp866", "cp1251"):
        try:
            return data.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return data.decode("latin1", errors="replace").strip()


def run_command(cmd: list[str], timeout: float = 12.0) -> tuple[int, str, str]:
    """
    Выполняет системную команду без всплывающего черного окна CMD
    и безопасно возвращает (код_возврата, stdout, stderr).
    """
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            creationflags=flags
        )
        stdout = decode_bytes(proc.stdout)
        stderr = decode_bytes(proc.stderr)
        return proc.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Превышено время ожидания команды (таймаут)"
    except Exception as exc:
        return -1, "", str(exc)


def refresh_wininet() -> None:
    """
    Принудительно обновляет сетевые настройки WinInet в памяти Windows,
    чтобы браузеры мгновенно переключились на прокси без перезагрузки.
    """
    try:
        INTERNET_OPTION_SETTINGS_CHANGED = 39
        INTERNET_OPTION_REFRESH = 37
        ctypes.windll.wininet.InternetSetOptionW(0, INTERNET_OPTION_SETTINGS_CHANGED, 0, 0)
        ctypes.windll.wininet.InternetSetOptionW(0, INTERNET_OPTION_REFRESH, 0, 0)
    except Exception:
        pass


def broadcast_environment_change() -> None:
    """
    Отправляет системное сообщение WM_SETTINGCHANGE ('Environment'),
    чтобы службы и новые терминалы сразу увидели обновленные переменные.
    """
    HWND_BROADCAST = 0xFFFF
    WM_SETTINGCHANGE = 0x001A
    SMTO_ABORTIFHUNG = 0x0002
    result = ctypes.c_ulong()
    try:
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST,
            WM_SETTINGCHANGE,
            0,
            "Environment",
            SMTO_ABORTIFHUNG,
            3000,
            ctypes.byref(result)
        )
    except Exception:
        pass


def is_valid_ipv4(ip: str) -> bool:
    """Проверяет валидность IPv4 адреса."""
    try:
        ipaddress.IPv4Address(ip.strip())
        return True
    except (ValueError, AttributeError):
        return False


def is_valid_port(port: int | str) -> bool:
    """Проверяет валидность сетевого порта (1..65535)."""
    try:
        p = int(port)
        return 1 <= p <= 65535
    except (ValueError, TypeError):
        return False


def detect_network_gateways() -> list[str]:
    """
    Определяет IP-адреса шлюзов (Gateway) активных сетевых адаптеров.
    При раздаче со смартфона шлюз обычно равен IP телефона (например, 192.168.43.1).
    """
    gateways: list[str] = []
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        proc = subprocess.run(["ipconfig"], capture_output=True, creationflags=flags)
        out = decode_bytes(proc.stdout)
        matches = re.findall(
            r"(?:Основной шлюз|Default Gateway)[ .:]+([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)",
            out,
            re.IGNORECASE
        )
        for g in matches:
            if g not in gateways and g != "0.0.0.0":
                gateways.append(g)
    except Exception:
        pass
    return gateways


def get_local_ips() -> set[str]:
    """Возвращает набор локальных IP-адресов текущего компьютера."""
    ips = set()
    try:
        for item in socket.gethostbyname_ex(socket.gethostname())[2]:
            ips.add(item)
    except Exception:
        pass
    return ips


def get_candidate_ips(current_ip: str = "") -> list[tuple[str, str]]:
    """
    Формирует список потенциальных IP-адресов телефона с Every Proxy:
    1. Текущий введенный IP (если есть).
    2. Все основные шлюзы активных адаптеров (ipconfig).
    3. Стандартные шлюзы точек доступа: 192.168.43.1 (Android), 172.20.10.1 (iOS), 192.168.42.129 (USB).
    4. Активные соседи локальной сети из ARP-таблицы.
    """
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    local_ips = get_local_ips()

    # 1. Текущий ввод пользователя
    c_ip = current_ip.strip()
    if c_ip and is_valid_ipv4(c_ip) and c_ip not in local_ips:
        seen.add(c_ip)
        candidates.append((c_ip, "Текущий ввод"))

    # 2. Основные шлюзы системы (Android hotspot / Wi-Fi роутер / USB)
    for gw in detect_network_gateways():
        if gw not in seen and gw not in local_ips:
            seen.add(gw)
            candidates.append((gw, "Сетевой шлюз"))

    # 3. Общеизвестные шлюзы мобильной раздачи
    well_known = [
        ("192.168.43.1", "Android Wi-Fi Hotspot"),
        ("172.20.10.1", "iOS Точка доступа"),
        ("192.168.42.129", "Android USB-модем"),
        ("192.168.44.1", "Bluetooth модем"),
    ]
    for wk_ip, desc in well_known:
        if wk_ip not in seen and wk_ip not in local_ips:
            seen.add(wk_ip)
            candidates.append((wk_ip, desc))

    # 4. Активные IP-адреса из ARP-таблицы
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        arp_proc = subprocess.run(["arp", "-a"], capture_output=True, creationflags=flags)
        arp_out = decode_bytes(arp_proc.stdout)
        arp_ips = re.findall(r"([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", arp_out)
        for a_ip in arp_ips:
            if a_ip in seen or a_ip in local_ips:
                continue
            try:
                ip_obj = ipaddress.IPv4Address(a_ip)
                if ip_obj.is_private and not ip_obj.is_multicast and not ip_obj.is_loopback:
                    if not a_ip.endswith(".255") and not a_ip.endswith(".0"):
                        seen.add(a_ip)
                        candidates.append((a_ip, "Локальный узел (ARP)"))
            except Exception:
                pass
    except Exception:
        pass

    return candidates


def check_proxy_and_vpn(ip: str, port: int = 8080, timeout: float = 2.5) -> dict:
    """
    Глубокая сквозная проверка прокси-сервера и мобильного VPN:
    1. TCP-сокет к IP:PORT (проверка, запущен ли Every Proxy).
    2. Пробный HTTP-запрос через прокси (проверка, пропускает ли VPN интернет-пакеты).
    3. Определение внешнего IP и геолокации VPN (страна, город).
    """
    result = {
        "ip": ip,
        "port": port,
        "port_open": False,
        "vpn_working": False,
        "latency_ms": None,
        "external_ip": None,
        "location": None,
        "status": "UNREACHABLE",  # "OK_VPN" | "PORT_OPEN_NO_VPN" | "UNREACHABLE"
        "message": ""
    }

    # 1. Проверка открытия TCP-порта
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.4)
        c_code = sock.connect_ex((ip, port))
        sock.close()
        if c_code != 0:
            result["status"] = "UNREACHABLE"
            result["message"] = f"Порт {port} закрыт (Every Proxy выключен на {ip})"
            return result
    except Exception as exc:
        result["status"] = "UNREACHABLE"
        result["message"] = f"Хост недоступен: {exc}"
        return result

    result["port_open"] = True

    # 2. Проверка сквозного интернета через Every Proxy (работоспособность VPN)
    proxy_url = f"http://{ip}:{port}"
    handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    opener = urllib.request.build_opener(handler)

    t0 = time.time()
    vpn_ok = False
    test_urls = [
        "http://cp.cloudflare.com/generate_204",
        "http://connectivitycheck.gstatic.com/generate_204"
    ]

    for test_url in test_urls:
        try:
            req = urllib.request.Request(
                test_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            )
            with opener.open(req, timeout=timeout) as resp:
                if resp.status in (200, 204):
                    vpn_ok = True
                    result["latency_ms"] = int((time.time() - t0) * 1000)
                    break
        except Exception:
            continue

    if vpn_ok:
        result["vpn_working"] = True
        result["status"] = "OK_VPN"

        # Получаем внешний IP через прокси
        ext_ip = None
        for ip_api in ("http://icanhazip.com", "http://ifconfig.me/ip"):
            try:
                req_ip = urllib.request.Request(ip_api, headers={"User-Agent": "curl/7.84.0"})
                with opener.open(req_ip, timeout=1.8) as r_ip:
                    val = r_ip.read().decode("utf-8", errors="ignore").strip()
                    if is_valid_ipv4(val):
                        ext_ip = val
                        break
            except Exception:
                continue

        result["external_ip"] = ext_ip

        # Определение страны/города VPN через ip-api
        if ext_ip:
            try:
                geo_url = f"http://ip-api.com/json/{ext_ip}?lang=ru"
                req_geo = urllib.request.Request(geo_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req_geo, timeout=1.8) as r_geo:
                    geo_data = json.loads(r_geo.read().decode("utf-8", errors="ignore"))
                    if geo_data.get("status") == "success":
                        cnt = geo_data.get("country", "")
                        ct = geo_data.get("city", "")
                        result["location"] = f"{cnt}, {ct}" if ct else cnt
            except Exception:
                pass

        loc_desc = f" ({result['location']})" if result["location"] else ""
        ext_desc = f", внешний IP: {result['external_ip']}" if result["external_ip"] else ""
        lat_desc = f", задержка: {result['latency_ms']}мс" if result["latency_ms"] else ""
        result["message"] = f"VPN и Every Proxy работают исправно!{loc_desc}{ext_desc}{lat_desc}"
    else:
        result["status"] = "PORT_OPEN_NO_VPN"
        result["message"] = (
            f"Порт {port} открыт, но интернет-трафик через него НЕ ИДЕТ!\n"
            f"Похоже, что VPN на смартфоне отключен, завис или не настроена раздача трафика."
        )

    return result


def auto_detect_working_proxy(port: int = 8080, current_ip: str = "", log_cb=None) -> tuple[Optional[str], dict]:
    """
    Быстро сканирует все сетевые интерфейсы, шлюзы и узлы ARP в поиске активного Every Proxy.
    Возвращает (лучший_IP, результат_диагностики).
    """
    def log(lvl: str, msg: str):
        if log_cb:
            log_cb(lvl, msg)

    candidates = get_candidate_ips(current_ip)
    if not candidates:
        return None, {"status": "NOT_FOUND", "message": "Сетевые узлы не обнаружены"}

    log("INFO", f"🔍 Автопоиск Every Proxy среди {len(candidates)} потенциальных адресов сети...")

    # Быстрый опрос TCP-порта 8080 параллельно
    open_candidates: list[tuple[str, str]] = []

    def probe_port(cand_tuple):
        cand_ip, desc = cand_tuple
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.35)
            code = s.connect_ex((cand_ip, port))
            s.close()
            if code == 0:
                return cand_ip, desc
        except Exception:
            pass
        return None

    with ThreadPoolExecutor(max_workers=min(12, len(candidates))) as executor:
        port_results = list(executor.map(probe_port, candidates))

    open_candidates = [r for r in port_results if r is not None]

    if not open_candidates:
        log("WARN", f"Every Proxy не обнаружен на порту {port}. Проверьте Wi-Fi и тумблер в приложении.")
        return None, {"status": "NOT_FOUND", "message": f"Порт {port} не отвечает ни на одном узле"}

    log("INFO", f"Найдено узлов с открытым портом {port}: {len(open_candidates)}. Проверка сквозного интернета (VPN)...")

    # Теперь проверяем каждый найденный IP на работоспособность VPN
    best_candidate_ip = None
    best_diag = None

    for cand_ip, desc in open_candidates:
        log("INFO", f"Тестирование узла {cand_ip} ({desc})...")
        diag = check_proxy_and_vpn(cand_ip, port, timeout=2.0)
        if diag["status"] == "OK_VPN":
            # Нашли идеальный IP с работающим VPN!
            best_candidate_ip = cand_ip
            best_diag = diag
            log("OK", f"✅ НАЙДЕН РАБОЧИЙ ПРОКСИ: {cand_ip} ({desc})! {diag['message']}")
            break
        elif diag["status"] == "PORT_OPEN_NO_VPN":
            # Порт открыт, но VPN не работает - запоминаем как резервный вариант
            if not best_candidate_ip:
                best_candidate_ip = cand_ip
                best_diag = diag
            log("WARN", f"⚠️ Узел {cand_ip}: порт открыт, но VPN на смартфоне не пропускает пакеты!")

    if best_candidate_ip and best_diag:
        return best_candidate_ip, best_diag

    # Если ни один не прошел
    first_ip, first_desc = open_candidates[0]
    return first_ip, check_proxy_and_vpn(first_ip, port)


# ==============================================================================
# 3. ОСНОВНОЙ КЛАСС УПРАВЛЕНИЯ СЕТЕВЫМИ НАСТРОЙКАМИ (NETWORK MANAGER)
# ==============================================================================

class NetworkManager:
    """Управление системным прокси, WinHTTP, реестром, Git, TTL и DNS."""

    REG_INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    REG_ENVIRONMENT = r"Environment"

    @classmethod
    def get_system_status(cls) -> dict:
        """Считывает текущее состояние сетевых параметров системы."""
        status = {
            "proxy_enable": False,
            "proxy_server": "",
            "winhttp": "Прямой доступ",
            "env_http": None,
            "env_https": None,
            "git_proxy": None,
            "ttl": 128,
            "is_active": False
        }

        # 1. Системный прокси Windows (HKCU)
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, cls.REG_INTERNET_SETTINGS, 0, winreg.KEY_READ) as k:
                en, _ = winreg.QueryValueEx(k, "ProxyEnable")
                status["proxy_enable"] = bool(en)
                try:
                    srv, _ = winreg.QueryValueEx(k, "ProxyServer")
                    status["proxy_server"] = str(srv).strip()
                except Exception:
                    pass
        except Exception:
            pass

        # 2. WinHTTP прокси
        _, out_wh, _ = run_command(["netsh", "winhttp", "show", "proxy"])
        out_wh_lower = out_wh.lower()
        if "прямой доступ" in out_wh_lower or "direct access" in out_wh_lower:
            status["winhttp"] = "Прямой доступ"
        else:
            m = re.search(r"(?:прокси-сервер\(ы\)|proxy server\(s\))\s*:\s*([^\r\n]+)", out_wh, re.IGNORECASE)
            status["winhttp"] = m.group(1).strip() if m else "Включен"

        # 3. Переменные окружения HKCU\Environment
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, cls.REG_ENVIRONMENT, 0, winreg.KEY_READ) as k:
                try:
                    status["env_http"] = winreg.QueryValueEx(k, "HTTP_PROXY")[0]
                except FileNotFoundError:
                    pass
                try:
                    status["env_https"] = winreg.QueryValueEx(k, "HTTPS_PROXY")[0]
                except FileNotFoundError:
                    pass
        except Exception:
            pass

        # 4. Git глобальный прокси
        if shutil.which("git"):
            code, out_git, _ = run_command(["git", "config", "--global", "--get", "http.proxy"])
            if code == 0 and out_git:
                status["git_proxy"] = out_git.strip()

        # 5. Текущий TTL (Hop Limit) IPv4
        _, out_ttl, _ = run_command(["netsh", "int", "ipv4", "show", "glob"])
        m_ttl = re.search(r"(?:Предел прыжков по умолчанию|Default Hop Limit)\s*:\s*(\d+)", out_ttl, re.IGNORECASE)
        if m_ttl:
            try:
                status["ttl"] = int(m_ttl.group(1))
            except ValueError:
                pass

        # Признак активности любого из компонентов прокси
        status["is_active"] = (
            status["proxy_enable"]
            or (status["winhttp"] != "Прямой доступ")
            or bool(status["env_http"])
            or bool(status["git_proxy"])
            or (status["ttl"] == 65)
        )

        return status

    @classmethod
    def enable_proxy(
        cls,
        ip: str,
        port: int = 8080,
        set_ttl: bool = True,
        check_ping: bool = True,
        log_cb: Optional[Callable[[str, str], None]] = None
    ) -> tuple[bool, str]:
        """РЕЖИМ 1: «Включить проксирование (Every Proxy)»."""
        def log(lvl: str, msg: str):
            if log_cb:
                log_cb(lvl, msg)

        ip = ip.strip()
        if not is_valid_ipv4(ip):
            err_msg = f"Некорректный IP-адрес: '{ip}'"
            log("ERROR", err_msg)
            return False, err_msg

        if not is_valid_port(port):
            err_msg = f"Недопустимый порт: '{port}' (разрешено 1-65535)"
            log("ERROR", err_msg)
            return False, err_msg

        proxy_str = f"{ip}:{port}"
        proxy_url = f"http://{proxy_str}"

        log("INFO", f"▶ Запуск активации прокси: {proxy_str}")

        # 1. Проверка доступности Every Proxy и работы мобильного VPN
        if check_ping:
            log("INFO", f"Проверка Every Proxy и сквозного интернета через VPN ({proxy_str})...")
            diag = check_proxy_and_vpn(ip, port, timeout=2.5)
            if diag["status"] == "OK_VPN":
                loc_txt = f" ({diag['location']})" if diag.get("location") else ""
                ext_txt = f", внешний IP: {diag['external_ip']}" if diag.get("external_ip") else ""
                lat_txt = f", задержка: {diag['latency_ms']}мс" if diag.get("latency_ms") else ""
                log("OK", f"✅ Связь подтверждена! VPN на смартфоне активен{loc_txt}{ext_txt}{lat_txt}.")
            elif diag["status"] == "PORT_OPEN_NO_VPN":
                log("WARN", "⚠️ ВНИМАНИЕ: Порт Every Proxy открыт, но интернет-трафик через него НЕ ИДЕТ!")
                log("WARN", "Похоже, что VPN на смартфоне отключен, завис или не пропускает пакеты.")
                log("WARN", "Применение сетевых настроек продолжается по запросу...")
            else:
                log("WARN", f"Хост {proxy_str} не отвечает (проверьте точку доступа и Every Proxy).")
                log("WARN", "Продолжаем применение настроек по запросу...")

        # 2. Настройка системного прокси Windows в реестре HKCU
        log("INFO", "Настройка системного прокси Windows (HKCU Internet Settings)...")
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, cls.REG_INTERNET_SETTINGS, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, proxy_str)
            refresh_wininet()
            log("OK", f"Системный прокси включен: {proxy_str} (WinInet обновлен)")
        except Exception as exc:
            log("ERROR", f"Ошибка записи системного прокси в реестр: {exc}")

        # 3. Настройка системного WinHTTP прокси через netsh
        log("INFO", f"Настройка WinHTTP прокси: netsh winhttp set proxy {proxy_str}...")
        c_wh, out_wh, err_wh = run_command(["netsh", "winhttp", "set", "proxy", proxy_str])
        if c_wh == 0:
            log("OK", f"WinHTTP прокси успешно применен ({proxy_str})")
        else:
            log("ERROR", f"Ошибка netsh winhttp: {err_wh or out_wh}")

        # 4. Постоянные пользовательские переменные окружения (setx + реестр)
        log("INFO", "Установка постоянных переменных окружения HTTP_PROXY и HTTPS_PROXY...")
        c_s1, _, _ = run_command(["setx", "HTTP_PROXY", proxy_url])
        c_s2, _, _ = run_command(["setx", "HTTPS_PROXY", proxy_url])
        if c_s1 == 0 and c_s2 == 0:
            log("OK", f"Переменные окружения HTTP_PROXY / HTTPS_PROXY сохранены (setx)")
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, cls.REG_ENVIRONMENT, 0, winreg.KEY_SET_VALUE) as env_k:
                    winreg.SetValueEx(env_k, "HTTP_PROXY", 0, winreg.REG_SZ, proxy_url)
                    winreg.SetValueEx(env_k, "HTTPS_PROXY", 0, winreg.REG_SZ, proxy_url)
                log("OK", "Переменные окружения записаны напрямую в HKCU\\Environment")
            except Exception as e_env:
                log("ERROR", f"Ошибка записи переменных окружения: {e_env}")

        # Применяем к сессии текущего процесса и рассылаем WM_SETTINGCHANGE
        os.environ["HTTP_PROXY"] = proxy_url
        os.environ["HTTPS_PROXY"] = proxy_url
        os.environ["http_proxy"] = proxy_url
        os.environ["https_proxy"] = proxy_url
        broadcast_environment_change()
        log("OK", "Переменные применены к текущей сессии и оповещена система (WM_SETTINGCHANGE)")

        # 5. Глобальный прокси для Git
        if shutil.which("git"):
            log("INFO", f"Настройка глобального прокси для Git ({proxy_url})...")
            g1, _, ge1 = run_command(["git", "config", "--global", "http.proxy", proxy_url])
            g2, _, ge2 = run_command(["git", "config", "--global", "https.proxy", proxy_url])
            if g1 == 0 and g2 == 0:
                log("OK", "Глобальный прокси для Git успешно сконфигурирован")
            else:
                log("WARN", f"Замечание при настройке Git: {ge1 or ge2}")
        else:
            log("INFO", "Git не найден в системе (настройка прокси Git пропущена)")

        # 6. Фиксация TTL=65 (маскировка тетеринга/раздачи перед оператором)
        if set_ttl:
            log("INFO", "Фиксация TTL = 65 для маскировки раздачи (IPv4 / IPv6)...")
            t4, _, te4 = run_command(["netsh", "int", "ipv4", "set", "glob", "defaultcurhoplimit=65"])
            t6, _, te6 = run_command(["netsh", "int", "ipv6", "set", "glob", "defaultcurhoplimit=65"])
            if t4 == 0:
                log("OK", "TTL IPv4 успешно зафиксирован: 65")
            else:
                log("WARN", f"Не удалось изменить TTL IPv4: {te4}")
            if t6 == 0:
                log("OK", "TTL IPv6 успешно зафиксирован: 65")
            else:
                log("WARN", f"Не удалось изменить TTL IPv6: {te6}")

        final_msg = f"Проксирование успешно активировано! Все службы направлены на {proxy_str}."
        log("OK", f"🎉 {final_msg}")
        return True, final_msg

    @classmethod
    def reset_proxy(
        cls,
        reset_ttl: bool = True,
        log_cb: Optional[Callable[[str, str], None]] = None
    ) -> tuple[bool, str]:
        """РЕЖИМ 2: «Сбросить настройки (Вернуть обычный Wi-Fi / Домашний интернет)»."""
        def log(lvl: str, msg: str):
            if log_cb:
                log_cb(lvl, msg)

        log("INFO", "▶ Запуск полного сброса сетевых настроек...")

        # 1. Отключение системного прокси в реестре Windows
        log("INFO", "Отключение системного прокси Windows (ProxyEnable = 0)...")
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, cls.REG_INTERNET_SETTINGS, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
            refresh_wininet()
            log("OK", "Системный прокси Windows отключен (WinInet обновлен)")
        except Exception as exc:
            log("ERROR", f"Ошибка отключения прокси в реестре: {exc}")

        # 2. Сброс WinHTTP прокси
        log("INFO", "Сброс системного WinHTTP прокси: netsh winhttp reset proxy...")
        c_wh, out_wh, err_wh = run_command(["netsh", "winhttp", "reset", "proxy"])
        if c_wh == 0:
            log("OK", "WinHTTP прокси сброшен (прямой доступ)")
        else:
            log("ERROR", f"Ошибка netsh winhttp reset: {err_wh or out_wh}")

        # 3. Удаление постоянных переменных окружения
        log("INFO", "Удаление пользовательских переменных окружения HTTP_PROXY и HTTPS_PROXY...")
        run_command(["reg", "delete", r"HKCU\Environment", "/v", "HTTP_PROXY", "/f"])
        run_command(["reg", "delete", r"HKCU\Environment", "/v", "HTTPS_PROXY", "/f"])
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, cls.REG_ENVIRONMENT, 0, winreg.KEY_SET_VALUE) as env_k:
                for vname in ("HTTP_PROXY", "HTTPS_PROXY"):
                    try:
                        winreg.DeleteValue(env_k, vname)
                    except FileNotFoundError:
                        pass
        except Exception:
            pass

        # Очистка из сессии текущего процесса
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ.pop(var, None)
        broadcast_environment_change()
        log("OK", "Переменные окружения удалены из реестра и сессии (WM_SETTINGCHANGE разослан)")

        # 4. Сброс глобального прокси в Git
        if shutil.which("git"):
            log("INFO", "Сброс глобальных настроек прокси в Git...")
            run_command(["git", "config", "--global", "--unset", "http.proxy"])
            run_command(["git", "config", "--global", "--unset", "https.proxy"])
            log("OK", "Прокси в Git успешно удален")
        else:
            log("INFO", "Git не установлен в системе (пропуск)")

        # 5. Очистка кэша DNS
        log("INFO", "Очистка кэша DNS: ipconfig /flushdns...")
        c_dns, _, e_dns = run_command(["ipconfig", "/flushdns"])
        if c_dns == 0:
            log("OK", "Кэш DNS успешно очищен")
        else:
            log("WARN", f"Замечание при очистке DNS: {e_dns}")

        # 6. Восстановление стандартного TTL Windows (128)
        if reset_ttl:
            log("INFO", "Восстановление стандартного TTL Windows (128)...")
            t4, _, te4 = run_command(["netsh", "int", "ipv4", "set", "glob", "defaultcurhoplimit=128"])
            t6, _, te6 = run_command(["netsh", "int", "ipv6", "set", "glob", "defaultcurhoplimit=128"])
            if t4 == 0:
                log("OK", "TTL IPv4 возвращен к 128 (defaultcurhoplimit=128)")
            else:
                log("WARN", f"Не удалось изменить TTL IPv4: {te4}")
            if t6 == 0:
                log("OK", "TTL IPv6 возвращен к 128 (defaultcurhoplimit=128)")
            else:
                log("WARN", f"Не удалось изменить TTL IPv6: {te6}")

        final_msg = "Сетевые настройки сброшены. Прямой доступ к интернету восстановлен."
        log("OK", f"🌟 {final_msg}")
        return True, final_msg


# ==============================================================================
# 4. СОВРЕМЕННЫЙ ГРАФИЧЕСКИЙ ИНТЕРФЕЙС (CUSTOMTKINTER FLUENT DARK)
# ==============================================================================

class ModernProxySwitcherApp(ctk.CTk if CUSTOMTKINTER_AVAILABLE else object):
    """
    Современный интерфейс в стиле Windows 11 Fluent Dark на базе CustomTkinter.
    """

    # Палитра дизайна Fluent Dark / Catppuccin
    COLOR_BG = "#1A1B1E"          # Основной глубокий фон
    COLOR_CARD = "#25262B"        # Фон карточек
    COLOR_CARD_ALT = "#1E1F24"    # Внутренние мини-панели
    COLOR_BORDER = "#2C2E33"      # Тонкие рамки карточек
    COLOR_PRIMARY = "#4F46E5"     # Акцентный индиго
    COLOR_SUCCESS = "#10B981"     # Изумрудный статус / кнопка
    COLOR_SUCCESS_HOVER = "#059669"
    COLOR_DANGER = "#EF4444"      # Кармин / кнопка сброса
    COLOR_DANGER_HOVER = "#DC2626"
    COLOR_TEXT = "#F1F3F5"        # Основной текст
    COLOR_TEXT_MUTED = "#909296"  # Второстепенный текст
    COLOR_INPUT_BG = "#1A1B1E"    # Поле ввода
    COLOR_INPUT_BORDER = "#373A40"

    def __init__(self):
        super().__init__()

        # Конфигурация окна CustomTkinter
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("Every Proxy Switcher — Сетевой шлюз и мобильный прокси")
        self.configure(fg_color=self.COLOR_BG)

        # Центрирование окна
        self._center_window(width=740, height=800)
        self.minsize(680, 700)

        # Иконка окна
        try:
            icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_icon.ico")
            if os.path.exists(icon_path):
                self.after(200, lambda: self.iconbitmap(icon_path))
        except Exception:
            pass

        # Переменные управления
        self.var_ip = ctk.StringVar(value="")
        self.var_port = ctk.StringVar(value="8080")
        self.var_ttl = ctk.BooleanVar(value=True)
        self.var_ping = ctk.BooleanVar(value=True)
        self.is_busy = False

        self._build_ui()
        self._detect_gateway_hint()
        self.refresh_status()

        # Фокус на поле ввода IP
        self.after(150, lambda: self.entry_ip.focus_set())

        # Автоматический фоновый поиск рабочего Every Proxy и проверка VPN
        self.after(350, self.start_auto_detect_silent)

    def _center_window(self, width: int = 740, height: int = 800):
        """Размещает окно строго по центру экрана пользователя."""
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x = max(0, int((sw - width) / 2))
        y = max(0, int((sh - height) / 2))
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _build_ui(self):
        """Создает и размещает все компоненты современного интерфейса."""
        # Главный контейнер
        self.main_container = ctk.CTkFrame(self, fg_color="transparent")
        self.main_container.pack(fill="both", expand=True, padx=22, pady=18)

        # ---------------- 1. ШАПКА (Header) ----------------
        self.header_frame = ctk.CTkFrame(self.main_container, fg_color="transparent")
        self.header_frame.pack(fill="x", pady=(0, 12))

        # Левая часть шапки: логотип, название и бейдж версии
        title_box = ctk.CTkFrame(self.header_frame, fg_color="transparent")
        title_box.pack(side="left", fill="y")

        lbl_logo = ctk.CTkLabel(
            title_box,
            text="📱",
            font=ctk.CTkFont(family="Segoe UI", size=22)
        )
        lbl_logo.pack(side="left", padx=(0, 8))

        lbl_title = ctk.CTkLabel(
            title_box,
            text="Every Proxy Switcher",
            font=ctk.CTkFont(family="Segoe UI", size=18, weight="bold"),
            text_color=self.COLOR_TEXT
        )
        lbl_title.pack(side="left")

        # Бейдж версии
        badge_ver = ctk.CTkFrame(
            title_box,
            fg_color="#2C2E33",
            corner_radius=6,
            height=20
        )
        badge_ver.pack(side="left", padx=(8, 0))
        lbl_ver = ctk.CTkLabel(
            badge_ver,
            text="v2.0",
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            text_color="#A6ADC8"
        )
        lbl_ver.pack(padx=6, pady=1)

        # Правая часть шапки: плашка глобального статуса
        self.status_pill = ctk.CTkFrame(
            self.header_frame,
            fg_color="#2C2E33",
            corner_radius=16,
            border_width=1,
            border_color="#373A40"
        )
        self.status_pill.pack(side="right")

        self.lbl_status_pill = ctk.CTkLabel(
            self.status_pill,
            text="● Определение...",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=self.COLOR_TEXT_MUTED
        )
        self.lbl_status_pill.pack(padx=12, pady=4)

        # ---------------- 2. КАРТОЧКА СТАТУСА СИСТЕМЫ ----------------
        card_status = ctk.CTkFrame(
            self.main_container,
            fg_color=self.COLOR_CARD,
            corner_radius=12,
            border_width=1,
            border_color=self.COLOR_BORDER
        )
        card_status.pack(fill="x", pady=(0, 12))

        # Заголовок карточки статуса
        hdr_status = ctk.CTkFrame(card_status, fg_color="transparent")
        hdr_status.pack(fill="x", padx=16, pady=(12, 8))

        lbl_status_sec = ctk.CTkLabel(
            hdr_status,
            text="СИСТЕМНЫЙ СТАТУС СЕТИ",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=self.COLOR_TEXT_MUTED
        )
        lbl_status_sec.pack(side="left")

        self.btn_refresh = ctk.CTkButton(
            hdr_status,
            text="🔄 Обновить",
            width=90,
            height=26,
            corner_radius=6,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            fg_color=self.COLOR_CARD_ALT,
            hover_color="#32343B",
            border_width=1,
            border_color=self.COLOR_BORDER,
            text_color=self.COLOR_TEXT,
            command=self.refresh_status
        )
        self.btn_refresh.pack(side="right")

        # Сетка из 4 мини-чипов компонентов
        chips_grid = ctk.CTkFrame(card_status, fg_color="transparent")
        chips_grid.pack(fill="x", padx=14, pady=(0, 14))
        chips_grid.grid_columnconfigure((0, 1, 2, 3), weight=1)

        # Чип 1: WinHTTP
        self.chip_winhttp = self._create_status_chip(chips_grid, 0, "WinHTTP", "Прямой")
        # Чип 2: Переменные среды
        self.chip_env = self._create_status_chip(chips_grid, 1, "Переменные (setx)", "Чисто")
        # Чип 3: Git Прокси
        self.chip_git = self._create_status_chip(chips_grid, 2, "Git Прокси", "Выкл")
        # Чип 4: TTL (Hop Limit)
        self.chip_ttl = self._create_status_chip(chips_grid, 3, "TTL (IPv4)", "128")

        # ---------------- 3. КАРТОЧКА НАСТРОЕК EVERY PROXY ----------------
        card_settings = ctk.CTkFrame(
            self.main_container,
            fg_color=self.COLOR_CARD,
            corner_radius=12,
            border_width=1,
            border_color=self.COLOR_BORDER
        )
        card_settings.pack(fill="x", pady=(0, 12))

        # Заголовок карточки настроек с кнопкой Автопоиска
        hdr_settings = ctk.CTkFrame(card_settings, fg_color="transparent")
        hdr_settings.pack(fill="x", padx=16, pady=(12, 10))

        lbl_cfg_title = ctk.CTkLabel(
            hdr_settings,
            text="ПАРАМЕТРЫ ПОДКЛЮЧЕНИЯ EVERY PROXY",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=self.COLOR_TEXT_MUTED
        )
        lbl_cfg_title.pack(side="left")

        self.btn_autosearch = ctk.CTkButton(
            hdr_settings,
            text="🔍 Автопоиск Every Proxy",
            width=175,
            height=28,
            corner_radius=6,
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            fg_color=self.COLOR_PRIMARY,
            hover_color="#4338CA",
            text_color="#FFFFFF",
            command=self.on_autosearch_clicked
        )
        self.btn_autosearch.pack(side="right")

        # Группа полей ввода IP, Порта и кнопки тестирования VPN
        inputs_frame = ctk.CTkFrame(card_settings, fg_color="transparent")
        inputs_frame.pack(fill="x", padx=16, pady=(0, 8))

        # Поле IP
        ip_box = ctk.CTkFrame(inputs_frame, fg_color="transparent")
        ip_box.pack(side="left", fill="x", expand=True, padx=(0, 10))

        lbl_ip = ctk.CTkLabel(
            ip_box,
            text="IP-адрес телефона (из Every Proxy):",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=self.COLOR_TEXT
        )
        lbl_ip.pack(anchor="w", pady=(0, 4))

        self.entry_ip = ctk.CTkEntry(
            ip_box,
            textvariable=self.var_ip,
            placeholder_text="например: 192.168.43.1 или 10.x.x.x",
            height=40,
            corner_radius=8,
            fg_color=self.COLOR_INPUT_BG,
            border_color=self.COLOR_INPUT_BORDER,
            text_color=self.COLOR_TEXT,
            font=ctk.CTkFont(family="Segoe UI", size=13)
        )
        self.entry_ip.pack(fill="x")

        # Поле Порт
        port_box = ctk.CTkFrame(inputs_frame, fg_color="transparent")
        port_box.pack(side="left", padx=(0, 10))

        lbl_port = ctk.CTkLabel(
            port_box,
            text="Порт:",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=self.COLOR_TEXT
        )
        lbl_port.pack(anchor="w", pady=(0, 4))

        self.entry_port = ctk.CTkEntry(
            port_box,
            textvariable=self.var_port,
            placeholder_text="8080",
            width=85,
            height=40,
            corner_radius=8,
            fg_color=self.COLOR_INPUT_BG,
            border_color=self.COLOR_INPUT_BORDER,
            text_color=self.COLOR_TEXT,
            font=ctk.CTkFont(family="Segoe UI", size=13)
        )
        self.entry_port.pack()

        # Кнопка быстрой проверки VPN на текущем IP:PORT
        test_btn_box = ctk.CTkFrame(inputs_frame, fg_color="transparent")
        test_btn_box.pack(side="right")

        lbl_test_pad = ctk.CTkLabel(
            test_btn_box,
            text="Диагностика:",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=self.COLOR_TEXT_MUTED
        )
        lbl_test_pad.pack(anchor="w", pady=(0, 4))

        self.btn_test_vpn = ctk.CTkButton(
            test_btn_box,
            text="🌐 Тест VPN",
            width=105,
            height=40,
            corner_radius=8,
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            fg_color="#1E293B",
            hover_color="#334155",
            border_width=1,
            border_color="#38BDF8",
            text_color="#38BDF8",
            command=self.on_test_vpn_clicked
        )
        self.btn_test_vpn.pack()

        # Карточка живого статуса проверки Every Proxy & VPN
        self.vpn_status_card = ctk.CTkFrame(
            card_settings,
            fg_color=self.COLOR_CARD_ALT,
            corner_radius=8,
            border_width=1,
            border_color=self.COLOR_BORDER
        )
        self.vpn_status_card.pack(fill="x", padx=16, pady=(0, 8))

        self.lbl_vpn_status = ctk.CTkLabel(
            self.vpn_status_card,
            text="💡 Нажмите '🔍 Автопоиск Every Proxy' для автоматического поиска рабочего IP и проверки VPN.",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=self.COLOR_TEXT_MUTED,
            anchor="w",
            justify="left"
        )
        self.lbl_vpn_status.pack(fill="x", padx=12, pady=6)

        # Кликабельный бейдж с обнаруженным шлюзом
        self.frame_hint = ctk.CTkFrame(card_settings, fg_color="transparent")
        self.frame_hint.pack(fill="x", padx=16, pady=(0, 10))

        self.lbl_hint_tip = ctk.CTkLabel(
            self.frame_hint,
            text="💡 Включите переключатель HTTP в Every Proxy на смартфоне и введите указанный IP.",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=self.COLOR_TEXT_MUTED
        )
        self.lbl_hint_tip.pack(side="left")

        # Чекбоксы настроек
        opts_frame = ctk.CTkFrame(card_settings, fg_color="transparent")
        opts_frame.pack(fill="x", padx=16, pady=(0, 14))

        self.chk_ttl = ctk.CTkCheckBox(
            opts_frame,
            text="Фиксировать TTL = 65 (маскировка тетеринга/раздачи перед мобильным оператором)",
            variable=self.var_ttl,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=self.COLOR_TEXT,
            fg_color=self.COLOR_PRIMARY,
            hover_color="#4338CA",
            corner_radius=6,
            checkbox_width=20,
            checkbox_height=20
        )
        self.chk_ttl.pack(anchor="w", pady=(0, 6))

        self.chk_ping = ctk.CTkCheckBox(
            opts_frame,
            text="Быстрая проверка доступности телефона перед подключением (Ping + TCP порт)",
            variable=self.var_ping,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=self.COLOR_TEXT,
            fg_color=self.COLOR_PRIMARY,
            hover_color="#4338CA",
            corner_radius=6,
            checkbox_width=20,
            checkbox_height=20
        )
        self.chk_ping.pack(anchor="w")

        # ---------------- 4. БЛОК ДЕЙСТВИЙ (Главные кнопки) ----------------
        actions_frame = ctk.CTkFrame(self.main_container, fg_color="transparent")
        actions_frame.pack(fill="x", pady=(0, 12))

        self.btn_enable = ctk.CTkButton(
            actions_frame,
            text="⚡ Включить проксирование (Every Proxy)",
            height=46,
            corner_radius=10,
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            fg_color=self.COLOR_SUCCESS,
            hover_color=self.COLOR_SUCCESS_HOVER,
            text_color="#FFFFFF",
            command=self.on_enable_clicked
        )
        self.btn_enable.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.btn_reset = ctk.CTkButton(
            actions_frame,
            text="🔄 Сбросить настройки (Вернуть интернет)",
            height=46,
            corner_radius=10,
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            fg_color=self.COLOR_DANGER,
            hover_color=self.COLOR_DANGER_HOVER,
            text_color="#FFFFFF",
            command=self.on_reset_clicked
        )
        self.btn_reset.pack(side="right", fill="x", expand=True, padx=(6, 0))

        # ---------------- 5. ЖУРНАЛ ОПЕРАЦИЙ (Лог-консоль) ----------------
        card_log = ctk.CTkFrame(
            self.main_container,
            fg_color=self.COLOR_CARD,
            corner_radius=12,
            border_width=1,
            border_color=self.COLOR_BORDER
        )
        card_log.pack(fill="both", expand=True)

        hdr_log = ctk.CTkFrame(card_log, fg_color="transparent")
        hdr_log.pack(fill="x", padx=16, pady=(12, 8))

        lbl_log_title = ctk.CTkLabel(
            hdr_log,
            text="ЖУРНАЛ ОПЕРАЦИЙ (ЛОГ)",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=self.COLOR_TEXT_MUTED
        )
        lbl_log_title.pack(side="left")

        # Кнопки копирования и очистки лога
        self.btn_copy_log = ctk.CTkButton(
            hdr_log,
            text="📋 Копировать",
            width=90,
            height=26,
            corner_radius=6,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            fg_color=self.COLOR_CARD_ALT,
            hover_color="#32343B",
            border_width=1,
            border_color=self.COLOR_BORDER,
            text_color=self.COLOR_TEXT,
            command=self.copy_log
        )
        self.btn_copy_log.pack(side="right", padx=(6, 0))

        self.btn_clear_log = ctk.CTkButton(
            hdr_log,
            text="🧹 Очистить",
            width=80,
            height=26,
            corner_radius=6,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            fg_color=self.COLOR_CARD_ALT,
            hover_color="#32343B",
            border_width=1,
            border_color=self.COLOR_BORDER,
            text_color=self.COLOR_TEXT,
            command=self.clear_log
        )
        self.btn_clear_log.pack(side="right")

        # Текстовое поле лога (CTkTextbox со скруглением и темным фоном)
        self.txt_log = ctk.CTkTextbox(
            card_log,
            wrap="word",
            font=ctk.CTkFont(family="Consolas", size=11),
            fg_color="#141517",
            border_width=1,
            border_color=self.COLOR_BORDER,
            corner_radius=8,
            text_color="#CDD6F4"
        )
        self.txt_log.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        # Цветовые теги подсветки сообщений в логе
        self.txt_log.tag_config("TIME", foreground="#6C7086")
        self.txt_log.tag_config("INFO", foreground="#89B4FA")
        self.txt_log.tag_config("OK", foreground="#10B981")
        self.txt_log.tag_config("WARN", foreground="#F59E0B")
        self.txt_log.tag_config("ERROR", foreground="#EF4444")

        self.append_log("INFO", "Интерфейс CustomTkinter загружен. Права администратора активны.")

    def _create_status_chip(self, parent: ctk.CTkFrame, col: int, title: str, default_val: str) -> dict:
        """Создает стильный мини-бейдж статуса одного сетевого компонента."""
        chip_frame = ctk.CTkFrame(
            parent,
            fg_color=self.COLOR_CARD_ALT,
            corner_radius=8,
            border_width=1,
            border_color=self.COLOR_BORDER
        )
        chip_frame.grid(row=0, column=col, padx=3, pady=2, sticky="nsew")

        lbl_t = ctk.CTkLabel(
            chip_frame,
            text=title,
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color=self.COLOR_TEXT_MUTED
        )
        lbl_t.pack(anchor="w", padx=8, pady=(4, 0))

        lbl_v = ctk.CTkLabel(
            chip_frame,
            text=default_val,
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=self.COLOR_TEXT
        )
        lbl_v.pack(anchor="w", padx=8, pady=(0, 4))

        return {"frame": chip_frame, "title": lbl_t, "value": lbl_v}

    def _detect_gateway_hint(self):
        """Определяет шлюз Wi-Fi адаптера и создает кликабельный pill-бейдж для быстрой вставки."""
        gateways = detect_network_gateways()
        if gateways:
            suggested = gateways[0]
            # Кликабельная кнопка-бейдж для автозаполнения
            btn_pill = ctk.CTkButton(
                self.frame_hint,
                text=f"💡 Обнаружен шлюз: {suggested} (нажмите для вставки)",
                height=26,
                corner_radius=13,
                font=ctk.CTkFont(family="Segoe UI", size=11),
                fg_color="#1E293B",
                hover_color="#334155",
                border_width=1,
                border_color="#38BDF8",
                text_color="#38BDF8",
                command=lambda: self.var_ip.set(suggested)
            )
            btn_pill.pack(side="right")

    def append_log(self, level: str, message: str):
        """Потокобезопасный вывод форматированного лога в терминал."""
        def _insert():
            t_str = datetime.now().strftime("[%H:%M:%S] ")
            self.txt_log.insert("end", t_str, "TIME")
            lvl_tag = level.upper()
            if lvl_tag not in ("INFO", "OK", "WARN", "ERROR"):
                lvl_tag = "INFO"
            self.txt_log.insert("end", f"[{lvl_tag}] ", lvl_tag)
            self.txt_log.insert("end", f"{message}\n")
            self.txt_log.see("end")

        if threading.current_thread() is threading.main_thread():
            _insert()
        else:
            self.after(0, _insert)

    def clear_log(self):
        self.txt_log.delete("1.0", "end")

    def copy_log(self):
        content = self.txt_log.get("1.0", "end").strip()
        self.clipboard_clear()
        self.clipboard_append(content)
        self.append_log("INFO", "Текст журнала скопирован в буфер обмена.")

    def set_controls_state(self, enabled: bool):
        """Блокирует или разблокирует кнопки управления на время выполнения системной задачи."""
        self.is_busy = not enabled
        state = "normal" if enabled else "disabled"
        self.btn_enable.configure(state=state)
        self.btn_reset.configure(state=state)
        self.btn_refresh.configure(state=state)
        self.entry_ip.configure(state=state)
        self.entry_port.configure(state=state)

    def refresh_status(self):
        """Асинхронный опрос сетевых параметров Windows и обновление дашборда."""
        def _task():
            st = NetworkManager.get_system_status()

            def _update():
                # 1. Обновление плашки в шапке
                if st["is_active"]:
                    proxy_target = st["proxy_server"] or st["winhttp"] or "Активен"
                    self.status_pill.configure(
                        fg_color="#064E3B",
                        border_color="#059669"
                    )
                    self.lbl_status_pill.configure(
                        text=f"🟢 Прокси активен [{proxy_target}]",
                        text_color="#10B981"
                    )
                else:
                    self.status_pill.configure(
                        fg_color="#2C2E33",
                        border_color="#373A40"
                    )
                    self.lbl_status_pill.configure(
                        text="⚪ Штатный режим",
                        text_color=self.COLOR_TEXT_MUTED
                    )

                # 2. Обновление мини-чипов
                # WinHTTP
                wh_val = st["winhttp"]
                self.chip_winhttp["value"].configure(
                    text=wh_val,
                    text_color="#10B981" if wh_val != "Прямой доступ" else self.COLOR_TEXT
                )

                # Переменные среды
                env_val = "Заданы" if st["env_http"] else "Чисто"
                self.chip_env["value"].configure(
                    text=env_val,
                    text_color="#10B981" if st["env_http"] else self.COLOR_TEXT
                )

                # Git
                git_val = "Настроен" if st["git_proxy"] else "Выкл"
                self.chip_git["value"].configure(
                    text=git_val,
                    text_color="#10B981" if st["git_proxy"] else self.COLOR_TEXT
                )

                # TTL
                ttl_val = f"{st['ttl']}" + (" (65)" if st["ttl"] == 65 else " (128)")
                self.chip_ttl["value"].configure(
                    text=ttl_val,
                    text_color="#F59E0B" if st["ttl"] == 65 else self.COLOR_TEXT
                )

            self.after(0, _update)

        threading.Thread(target=_task, daemon=True).start()

    def update_vpn_status_ui(self, diag: dict):
        """Обновляет плашку статуса VPN и Every Proxy в интерфейсе."""
        st = diag.get("status", "UNREACHABLE")
        if st == "OK_VPN":
            loc = f" ({diag['location']})" if diag.get("location") else ""
            ext = f" • Внешний IP: {diag['external_ip']}" if diag.get("external_ip") else ""
            lat = f" • Задержка: {diag['latency_ms']}мс" if diag.get("latency_ms") else ""
            self.vpn_status_card.configure(
                fg_color="#064E3B",
                border_color="#059669"
            )
            self.lbl_vpn_status.configure(
                text=f"🟢 Every Proxy и VPN работают!{ext}{loc}{lat}",
                text_color="#10B981"
            )
        elif st == "PORT_OPEN_NO_VPN":
            self.vpn_status_card.configure(
                fg_color="#451A03",
                border_color="#D97706"
            )
            self.lbl_vpn_status.configure(
                text="⚠️ ВНИМАНИЕ: Порт открыт, но VPN на смартфоне НЕ РАБОТАЕТ! Пакеты в интернет не идут.",
                text_color="#F59E0B"
            )
        elif st == "UNREACHABLE":
            self.vpn_status_card.configure(
                fg_color="#2A1B1E",
                border_color="#EF4444"
            )
            self.lbl_vpn_status.configure(
                text=f"❌ Хост {diag.get('ip', '')}:{diag.get('port', 8080)} недоступен (Every Proxy не отвечает)",
                text_color="#F87171"
            )
        else:
            self.vpn_status_card.configure(
                fg_color=self.COLOR_CARD_ALT,
                border_color=self.COLOR_BORDER
            )
            self.lbl_vpn_status.configure(
                text="💡 Нажмите '🔍 Автопоиск Every Proxy' для автоматического поиска рабочего IP.",
                text_color=self.COLOR_TEXT_MUTED
            )

    def on_autosearch_clicked(self):
        """Запуск сканирования сети для поиска активного Every Proxy и проверки VPN."""
        if self.is_busy:
            return

        port_str = self.var_port.get().strip()
        port = int(port_str) if is_valid_port(port_str) else 8080
        curr_ip = self.var_ip.get().strip()

        self.btn_autosearch.configure(text="⏳ Поиск...", state="disabled")
        self.btn_test_vpn.configure(state="disabled")

        def _task():
            found_ip, diag = auto_detect_working_proxy(port=port, current_ip=curr_ip, log_cb=self.append_log)

            def _done():
                self.btn_autosearch.configure(text="🔍 Автопоиск Every Proxy", state="normal")
                self.btn_test_vpn.configure(state="normal")
                if found_ip:
                    self.var_ip.set(found_ip)
                    self.update_vpn_status_ui(diag)
                else:
                    self.update_vpn_status_ui({"status": "UNREACHABLE", "ip": curr_ip or "узел", "port": port})

            self.after(0, _done)

        threading.Thread(target=_task, daemon=True).start()

    def on_test_vpn_clicked(self):
        """Быстрая проверка Every Proxy и работы VPN для текущего введенного IP:PORT."""
        ip = self.var_ip.get().strip()
        if not ip or not is_valid_ipv4(ip):
            self.append_log("WARN", "Для проверки введите валидный IP или нажмите 'Автопоиск Every Proxy'.")
            return

        port_str = self.var_port.get().strip()
        port = int(port_str) if is_valid_port(port_str) else 8080

        self.btn_test_vpn.configure(text="⏳ Тест...", state="disabled")
        self.append_log("INFO", f"Диагностика Every Proxy и сквозного интернета на {ip}:{port}...")

        def _task():
            diag = check_proxy_and_vpn(ip=ip, port=port, timeout=2.5)

            def _done():
                self.btn_test_vpn.configure(text="🌐 Тест VPN", state="normal")
                self.update_vpn_status_ui(diag)
                if diag["status"] == "OK_VPN":
                    self.append_log("OK", f"✅ {diag['message']}")
                elif diag["status"] == "PORT_OPEN_NO_VPN":
                    self.append_log("WARN", f"⚠️ {diag['message']}")
                else:
                    self.append_log("ERROR", f"❌ {diag['message']}")

            self.after(0, _done)

        threading.Thread(target=_task, daemon=True).start()

    def start_auto_detect_silent(self):
        """Тихий автопоиск при старте приложения."""
        def _task():
            port = 8080
            curr_ip = self.var_ip.get().strip()
            found_ip, diag = auto_detect_working_proxy(port=port, current_ip=curr_ip)
            if found_ip:
                def _update():
                    self.var_ip.set(found_ip)
                    self.update_vpn_status_ui(diag)
                    if diag["status"] == "OK_VPN":
                        loc_txt = f" ({diag['location']})" if diag.get("location") else ""
                        self.append_log("OK", f"Автопоиск: обнаружен рабочий Every Proxy на {found_ip}:{port}{loc_txt}")
                    elif diag["status"] == "PORT_OPEN_NO_VPN":
                        self.append_log("WARN", f"Автопоиск: на {found_ip}:{port} открыт Every Proxy, но VPN на смартфоне не пропускает трафик!")
                self.after(0, _update)

        threading.Thread(target=_task, daemon=True).start()

    def on_enable_clicked(self):
        """Клик по кнопке 'Включить проксирование' с предупреждением о неработающем VPN."""
        if self.is_busy:
            return

        ip = self.var_ip.get().strip()

        # Если поле пустое, запускаем автопоиск или запрашиваем ввод
        if not ip or not is_valid_ipv4(ip):
            self.append_log("INFO", "IP-адрес не указан. Запуск автопоиска Every Proxy в сети...")
            self.on_autosearch_clicked()
            ip = self.var_ip.get().strip()
            if not ip or not is_valid_ipv4(ip):
                dialog = ctk.CTkInputDialog(
                    text="Введите IP-адрес смартфона из блока HTTP приложения Every Proxy:",
                    title="IP-адрес Every Proxy"
                )
                input_val = dialog.get_input()
                if not input_val:
                    self.append_log("WARN", "Активация отменена: IP-адрес не указан.")
                    return
                ip = input_val.strip()
                self.var_ip.set(ip)

        if not is_valid_ipv4(ip):
            self.append_log("ERROR", f"Некорректный IPv4-адрес: '{ip}'")
            return

        port_str = self.var_port.get().strip()
        if not is_valid_port(port_str):
            self.append_log("ERROR", f"Недопустимый порт: '{port_str}'. Разрешено от 1 до 65535.")
            return
        port = int(port_str)

        set_ttl = self.var_ttl.get()
        check_ping = self.var_ping.get()

        self.set_controls_state(False)

        def _worker():
            # Превентивная проверка VPN
            if check_ping:
                self.append_log("INFO", f"Диагностика Every Proxy и работы мобильного VPN на {ip}:{port}...")
                diag = check_proxy_and_vpn(ip=ip, port=port, timeout=2.5)
                self.after(0, lambda: self.update_vpn_status_ui(diag))

                if diag["status"] == "PORT_OPEN_NO_VPN":
                    # Предупреждение пользователю о неработающем VPN!
                    self.append_log("WARN", "⚠️ ВНИМАНИЕ: VPN на смартфоне не пропускает трафик!")
                    proceed_anyway = [False]

                    def _ask_modal():
                        ans = messagebox.askyesno(
                            "⚠️ Внимание: VPN на телефоне не работает!",
                            f"Every Proxy на адресе {ip}:{port} отвечает, но интернет-трафик через него НЕ ИДЕТ!\n\n"
                            f"Возможные причины:\n"
                            f"• На телефоне отключен или завис VPN (V2Ray, Outline, WireGuard, Amnezia)\n"
                            f"• В приложении VPN не разрешена раздача на локальную сеть\n"
                            f"• Закончился мобильный интернет\n\n"
                            f"Если включить проксирование сейчас, на компьютере ПРОПАДЕТ ИНТЕРНЕТ.\n\n"
                            f"Все равно включить проксирование?",
                            icon="warning"
                        )
                        proceed_anyway[0] = ans

                    # Показываем модальное окно в главном потоке и ждем
                    evt = threading.Event()
                    def _call():
                        _ask_modal()
                        evt.set()
                    self.after(0, _call)
                    evt.wait()

                    if not proceed_anyway[0]:
                        self.append_log("WARN", "Включение прокси отменено пользователем из-за неработающего VPN.")
                        self.after(0, lambda: self.set_controls_state(True))
                        return

                elif diag["status"] == "UNREACHABLE":
                    self.append_log("ERROR", f"❌ Хост {ip}:{port} недоступен. Проверьте Wi-Fi и тумблер в Every Proxy.")
                    def _err_modal():
                        messagebox.showerror(
                            "Хост недоступен",
                            f"Хост {ip}:{port} не отвечает ни на пинг, ни на порту {port}.\n\n"
                            f"Убедитесь, что ПК подключен к точке доступа телефона и в Every Proxy включен тумблер HTTP."
                        )
                    self.after(0, _err_modal)
                    self.after(0, lambda: self.set_controls_state(True))
                    return

            try:
                NetworkManager.enable_proxy(
                    ip=ip,
                    port=port,
                    set_ttl=set_ttl,
                    check_ping=False,  # Мы уже выполнили глубокую проверку
                    log_cb=self.append_log
                )
            except Exception as exc:
                self.append_log("ERROR", f"Ошибка: {exc}")
            finally:
                self.after(0, lambda: self.set_controls_state(True))
                self.refresh_status()

        threading.Thread(target=_worker, daemon=True).start()

    def on_reset_clicked(self):
        """Клик по кнопке 'Сбросить настройки'."""
        if self.is_busy:
            return

        self.set_controls_state(False)

        def _worker():
            try:
                NetworkManager.reset_proxy(
                    reset_ttl=True,
                    log_cb=self.append_log
                )
            except Exception as exc:
                self.append_log("ERROR", f"Ошибка при сбросе: {exc}")
            finally:
                self.after(0, lambda: self.set_controls_state(True))
                self.refresh_status()

        threading.Thread(target=_worker, daemon=True).start()


# ==============================================================================
# 5. КОНСОЛЬНЫЙ ИНТЕРФЕЙС (CLI MENU)
# ==============================================================================

class ConsoleColors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"


def cli_log(level: str, message: str):
    """Цветной вывод лога в консоль."""
    t_str = datetime.now().strftime("[%H:%M:%S]")
    if level == "OK":
        color = ConsoleColors.GREEN
    elif level == "WARN":
        color = ConsoleColors.YELLOW
    elif level == "ERROR":
        color = ConsoleColors.RED
    else:
        color = ConsoleColors.CYAN

    print(f"{ConsoleColors.GRAY}{t_str}{ConsoleColors.RESET} {color}[{level}]{ConsoleColors.RESET} {message}")


def run_cli_interactive():
    """Интерактивное консольное меню с цветным выводом."""
    if sys.platform == "win32":
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass

    while True:
        st = NetworkManager.get_system_status()
        status_line = (
            f"{ConsoleColors.GREEN}АКТИВЕН ({st['proxy_server'] or st['winhttp']}){ConsoleColors.RESET}"
            if st["is_active"]
            else f"{ConsoleColors.GRAY}Штатный режим (Прокси выключен){ConsoleColors.RESET}"
        )

        print("\n" + "=" * 65)
        print(f" {ConsoleColors.BOLD}📱 EVERY PROXY SWITCHER — УПРАВЛЕНИЕ СЕТЬЮ WINDOWS{ConsoleColors.RESET}")
        print("=" * 65)
        print(f" Текущий статус: {status_line}")
        print(f" WinHTTP: {st['winhttp']} | Git: {st['git_proxy'] or 'Прямой'} | TTL: {st['ttl']}")
        print("-" * 65)
        print(f" [{ConsoleColors.GREEN}1{ConsoleColors.RESET}] Включить проксирование (Every Proxy)")
        print(f" [{ConsoleColors.RED}2{ConsoleColors.RESET}] Сбросить настройки (Вернуть обычный Wi-Fi / Домашний интернет)")
        print(f" [{ConsoleColors.CYAN}3{ConsoleColors.RESET}] Проверить текущий статус сети")
        print(f" [{ConsoleColors.YELLOW}4{ConsoleColors.RESET}] 🔍 Автопоиск Every Proxy и диагностика VPN")
        print(f" [{ConsoleColors.GRAY}0{ConsoleColors.RESET}] Выход")
        print("=" * 65)

        choice = input("Выберите действие [0-4]: ").strip()

        if choice == "1":
            ip_input = input(f"\nВведите IP-адрес телефона [или нажмите Enter для автопоиска]: ").strip()
            if not ip_input:
                print("Запуск автопоиска Every Proxy в локальной сети...")
                found_ip, diag = auto_detect_working_proxy(port=8080, log_cb=cli_log)
                if found_ip:
                    ip = found_ip
                    print(f"{ConsoleColors.GREEN}Используем найденный IP: {ip}{ConsoleColors.RESET}")
                else:
                    print(f"{ConsoleColors.RED}Every Proxy не найден автоматически. Введите IP вручную.{ConsoleColors.RESET}")
                    ip = ""
            else:
                ip = ip_input

            while not is_valid_ipv4(ip):
                ip_val = input("Введите корректный IP-адрес: ").strip()
                if is_valid_ipv4(ip_val):
                    ip = ip_val

            port_input = input("Порт Every Proxy [по умолчанию 8080]: ").strip()
            port = int(port_input) if (port_input.isdigit() and is_valid_port(port_input)) else 8080

            # Диагностика VPN перед применением
            print("Проверка Every Proxy и мобильного VPN...")
            diag = check_proxy_and_vpn(ip=ip, port=port, timeout=2.5)
            if diag["status"] == "PORT_OPEN_NO_VPN":
                print(f"\n{ConsoleColors.RED}⚠️ ВНИМАНИЕ: Порт Every Proxy открыт, но VPN на смартфоне НЕ РАБОТАЕТ!{ConsoleColors.RESET}")
                print("Пакеты в интернет не проходят. Если включить прокси, интернет на ПК пропадет.")
                ans_cont = input("Все равно включить проксирование? [y/N]: ").strip().lower()
                if ans_cont not in ("y", "yes", "да", "1"):
                    print("Включение отменено.")
                    continue
            elif diag["status"] == "UNREACHABLE":
                print(f"{ConsoleColors.RED}❌ Хост {ip}:{port} недоступен. Проверьте Wi-Fi и Every Proxy.{ConsoleColors.RESET}")
                continue
            else:
                print(f"{ConsoleColors.GREEN}✅ VPN активен! {diag['message']}{ConsoleColors.RESET}")

            ttl_ans = input("Зафиксировать TTL=65 для маскировки раздачи? [Y/n]: ").strip().lower()
            set_ttl = ttl_ans not in ("n", "no", "нет", "0")

            print("\nПрименение сетевых настроек...")
            NetworkManager.enable_proxy(
                ip=ip,
                port=port,
                set_ttl=set_ttl,
                check_ping=False,
                log_cb=cli_log
            )

        elif choice == "2":
            confirm = input(f"\n{ConsoleColors.YELLOW}Сбросить все настройки прокси и вернуть стандартный интернет? [Y/n]: {ConsoleColors.RESET}").strip().lower()
            if confirm in ("", "y", "yes", "да", "1"):
                print("\nСброс настроек...")
                NetworkManager.reset_proxy(reset_ttl=True, log_cb=cli_log)
            else:
                print("Сброс отменен пользователем.")

        elif choice == "3":
            print("\n" + "-" * 50)
            print(f"{ConsoleColors.CYAN}Подробный статус компонентов:{ConsoleColors.RESET}")
            st = NetworkManager.get_system_status()
            print(f" - Системный прокси (Реестр): {'Включен (' + st['proxy_server'] + ')' if st['proxy_enable'] else 'Выключен'}")
            print(f" - WinHTTP прокси:            {st['winhttp']}")
            print(f" - HTTP_PROXY (Env):          {st['env_http'] or 'Не задан'}")
            print(f" - HTTPS_PROXY (Env):         {st['env_https'] or 'Не задан'}")
            print(f" - Git http.proxy:            {st['git_proxy'] or 'Не задан'}")
            print(f" - TTL (Hop Limit IPv4):      {st['ttl']}")
            print("-" * 50)

        elif choice == "4":
            print("\n" + "=" * 50)
            print(" 🔍 АВТОПОИСК EVERY PROXY И ПРОВЕРКА VPN")
            print("=" * 50)
            port_in = input("Порт для поиска [по умолчанию 8080]: ").strip()
            port = int(port_in) if (port_in.isdigit() and is_valid_port(port_in)) else 8080
            found_ip, diag = auto_detect_working_proxy(port=port, log_cb=cli_log)
            print("-" * 50)
            if found_ip:
                print(f"{ConsoleColors.GREEN}Результат: Найден узел {found_ip}:{port}{ConsoleColors.RESET}")
                print(f"Статус: {diag['message']}")
            else:
                print(f"{ConsoleColors.RED}Результат: Every Proxy не обнаружен.{ConsoleColors.RESET}")
            print("=" * 50)
            print("-" * 50)

        elif choice == "0":
            print("Выход из программы.")
            break
        else:
            print(f"{ConsoleColors.RED}Неверный пункт меню.{ConsoleColors.RESET}")


# ==============================================================================
# 6. ТОЧКА ВХОДА (MAIN)
# ==============================================================================

def main():
    """Точка входа: проверка UAC, аргументы CLI и запуск интерфейса."""
    parser = argparse.ArgumentParser(
        description="Every Proxy Switcher — переключение прокси и домашнего интернета на Windows 10/11"
    )
    parser.add_argument("--cli", action="store_true", help="Запустить в интерактивном консольном режиме")
    parser.add_argument("--enable", metavar="IP", help="Быстро включить прокси для указанного IP")
    parser.add_argument("--port", type=int, default=8080, help="Порт прокси (по умолчанию 8080)")
    parser.add_argument("--no-ttl", action="store_true", help="Не изменять значение TTL")
    parser.add_argument("--reset", action="store_true", help="Быстро сбросить настройки в штатный режим")
    parser.add_argument("--status", action="store_true", help="Вывести текущий сетевой статус и выйти")
    parser.add_argument("--no-uac", action="store_true", help="Не запрашивать повышение прав через UAC")

    args = parser.parse_args()

    # 1. Быстрый статус только для чтения (не требует прав администратора)
    if args.status:
        st = NetworkManager.get_system_status()
        print(f"Status: {'ACTIVE' if st['is_active'] else 'DIRECT'}")
        print(f"System Proxy: {'Enabled' if st['proxy_enable'] else 'Disabled'} ({st['proxy_server']})")
        print(f"WinHTTP: {st['winhttp']}")
        print(f"Env HTTP_PROXY: {st['env_http']}")
        print(f"Git Proxy: {st['git_proxy']}")
        print(f"TTL: {st['ttl']}")
        sys.exit(0)

    # 2. Проверка прав администратора
    if not is_admin() and not args.no_uac:
        elevated = elevate_privileges()
        if not elevated:
            msg = (
                "Внимание: программа не имеет прав администратора!\n"
                "Сетевые команды (netsh winhttp, изменение TTL) требуют прав администратора.\n"
                "Пожалуйста, подтвердите диалог UAC при запуске."
            )
            print(f"[!] {msg}")
            sys.exit(1)

    # 3. Неинтерактивные команды CLI
    if args.enable:
        NetworkManager.enable_proxy(
            ip=args.enable,
            port=args.port,
            set_ttl=not args.no_ttl,
            check_ping=True,
            log_cb=cli_log
        )
        sys.exit(0)

    if args.reset:
        NetworkManager.reset_proxy(reset_ttl=True, log_cb=cli_log)
        sys.exit(0)

    # 4. Интерактивный CLI-режим
    if args.cli:
        run_cli_interactive()
        sys.exit(0)

    # 5. Графический интерфейс на CustomTkinter
    if not CUSTOMTKINTER_AVAILABLE:
        print("\n[!] Библиотека 'customtkinter' не обнаружена.")
        print("    Для запуска современного графического интерфейса выполните:")
        print("    pip install customtkinter\n")
        print("    Переключение в консольный режим (CLI)...\n")
        run_cli_interactive()
        sys.exit(0)

    # Поддержка четких шрифтов High-DPI Windows 10/11
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    app = ModernProxySwitcherApp()
    app.mainloop()


if __name__ == "__main__":
    main()
