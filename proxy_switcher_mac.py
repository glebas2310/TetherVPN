#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TetherVPN Client for macOS
================================================================================
Монолитный сетевой клиент для macOS (аналог Windows proxy_switcher).
Подключает Mac к Android-смартфону с активным TetherVPN по Wi-Fi / USB-модему,
настраивая системный HTTP/HTTPS прокси, исключения (Bypass), TTL=65 и Git CLI.

Стек: Python 3 + CustomTkinter (Fluent Dark UI).
"""

from __future__ import annotations

import atexit
import ipaddress
import os
import platform
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Tuple, List, Dict, Set

# ------------------------------------------------------------------------------
# Проверка и импорт GUI библиотек
# ------------------------------------------------------------------------------
try:
    import customtkinter as ctk
    CUSTOMTKINTER_AVAILABLE = True
except ImportError:
    ctk = None
    CUSTOMTKINTER_AVAILABLE = False

try:
    import tkinter as tk
    from tkinter import messagebox
    TKINTER_AVAILABLE = True
except ImportError:
    tk = None
    messagebox = None
    TKINTER_AVAILABLE = False


# ==============================================================================
# 1. СЕТЕВОЙ МЕНЕДЖЕР MACOS (NETWORKSETUP / SYSCTL / ROUTE)
# ==============================================================================

class MacOSNetworkManager:
    """
    Управляет системными сетевыми параметрами macOS через встроенные утилиты:
    - networksetup: HTTP/HTTPS прокси, список исключений (bypass)
    - route & networksetup: поиск активного шлюза и привязка к системной службе
    - sysctl & osascript: фиксация TTL = 65 (маскировка раздачи перед оператором)
    - git: глобальный HTTP/HTTPS прокси для терминала
    - dscacheutil: очистка локального кэша DNS
    """

    DEFAULT_BYPASS_DOMAINS = [
        "127.0.0.1",
        "localhost",
        "*.local",
        "192.168.0.0/16",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "*.apple.com",
        "captive.apple.com",
    ]

    @staticmethod
    def run_cmd(cmd: List[str], timeout: float = 6.0) -> Tuple[int, str, str]:
        """Безопасный запуск системной консольной команды с защитой от сбоев кодировки."""
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False
            )
            return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
        except subprocess.TimeoutExpired:
            return -1, "", f"Превышен таймаут выполнения команды ({timeout}c)"
        except FileNotFoundError:
            return -1, "", f"Команда не найдена: {cmd[0]}"
        except Exception as exc:
            return -1, "", str(exc)

    @classmethod
    def run_applescript_sudo(cls, command_str: str) -> Tuple[bool, str]:
        """
        Выполняет shell-команду с правами администратора через системное окно Touch ID / пароля macOS.
        Корректно обрабатывает отказ пользователя (код ошибки -128).
        Возвращает (успех, сообщение).
        """
        # Экранируем обратные слэши и двойные кавычки для do shell script
        safe_cmd = command_str.replace('\\', '\\\\').replace('"', '\\"')
        script = f'do shell script "{safe_cmd}" with administrator privileges'
        code, out, err = cls.run_cmd(["osascript", "-e", script], timeout=35.0)

        if code == 0:
            return True, out

        # Код -128 в AppleScript означает "User canceled" (нажал Отмена)
        if "-128" in err or "-128" in out:
            return False, "Пользователь отменил ввод системного пароля (код -128)"

        return False, err or out or "Ошибка выполнения привилегированной команды"

    @classmethod
    def get_default_gateway_and_device(cls) -> Tuple[Optional[str], Optional[str]]:
        """
        Определяет IP-адрес основного шлюза (телефон) и BSD-имя активного интерфейса (например en0).
        Использует системную таблицу маршрутизации macOS `route -n get default`
        (с поддержкой обнаружения шлюза на Windows в режиме предварительного просмотра).
        """
        if platform.system() == "Windows":
            try:
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                p = subprocess.run(["ipconfig"], capture_output=True, text=True, errors="replace", creationflags=flags)
                matches = re.findall(r"(?:Основной шлюз|Default Gateway)[ .:]+([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", p.stdout)
                for g in matches:
                    if g != "0.0.0.0":
                        return g, "en0 (Wi-Fi)"
            except Exception:
                pass
            return "192.168.43.1", "en0 (Wi-Fi)"

        # Сначала запрашиваем IPv4 маршрут по умолчанию в macOS
        code, stdout, _ = cls.run_cmd(["route", "-n", "get", "-inet", "default"], timeout=3.0)
        if code != 0 or not stdout:
            code, stdout, _ = cls.run_cmd(["route", "-n", "get", "default"], timeout=3.0)
            if code != 0 or not stdout:
                return None, None

        gateway: Optional[str] = None
        device: Optional[str] = None

        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("gateway:"):
                parts = line.split(":", 1)
                if len(parts) == 2:
                    gateway = parts[1].strip()
            elif line.startswith("interface:"):
                parts = line.split(":", 1)
                if len(parts) == 2:
                    device = parts[1].strip()

        return gateway, device

    @classmethod
    def map_device_to_service(cls, target_device: str) -> Optional[str]:
        """
        Преобразует системное BSD-имя адаптера (например, 'en0')
        в точное имя сетевой службы macOS (например, 'Wi-Fi' или 'iPhone USB').
        """
        if not target_device:
            return None

        # 1. Приоритетный поиск через порядок сетевых служб (учитывает переименованные службы)
        code, stdout, _ = cls.run_cmd(["networksetup", "-listnetworkserviceorder"], timeout=4.0)
        if code == 0 and stdout:
            current_service = None
            for line in stdout.splitlines():
                line = line.strip()
                # Строка формата: (1) Wi-Fi  или  (2) iPhone USB
                m_srv = re.match(r"^\(\d+\)\s+(.+)$", line)
                if m_srv:
                    current_service = m_srv.group(1).strip()
                elif current_service and f"Device: {target_device}" in line:
                    return current_service

        # 2. Запасной поиск через список аппаратных портов
        code, stdout, _ = cls.run_cmd(["networksetup", "-listallhardwareports"], timeout=4.0)
        if code == 0 and stdout:
            current_port = None
            for line in stdout.splitlines():
                line = line.strip()
                if line.startswith("Hardware Port:"):
                    current_port = line.split(":", 1)[1].strip()
                elif line.startswith("Device:"):
                    dev = line.split(":", 1)[1].strip()
                    if dev == target_device and current_port:
                        return current_port

        return None

    @classmethod
    def get_all_services(cls) -> List[str]:
        """Возвращает список всех зарегистрированных сетевых служб macOS."""
        code, stdout, _ = cls.run_cmd(["networksetup", "-listallnetworkservices"], timeout=3.0)
        services: List[str] = []
        if code == 0 and stdout:
            for line in stdout.splitlines():
                line = line.strip()
                if line and not line.startswith("*") and "denotes" not in line.lower():
                    services.append(line)

        if not services:
            services = ["Wi-Fi"]
        return services

    @classmethod
    def get_active_network_service(cls) -> str:
        """
        Определяет текущий активный сетевой сервис macOS.
        Если невозможно сопоставить интерфейс со службой, возвращает 'Wi-Fi' по умолчанию.
        """
        _, device = cls.get_default_gateway_and_device()
        if device:
            service = cls.map_device_to_service(device)
            if service:
                return service

        # Запасной вариант: берем первый доступный сервис из списка
        services = cls.get_all_services()
        for s in services:
            if "Wi-Fi" in s:
                return s
        if services:
            return services[0]

        return "Wi-Fi"

    @classmethod
    def get_current_ttl(cls) -> int:
        """Возвращает текущее системное значение TTL через sysctl (не требует прав root)."""
        code, stdout, _ = cls.run_cmd(["sysctl", "-n", "net.inet.ip.ttl"], timeout=2.0)
        if code == 0 and stdout.strip().isdigit():
            return int(stdout.strip())
        return 64

    @classmethod
    def set_system_ttl(cls, ttl: int = 65) -> Tuple[bool, str]:
        """
        Устанавливает системный TTL через sysctl с правами администратора.
        Если TTL уже равен целевому значению, пароль не запрашивается.
        """
        if platform.system() != "Darwin":
            return True, f"TTL {ttl} симулирован (на macOS выполняется sysctl -w net.inet.ip.ttl={ttl})"

        current = cls.get_current_ttl()
        if current == ttl:
            return True, f"TTL уже установлен на {ttl}"

        cmd = f"sysctl -w net.inet.ip.ttl={ttl}"
        ok, msg = cls.run_applescript_sudo(cmd)
        if ok:
            return True, f"TTL успешно зафиксирован на {ttl}"
        return False, msg

    @classmethod
    def enable_proxy(
        cls,
        service: str,
        ip: str,
        port: int | str,
        bypass_domains: Optional[List[str]] = None
    ) -> Tuple[bool, List[str]]:
        """
        Активирует системный HTTP и HTTPS прокси для указанной сетевой службы macOS.
        """
        logs: List[str] = []
        port_str = str(port)
        bypass = bypass_domains or cls.DEFAULT_BYPASS_DOMAINS

        if platform.system() != "Darwin":
            logs.append(f"HTTP прокси активирован [Windows Preview]: {ip}:{port_str} ({service})")
            logs.append(f"HTTPS прокси активирован [Windows Preview]: {ip}:{port_str} ({service})")
            logs.append(f"Исключения настроены ({len(bypass)} доменов)")
            return True, logs

        safe_srv = service.replace('\\', '\\\\').replace('"', '\\"')

        # 1. HTTP прокси (-setwebproxy "<Service>" <IP> <PORT>)
        c1, _, e1 = cls.run_cmd(["networksetup", "-setwebproxy", service, ip, port_str])
        if c1 != 0:
            ok, msg = cls.run_applescript_sudo(f'networksetup -setwebproxy "{safe_srv}" {ip} {port_str}')
            if not ok:
                logs.append(f"Ошибка настройки WebProxy: {e1 or msg}")
                return False, logs

        c1_on, _, _ = cls.run_cmd(["networksetup", "-setwebproxystate", service, "on"])
        if c1_on != 0:
            cls.run_applescript_sudo(f'networksetup -setwebproxystate "{safe_srv}" on')
        logs.append(f"HTTP прокси активирован: {ip}:{port_str} ({service})")

        # 2. HTTPS (Secure Web) прокси (-setsecurewebproxy "<Service>" <IP> <PORT>)
        c2, _, e2 = cls.run_cmd(["networksetup", "-setsecurewebproxy", service, ip, port_str])
        if c2 != 0:
            cls.run_applescript_sudo(f'networksetup -setsecurewebproxy "{safe_srv}" {ip} {port_str}')

        c2_on, _, _ = cls.run_cmd(["networksetup", "-setsecurewebproxystate", service, "on"])
        if c2_on != 0:
            cls.run_applescript_sudo(f'networksetup -setsecurewebproxystate "{safe_srv}" on')
        logs.append(f"HTTPS прокси активирован: {ip}:{port_str} ({service})")

        # 3. Список исключений (-setproxybypassdomains "<Service>" ...)
        cmd_bypass = ["networksetup", "-setproxybypassdomains", service] + bypass
        c3, _, e3 = cls.run_cmd(cmd_bypass)
        if c3 != 0:
            domains_joined = " ".join(f'"{d}"' for d in bypass)
            cls.run_applescript_sudo(f'networksetup -setproxybypassdomains "{safe_srv}" {domains_joined}')
        logs.append(f"Исключения настроены ({len(bypass)} доменов)")

        return True, logs

    @classmethod
    def disable_proxy(cls, service: str) -> Tuple[bool, List[str]]:
        """
        Отключает системный HTTP и HTTPS прокси для указанного сетевого сервиса.
        """
        logs: List[str] = []
        if platform.system() != "Darwin":
            logs.append(f"HTTP прокси отключен [Windows Preview] ({service})")
            logs.append(f"HTTPS прокси отключен [Windows Preview] ({service})")
            return True, logs

        safe_srv = service.replace('\\', '\\\\').replace('"', '\\"')

        # Отключение HTTP прокси (-setwebproxystate "<Service>" off)
        c1, _, e1 = cls.run_cmd(["networksetup", "-setwebproxystate", service, "off"])
        if c1 == 0:
            logs.append(f"HTTP прокси отключен ({service})")
        else:
            ok, _ = cls.run_applescript_sudo(f'networksetup -setwebproxystate "{safe_srv}" off')
            if ok:
                logs.append(f"HTTP прокси отключен через admin ({service})")
            else:
                logs.append(f"Не удалось отключить HTTP прокси: {e1}")

        # Отключение HTTPS прокси (-setsecurewebproxystate "<Service>" off)
        c2, _, e2 = cls.run_cmd(["networksetup", "-setsecurewebproxystate", service, "off"])
        if c2 == 0:
            logs.append(f"HTTPS прокси отключен ({service})")
        else:
            ok, _ = cls.run_applescript_sudo(f'networksetup -setsecurewebproxystate "{safe_srv}" off')
            if ok:
                logs.append(f"HTTPS прокси отключен через admin ({service})")
            else:
                logs.append(f"Не удалось отключить HTTPS прокси: {e2}")

        return True, logs

    @classmethod
    def get_service_proxy_status(cls, service: str) -> Dict[str, any]:
        """Возвращает текущее состояние прокси для сетевой службы."""
        status = {
            "http_enabled": False,
            "https_enabled": False,
            "ip": "",
            "port": ""
        }
        # Web Proxy
        c1, out1, _ = cls.run_cmd(["networksetup", "-getwebproxy", service], timeout=2.0)
        if c1 == 0 and out1:
            for line in out1.splitlines():
                if "Enabled: Yes" in line:
                    status["http_enabled"] = True
                elif line.startswith("Server:"):
                    status["ip"] = line.split(":", 1)[1].strip()
                elif line.startswith("Port:"):
                    status["port"] = line.split(":", 1)[1].strip()

        # Secure Web Proxy
        c2, out2, _ = cls.run_cmd(["networksetup", "-getsecurewebproxy", service], timeout=2.0)
        if c2 == 0 and out2:
            for line in out2.splitlines():
                if "Enabled: Yes" in line:
                    status["https_enabled"] = True

        return status

    @classmethod
    def enable_git_proxy(cls, ip: str, port: int | str) -> Tuple[bool, str]:
        """Настраивает глобальный HTTP/HTTPS прокси в Git."""
        proxy_url = f"http://{ip}:{port}"
        c1, _, e1 = cls.run_cmd(["git", "config", "--global", "http.proxy", proxy_url])
        c2, _, e2 = cls.run_cmd(["git", "config", "--global", "https.proxy", proxy_url])
        if c1 == 0 and c2 == 0:
            return True, f"Git настроен на {proxy_url}"
        return False, f"Ошибка Git: {e1 or e2}"

    @classmethod
    def disable_git_proxy(cls) -> Tuple[bool, str]:
        """Сбрасывает настройки прокси в Git."""
        cls.run_cmd(["git", "config", "--global", "--unset", "http.proxy"])
        cls.run_cmd(["git", "config", "--global", "--unset", "https.proxy"])
        return True, "Настройки Git прокси очищены"

    @classmethod
    def flush_dns(cls) -> Tuple[bool, str]:
        """Сбрасывает локальный кэш DNS в macOS (или Windows в режиме предпросмотра)."""
        if platform.system() != "Darwin":
            cls.run_cmd(["ipconfig", "/flushdns"])
            return True, "Кэш DNS успешно очищен (ipconfig /flushdns)"

        c, _, err = cls.run_cmd(["dscacheutil", "-flushcache"])
        cls.run_cmd(["killall", "-HUP", "mDNSResponder"])
        if c == 0:
            return True, "Кэш DNS успешно очищен"
        return False, err or "Не удалось очистить кэш DNS"


# ==============================================================================
# 2. СКАНЕР СЕТИ И ДИАГНОСТИКА СОЕДИНЕНИЯ
# ==============================================================================

class NetworkScanner:
    """
    Сканирует локальную сеть macOS, определяет IP смартфона (TetherVPN)
    и проверяет доступность через контрольные HTTP/TCP запросы.
    """

    @staticmethod
    def is_valid_ipv4(ip: str) -> bool:
        """Проверяет корректность IPv4 адреса."""
        try:
            ipaddress.IPv4Address(ip.strip())
            return True
        except (ValueError, AttributeError):
            return False

    @staticmethod
    def check_tcp_port(ip: str, port: int, timeout: float = 1.0) -> Tuple[bool, float]:
        """
        Проверяет открытость TCP-порта и замеряет время отклика (пинг в миллисекундах).
        """
        t_start = time.perf_counter()
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                latency = (time.perf_counter() - t_start) * 1000.0
                return True, round(latency, 1)
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False, 0.0

    @classmethod
    def test_http_proxy(cls, ip: str, port: int | str, timeout: float = 3.5) -> Tuple[bool, float, str]:
        """
        Выполняет контрольный запрос через HTTP-прокси к легковесному endpoint'у
        для проверки реального выхода в интернет через VPN смартфона.
        """
        try:
            port_num = int(port)
        except ValueError:
            return False, 0.0, f"Некорректный порт: {port}"

        tcp_ok, tcp_ping = cls.check_tcp_port(ip, port_num, timeout=1.5)
        if not tcp_ok:
            return False, 0.0, f"Порт {port_num} на {ip} закрыт или не отвечает"

        proxy_handler = urllib.request.ProxyHandler({
            "http": f"http://{ip}:{port_num}",
            "https": f"http://{ip}:{port_num}",
        })
        opener = urllib.request.build_opener(proxy_handler)

        check_urls = [
            "http://connectivitycheck.gstatic.com/generate_204",
            "http://cp.cloudflare.com/",
            "http://detectportal.firefox.com/success.txt",
        ]

        for test_url in check_urls:
            try:
                t0 = time.perf_counter()
                req = urllib.request.Request(
                    test_url,
                    headers={"User-Agent": "TetherVPN-MacClient/2.0"}
                )
                with opener.open(req, timeout=timeout) as resp:
                    elapsed = (time.perf_counter() - t0) * 1000.0
                    if resp.status in (200, 204):
                        return True, round(elapsed, 1), f"VPN работает! Пинг: {round(elapsed, 1)} мс"
            except Exception:
                continue

        # Если интернет-запрос не прошел, но порт отвечает
        return True, tcp_ping, f"Прокси отвечает по TCP ({tcp_ping} мс), но интернет-доступ ограничен"

    @classmethod
    def get_candidate_ips(cls, current_ip: str = "") -> List[Tuple[str, str]]:
        """
        Формирует приоритетный список кандидатов на IP телефона:
        1. Текущий введенный пользователем адрес.
        2. Системный шлюз по умолчанию (route default).
        3. Стандартные шлюзы Hotspot Android (192.168.43.1, 192.168.42.129, 172.20.10.1 и др.).
        4. Соседи из таблицы ARP.
        """
        candidates: List[Tuple[str, str]] = []
        seen: Set[str] = set()

        def add_cand(ip_addr: str, desc: str):
            ip_str = ip_addr.strip()
            if ip_str and cls.is_valid_ipv4(ip_str) and ip_str not in seen:
                if not ip_str.startswith("127.") and ip_str != "0.0.0.0":
                    seen.add(ip_str)
                    candidates.append((ip_str, desc))

        # 1. Текущий ввод
        if current_ip:
            add_cand(current_ip, "Текущий ввод")

        # 2. Шлюз по умолчанию
        gw, _ = MacOSNetworkManager.get_default_gateway_and_device()
        if gw:
            add_cand(gw, "Шлюз по умолчанию")

        # 3. Популярные шлюзы раздачи смартфонов
        add_cand("192.168.43.1", "Android Wi-Fi Hotspot")
        add_cand("192.168.42.129", "Android USB Модем")
        add_cand("172.20.10.1", "Apple iPhone Hotspot")
        add_cand("192.168.44.1", "Xiaomi / HyperOS Hotspot")
        add_cand("192.168.225.1", "Samsung Hotspot")
        add_cand("10.42.0.1", "LineageOS Hotspot")
        add_cand("192.168.43.254", "Альтернативный Android шлюз")

        # 4. ARP таблица соседей
        code, arp_out, _ = MacOSNetworkManager.run_cmd(["arp", "-a", "-n"], timeout=2.5)
        if code == 0 and arp_out:
            matches = re.findall(r"\(([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)\)", arp_out)
            for m in matches:
                add_cand(m, "Устройство из ARP таблицы")

        return candidates

    @classmethod
    def autosearch_proxy(cls, port: int = 8080) -> Optional[Tuple[str, float]]:
        """
        Параллельно сканирует кандидатов и находит первый ответивший прокси TetherVPN.
        """
        candidates = cls.get_candidate_ips()
        ip_list = [ip for ip, _ in candidates]

        with ThreadPoolExecutor(max_workers=min(12, len(ip_list) or 1)) as pool:
            future_to_ip = {
                pool.submit(cls.check_tcp_port, ip, port, 0.85): ip
                for ip in ip_list
            }
            for fut in as_completed(future_to_ip):
                ip = future_to_ip[fut]
                try:
                    ok, latency = fut.result()
                    if ok:
                        return ip, latency
                except Exception:
                    pass

        return None


# ==============================================================================
# 3. ПОЛЬЗОВАТЕЛЬСКИЙ ИНТЕРФЕЙС (FLUENT DARK CUSTOMTKINTER ДЛЯ MACOS)
# ==============================================================================

class TetherVPNMacApp(ctk.CTk if CUSTOMTKINTER_AVAILABLE else (tk.Tk if TKINTER_AVAILABLE else object)):
    """
    Графический интерфейс TetherVPN для macOS в дизайне Fluent Dark.
    """

    # Палитра дизайна Fluent Dark
    COLOR_BG = "#111317"          # Глубокий темный фон
    COLOR_CARD = "#1C1E24"        # Фон карточек
    COLOR_CARD_ALT = "#16181D"    # Внутренние мини-панели
    COLOR_BORDER = "#282A30"      # Рамки блоков
    COLOR_PRIMARY = "#0078D4"     # Акцентный синий macOS / Fluent
    COLOR_PRIMARY_HOVER = "#106EBE"
    COLOR_SUCCESS = "#107C41"     # Изумрудный / Включено
    COLOR_SUCCESS_HOVER = "#0F6B38"
    COLOR_DANGER = "#D83B01"      # Кармин / Отключено
    COLOR_DANGER_HOVER = "#A80000"
    COLOR_TEXT = "#FFFFFF"        # Основной текст
    COLOR_TEXT_MUTED = "#8A8D93"  # Подписи и вторичный текст
    COLOR_INPUT_BG = "#15171C"    # Поля ввода
    COLOR_INPUT_BORDER = "#282A30"

    FONT_FAMILY = "SF Pro" if platform.system() == "Darwin" else "Segoe UI"
    FONT_MONO = "SF Mono" if platform.system() == "Darwin" else "Consolas"

    def __init__(self):
        if not CUSTOMTKINTER_AVAILABLE and not TKINTER_AVAILABLE:
            raise RuntimeError("Для работы требуется библиотека customtkinter или tkinter. Выполните: pip install customtkinter")

        super().__init__()

        # Состояние приложения
        self.is_connected = False
        self.active_service = MacOSNetworkManager.get_active_network_service()
        self.configured_service: Optional[str] = None
        self.initial_ttl = MacOSNetworkManager.get_current_ttl()
        self.ttl_applied = False
        self.git_applied = False
        self.is_busy = False

        # Переменные UI
        self.var_ip = ctk.StringVar(value="192.168.43.1")
        self.var_port = ctk.StringVar(value="8080")
        self.var_ttl = ctk.BooleanVar(value=True)
        self.var_git = ctk.BooleanVar(value=False)
        self.var_service = ctk.StringVar(value=self.active_service)

        # Конфигурация окна
        self.title("TetherVPN — macOS Client")
        self.geometry("640x730")
        self.resizable(False, False)
        if CUSTOMTKINTER_AVAILABLE:
            ctk.set_appearance_mode("dark")
            self.configure(fg_color=self.COLOR_BG)

        self._center_window(640, 730)
        self._build_ui()

        # Безопасный выход, перехват сигналов и откат параметров
        self.protocol("WM_DELETE_WINDOW", self.on_window_close)
        atexit.register(self.safe_cleanup_on_exit)
        try:
            signal.signal(signal.SIGINT, self._signal_handler)
            signal.signal(signal.SIGTERM, self._signal_handler)
        except (ValueError, AttributeError):
            pass
        self._schedule_signal_check()

        # Автоматический старт: поиск шлюза и проверка статуса
        self.after(200, self.auto_detect_gateway)

    def _schedule_signal_check(self):
        """Периодический тик для своевременной обработки сигналов Python в цикле Tkinter."""
        self.after(500, self._schedule_signal_check)

    def _signal_handler(self, signum, frame):
        """Перехват SIGINT и SIGTERM для немедленного отката сети и выхода."""
        print(f"\n[TetherVPN] Получен сигнал {signum}. Экстренное завершение и откат сети...")
        self.safe_cleanup_on_exit()
        try:
            self.destroy()
        except Exception:
            pass
        sys.exit(0)

    def _center_window(self, width: int, height: int):
        """Центрирует окно на экране пользователя."""
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x = max(0, int((sw - width) / 2))
        y = max(0, int((sh - height) / 2))
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _build_ui(self):
        """Создает элементы интерфейса Fluent Dark."""
        self.main_container = ctk.CTkFrame(self, fg_color="transparent")
        self.main_container.pack(fill="both", expand=True, padx=20, pady=18)

        # ==================== 1. ШАПКА (HEADER) ====================
        header = ctk.CTkFrame(self.main_container, fg_color="transparent")
        header.pack(fill="x", pady=(0, 14))

        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.pack(side="left")

        lbl_icon = ctk.CTkLabel(
            title_box,
            text="📱",
            font=ctk.CTkFont(family=self.FONT_FAMILY, size=24)
        )
        lbl_icon.pack(side="left", padx=(0, 8))

        lbl_title = ctk.CTkLabel(
            title_box,
            text="TetherVPN",
            font=ctk.CTkFont(family=self.FONT_FAMILY, size=20, weight="bold"),
            text_color=self.COLOR_TEXT
        )
        lbl_title.pack(side="left")

        # Бейдж платформы
        badge_platform = ctk.CTkFrame(title_box, fg_color="#242730", corner_radius=6)
        badge_platform.pack(side="left", padx=(8, 0))
        lbl_badge = ctk.CTkLabel(
            badge_platform,
            text="macOS Client v2.0",
            font=ctk.CTkFont(family=self.FONT_FAMILY, size=11, weight="bold"),
            text_color="#89B4FA"
        )
        lbl_badge.pack(padx=8, pady=2)

        # Бейдж общего статуса (справа в шапке)
        self.status_badge = ctk.CTkFrame(
            header,
            fg_color="#242730",
            corner_radius=14,
            border_width=1,
            border_color=self.COLOR_BORDER
        )
        self.status_badge.pack(side="right")
        self.lbl_status_badge = ctk.CTkLabel(
            self.status_badge,
            text="● Отключено",
            font=ctk.CTkFont(family=self.FONT_FAMILY, size=11, weight="bold"),
            text_color=self.COLOR_TEXT_MUTED
        )
        self.lbl_status_badge.pack(padx=12, pady=4)

        # ==================== 2. КАРТОЧКА ПАРАМЕТРОВ ====================
        card_params = ctk.CTkFrame(
            self.main_container,
            fg_color=self.COLOR_CARD,
            corner_radius=12,
            border_width=1,
            border_color=self.COLOR_BORDER
        )
        card_params.pack(fill="x", pady=(0, 12))

        # Заголовок карточки и кнопка автопоиска
        card_hdr = ctk.CTkFrame(card_params, fg_color="transparent")
        card_hdr.pack(fill="x", padx=16, pady=(12, 10))

        lbl_card_title = ctk.CTkLabel(
            card_hdr,
            text="ПАРАМЕТРЫ СЕРВЕРА (ТЕЛЕФОН)",
            font=ctk.CTkFont(family=self.FONT_FAMILY, size=11, weight="bold"),
            text_color=self.COLOR_TEXT_MUTED
        )
        lbl_card_title.pack(side="left")

        self.btn_autosearch = ctk.CTkButton(
            card_hdr,
            text="🔍 Автопоиск в сети",
            width=150,
            height=26,
            corner_radius=6,
            font=ctk.CTkFont(family=self.FONT_FAMILY, size=11, weight="bold"),
            fg_color=self.COLOR_PRIMARY,
            hover_color=self.COLOR_PRIMARY_HOVER,
            command=self.on_autosearch_clicked
        )
        self.btn_autosearch.pack(side="right")

        # Поля ввода IP и Порта
        inputs_row = ctk.CTkFrame(card_params, fg_color="transparent")
        inputs_row.pack(fill="x", padx=16, pady=(0, 10))

        # Поле IP (дефолт 192.168.43.1)
        box_ip = ctk.CTkFrame(inputs_row, fg_color="transparent")
        box_ip.pack(side="left", fill="x", expand=True, padx=(0, 10))

        lbl_ip = ctk.CTkLabel(
            box_ip,
            text="IP-адрес телефона (из TetherVPN):",
            font=ctk.CTkFont(family=self.FONT_FAMILY, size=11, weight="bold"),
            text_color=self.COLOR_TEXT
        )
        lbl_ip.pack(anchor="w", pady=(0, 4))

        self.entry_ip = ctk.CTkEntry(
            box_ip,
            textvariable=self.var_ip,
            placeholder_text="192.168.43.1",
            placeholder_text_color=self.COLOR_TEXT_MUTED,
            height=38,
            corner_radius=8,
            fg_color=self.COLOR_INPUT_BG,
            border_color=self.COLOR_INPUT_BORDER,
            text_color=self.COLOR_TEXT,
            font=ctk.CTkFont(family=self.FONT_FAMILY, size=13)
        )
        self.entry_ip.pack(fill="x")

        # Поле Порт (8080)
        box_port = ctk.CTkFrame(inputs_row, fg_color="transparent")
        box_port.pack(side="left", padx=(0, 10))

        lbl_port = ctk.CTkLabel(
            box_port,
            text="Порт:",
            font=ctk.CTkFont(family=self.FONT_FAMILY, size=11, weight="bold"),
            text_color=self.COLOR_TEXT
        )
        lbl_port.pack(anchor="w", pady=(0, 4))

        self.entry_port = ctk.CTkEntry(
            box_port,
            textvariable=self.var_port,
            placeholder_text="8080",
            placeholder_text_color=self.COLOR_TEXT_MUTED,
            width=80,
            height=38,
            corner_radius=8,
            fg_color=self.COLOR_INPUT_BG,
            border_color=self.COLOR_INPUT_BORDER,
            text_color=self.COLOR_TEXT,
            font=ctk.CTkFont(family=self.FONT_FAMILY, size=13)
        )
        self.entry_port.pack()

        # Кнопка быстрой проверки
        box_check = ctk.CTkFrame(inputs_row, fg_color="transparent")
        box_check.pack(side="right")

        lbl_check_pad = ctk.CTkLabel(
            box_check,
            text="Проверка:",
            font=ctk.CTkFont(family=self.FONT_FAMILY, size=11, weight="bold"),
            text_color=self.COLOR_TEXT_MUTED
        )
        lbl_check_pad.pack(anchor="w", pady=(0, 4))

        self.btn_check = ctk.CTkButton(
            box_check,
            text="⚡ Проверить",
            width=100,
            height=38,
            corner_radius=8,
            font=ctk.CTkFont(family=self.FONT_FAMILY, size=11, weight="bold"),
            fg_color="#1E293B",
            hover_color="#334155",
            border_width=1,
            border_color="#38BDF8",
            text_color="#38BDF8",
            command=self.on_check_connection_clicked
        )
        self.btn_check.pack()

        # Карточка диагностики
        self.diag_box = ctk.CTkFrame(
            card_params,
            fg_color=self.COLOR_CARD_ALT,
            corner_radius=8,
            border_width=1,
            border_color=self.COLOR_BORDER
        )
        self.diag_box.pack(fill="x", padx=16, pady=(0, 10))

        self.lbl_diag = ctk.CTkLabel(
            self.diag_box,
            text="💡 Включите раздачу в приложении TetherVPN на телефоне и нажмите «Подключить».",
            font=ctk.CTkFont(family=self.FONT_FAMILY, size=11),
            text_color=self.COLOR_TEXT_MUTED,
            anchor="w",
            justify="left"
        )
        self.lbl_diag.pack(fill="x", padx=12, pady=6)

        # Чекбоксы настроек (TTL и Git)
        opts_frame = ctk.CTkFrame(card_params, fg_color="transparent")
        opts_frame.pack(fill="x", padx=16, pady=(0, 12))

        self.chk_ttl = ctk.CTkCheckBox(
            opts_frame,
            text="Маскировка TTL (65) — обход ограничений раздачи мобильных операторов",
            variable=self.var_ttl,
            font=ctk.CTkFont(family=self.FONT_FAMILY, size=11),
            text_color=self.COLOR_TEXT,
            fg_color=self.COLOR_PRIMARY,
            hover_color=self.COLOR_PRIMARY_HOVER,
            corner_radius=5,
            checkbox_width=18,
            checkbox_height=18
        )
        self.chk_ttl.pack(anchor="w", pady=(0, 6))

        self.chk_git = ctk.CTkCheckBox(
            opts_frame,
            text="Проксировать Git и Терминал (git config --global http.proxy)",
            variable=self.var_git,
            font=ctk.CTkFont(family=self.FONT_FAMILY, size=11),
            text_color=self.COLOR_TEXT,
            fg_color=self.COLOR_PRIMARY,
            hover_color=self.COLOR_PRIMARY_HOVER,
            corner_radius=5,
            checkbox_width=18,
            checkbox_height=18
        )
        self.chk_git.pack(anchor="w")

        # ==================== 3. ГЛАВНАЯ КНОПКА ДЕЙСТВИЯ ====================
        self.btn_toggle = ctk.CTkButton(
            self.main_container,
            text="⚡ Подключить системный прокси",
            height=46,
            corner_radius=10,
            font=ctk.CTkFont(family=self.FONT_FAMILY, size=13, weight="bold"),
            fg_color=self.COLOR_SUCCESS,
            hover_color=self.COLOR_SUCCESS_HOVER,
            text_color="#FFFFFF",
            command=self.on_toggle_clicked
        )
        self.btn_toggle.pack(fill="x", pady=(0, 12))

        # ==================== 4. КАРТОЧКА СТАТУСА MACOS ====================
        card_status = ctk.CTkFrame(
            self.main_container,
            fg_color=self.COLOR_CARD,
            corner_radius=12,
            border_width=1,
            border_color=self.COLOR_BORDER
        )
        card_status.pack(fill="x", pady=(0, 12))

        lbl_stat_hdr = ctk.CTkLabel(
            card_status,
            text="СОСТОЯНИЕ СИСТЕМЫ MACOS",
            font=ctk.CTkFont(family=self.FONT_FAMILY, size=11, weight="bold"),
            text_color=self.COLOR_TEXT_MUTED
        )
        lbl_stat_hdr.pack(anchor="w", padx=16, pady=(10, 6))

        stat_grid = ctk.CTkFrame(card_status, fg_color="transparent")
        stat_grid.pack(fill="x", padx=16, pady=(0, 10))

        # Колонка 1: Сетевой сервис
        col1 = ctk.CTkFrame(stat_grid, fg_color=self.COLOR_CARD_ALT, corner_radius=8)
        col1.pack(side="left", fill="both", expand=True, padx=(0, 6), pady=2)
        ctk.CTkLabel(
            col1,
            text="Сетевой сервис",
            font=ctk.CTkFont(family=self.FONT_FAMILY, size=10),
            text_color=self.COLOR_TEXT_MUTED
        ).pack(pady=(6, 2))

        self.opt_service = ctk.CTkOptionMenu(
            col1,
            variable=self.var_service,
            values=[self.active_service],
            height=26,
            corner_radius=6,
            font=ctk.CTkFont(family=self.FONT_FAMILY, size=11, weight="bold"),
            fg_color="#242730",
            button_color="#2D313D",
            button_hover_color=self.COLOR_PRIMARY,
            text_color=self.COLOR_TEXT,
            dropdown_fg_color=self.COLOR_CARD,
            dropdown_hover_color=self.COLOR_PRIMARY_HOVER,
            dropdown_text_color=self.COLOR_TEXT,
            command=self.on_service_changed
        )
        self.opt_service.pack(pady=(0, 6), padx=8)

        # Колонка 2: Шлюз по умолчанию
        col2 = ctk.CTkFrame(stat_grid, fg_color=self.COLOR_CARD_ALT, corner_radius=8)
        col2.pack(side="left", fill="both", expand=True, padx=4, pady=2)
        ctk.CTkLabel(
            col2,
            text="Шлюз по умолчанию",
            font=ctk.CTkFont(family=self.FONT_FAMILY, size=10),
            text_color=self.COLOR_TEXT_MUTED
        ).pack(pady=(6, 0))
        self.lbl_val_gateway = ctk.CTkLabel(
            col2,
            text="Определение...",
            font=ctk.CTkFont(family=self.FONT_FAMILY, size=12, weight="bold"),
            text_color=self.COLOR_TEXT
        )
        self.lbl_val_gateway.pack(pady=(0, 6))

        # Колонка 3: Задержка (пинг)
        col3 = ctk.CTkFrame(stat_grid, fg_color=self.COLOR_CARD_ALT, corner_radius=8)
        col3.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=2)
        ctk.CTkLabel(
            col3,
            text="Задержка (пинг)",
            font=ctk.CTkFont(family=self.FONT_FAMILY, size=10),
            text_color=self.COLOR_TEXT_MUTED
        ).pack(pady=(6, 0))
        self.lbl_val_ping = ctk.CTkLabel(
            col3,
            text="—",
            font=ctk.CTkFont(family=self.FONT_FAMILY, size=12, weight="bold"),
            text_color=self.COLOR_TEXT
        )
        self.lbl_val_ping.pack(pady=(0, 6))

        # ==================== 5. ЖУРНАЛ ОПЕРАЦИЙ (LOGS) ====================
        card_log = ctk.CTkFrame(
            self.main_container,
            fg_color=self.COLOR_CARD,
            corner_radius=12,
            border_width=1,
            border_color=self.COLOR_BORDER
        )
        card_log.pack(fill="both", expand=True)

        log_hdr = ctk.CTkFrame(card_log, fg_color="transparent")
        log_hdr.pack(fill="x", padx=16, pady=(8, 4))

        ctk.CTkLabel(
            log_hdr,
            text="ЖУРНАЛ ОПЕРАЦИЙ",
            font=ctk.CTkFont(family=self.FONT_FAMILY, size=11, weight="bold"),
            text_color=self.COLOR_TEXT_MUTED
        ).pack(side="left")

        btn_copy_env = ctk.CTkButton(
            log_hdr,
            text="📋 Копировать экспорт для терминала",
            width=210,
            height=22,
            corner_radius=4,
            font=ctk.CTkFont(family=self.FONT_FAMILY, size=10),
            fg_color="#272A34",
            hover_color="#363A47",
            command=self.copy_terminal_export
        )
        btn_copy_env.pack(side="right")

        self.txt_log = ctk.CTkTextbox(
            card_log,
            fg_color=self.COLOR_CARD_ALT,
            text_color="#C0CAF5",
            font=ctk.CTkFont(family=self.FONT_MONO, size=11),
            corner_radius=8,
            border_width=1,
            border_color=self.COLOR_BORDER
        )
        self.txt_log.pack(fill="both", expand=True, padx=16, pady=(0, 10))

        # Первичное приветственное сообщение
        self.log("INFO", "TetherVPN macOS Client запущен.")
        self.log("INFO", f"Активная сетевая служба: {self.active_service}")
        if platform.system() != "Darwin":
            self.log("INFO", "🖥️ Режим предпросмотра на Windows: UI полностью интерактивен.")
            self.log("INFO", "Проверка связи и автопоиск работают в реальной сети через сокеты.")

    def on_service_changed(self, choice: str):
        """Обработка ручного переключения сетевой службы пользователем."""
        self.active_service = choice
        self.log("INFO", f"Выбрана сетевая служба macOS: {choice}")

    def log(self, level: str, message: str):
        """Безопасно выводит форматированное сообщение в лог."""
        timestamp = time.strftime("%H:%M:%S")
        prefix = {
            "OK": "✅ [OK]",
            "INFO": "ℹ️ [ИНФО]",
            "WARN": "⚠️ [ВНИМАНИЕ]",
            "ERR": "❌ [ОШИБКА]",
        }.get(level, f"[{level}]")

        formatted = f"{timestamp} {prefix} {message}\n"
        self.txt_log.insert("end", formatted)
        self.txt_log.see("end")

    # ==================== СЕТЕВЫЕ ОПЕРАЦИИ И ПОТОКИ ====================

    def auto_detect_gateway(self):
        """В фоновом потоке определяет шлюз по умолчанию и актуализирует интерфейс."""
        def worker():
            gw, dev = MacOSNetworkManager.get_default_gateway_and_device()
            service = MacOSNetworkManager.get_active_network_service()
            all_services = MacOSNetworkManager.get_all_services()

            self.after(0, lambda: self._apply_detected_gateway(gw, service, all_services))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_detected_gateway(self, gw: Optional[str], service: str, all_services: List[str]):
        self.active_service = service
        self.var_service.set(service)
        if all_services:
            self.opt_service.configure(values=all_services)

        if gw:
            self.lbl_val_gateway.configure(text=gw)
            curr = self.var_ip.get().strip()
            if not curr or curr == "192.168.43.1":
                self.var_ip.set(gw)
                self.log("INFO", f"Обнаружен системный шлюз: {gw} ({service})")
                self.lbl_diag.configure(
                    text=f"💡 Подставлен шлюз {gw}. Нажмите «⚡ Проверить» или сразу «Подключить».",
                    text_color="#38BDF8"
                )
        else:
            self.lbl_val_gateway.configure(text="Не определен")

    def on_autosearch_clicked(self):
        """Обработчик кнопки автопоиска прокси в локальной сети."""
        if self.is_busy:
            return
        self.is_busy = True
        self.btn_autosearch.configure(state="disabled", text="⏳ Поиск...")
        self.log("INFO", "Сканирование локальной сети на наличие работающего TetherVPN...")

        def worker():
            port_val = int(self.var_port.get().strip() or "8080")
            result = NetworkScanner.autosearch_proxy(port=port_val)

            def update_ui():
                self.is_busy = False
                self.btn_autosearch.configure(state="normal", text="🔍 Автопоиск в сети")
                if result:
                    found_ip, latency = result
                    self.var_ip.set(found_ip)
                    self.lbl_val_ping.configure(text=f"{latency} мс")
                    self.lbl_diag.configure(
                        text=f"✅ Найден TetherVPN: {found_ip}:{port_val} (пинг: {latency} мс)",
                        text_color="#34D399"
                    )
                    self.log("OK", f"Успешно обнаружен TetherVPN на {found_ip}:{port_val} ({latency} мс)")
                else:
                    self.lbl_diag.configure(
                        text="⚠️ Прокси не найден. Убедитесь, что на телефоне включена раздача и TetherVPN.",
                        text_color="#F87171"
                    )
                    self.log("WARN", "Автопоиск не обнаружил активного TetherVPN в локальной сети")

            self.after(0, update_ui)

        threading.Thread(target=worker, daemon=True).start()

    def on_check_connection_clicked(self):
        """Обработчик кнопки проверки связи с сервером."""
        ip = self.var_ip.get().strip()
        port = self.var_port.get().strip() or "8080"

        if not NetworkScanner.is_valid_ipv4(ip):
            messagebox.showwarning("Некорректный IP", "Введите корректный IPv4 адрес (например, 192.168.43.1)")
            return

        self.btn_check.configure(state="disabled", text="⏳ Проверка")
        self.log("INFO", f"Проверка соединения с {ip}:{port}...")

        def worker():
            ok, ping_ms, msg = NetworkScanner.test_http_proxy(ip, port)

            def update_ui():
                self.btn_check.configure(state="normal", text="⚡ Проверить")
                if ok:
                    self.lbl_val_ping.configure(text=f"{ping_ms} мс")
                    self.lbl_diag.configure(text=f"✅ {msg}", text_color="#34D399")
                    self.log("OK", f"Диагностика успешна: {msg}")
                else:
                    self.lbl_val_ping.configure(text="Таймаут")
                    self.lbl_diag.configure(text=f"❌ {msg}", text_color="#F87171")
                    self.log("ERR", f"Ошибка проверки: {msg}")

            self.after(0, update_ui)

        threading.Thread(target=worker, daemon=True).start()

    def on_toggle_clicked(self):
        """Главный переключатель Подключить / Отключить."""
        if self.is_busy:
            return

        if self.is_connected:
            self.disconnect_proxy()
        else:
            self.connect_proxy()

    def connect_proxy(self):
        """Включает системный прокси, TTL и Git в macOS."""
        ip = self.var_ip.get().strip()
        port = self.var_port.get().strip() or "8080"
        target_service = self.var_service.get().strip() or self.active_service

        if not NetworkScanner.is_valid_ipv4(ip):
            messagebox.showerror("Ошибка", "Укажите корректный IPv4 адрес телефона.")
            return

        self.is_busy = True
        self.btn_toggle.configure(state="disabled", text="⏳ Подключение сетевого прокси...")
        self.log("INFO", f"Включение TetherVPN прокси для службы '{target_service}'...")

        def worker():
            self.configured_service = target_service

            # 1. Системный прокси через networksetup
            ok_proxy, proxy_logs = MacOSNetworkManager.enable_proxy(target_service, ip, port)

            # 2. Маскировка TTL = 65 (если чекбокс активен)
            ttl_ok = True
            ttl_msg = ""
            if self.var_ttl.get():
                ttl_ok, ttl_msg = MacOSNetworkManager.set_system_ttl(65)
                self.ttl_applied = ttl_ok
            else:
                self.ttl_applied = False

            # 3. Git CLI прокси (если чекбокс активен)
            git_ok = True
            git_msg = ""
            if self.var_git.get():
                git_ok, git_msg = MacOSNetworkManager.enable_git_proxy(ip, port)
                self.git_applied = git_ok
            else:
                self.git_applied = False

            # 4. Сброс кэша DNS
            MacOSNetworkManager.flush_dns()

            def finish():
                self.is_busy = False
                self.btn_toggle.configure(state="normal")
                for l in proxy_logs:
                    self.log("OK" if ok_proxy else "ERR", l)

                if self.var_ttl.get():
                    self.log("OK" if ttl_ok else "WARN", f"TTL: {ttl_msg}")
                if self.var_git.get():
                    self.log("OK" if git_ok else "WARN", f"Git: {git_msg}")

                if ok_proxy:
                    self.is_connected = True
                    self.btn_toggle.configure(
                        text="🛑 Отключить проксирование",
                        fg_color=self.COLOR_DANGER,
                        hover_color=self.COLOR_DANGER_HOVER
                    )
                    self.lbl_status_badge.configure(text="● Подключено", text_color="#34D399")
                    self.status_badge.configure(border_color=self.COLOR_SUCCESS)
                    self.lbl_diag.configure(
                        text=f"🟢 Прокси активен! Весь интернет Mac направлен через {ip}:{port}",
                        text_color="#34D399"
                    )
                    self.log("OK", "🚀 Трафик macOS успешно перенаправлен через TetherVPN!")
                else:
                    self.btn_toggle.configure(
                        text="⚡ Подключить системный прокси",
                        fg_color=self.COLOR_SUCCESS,
                        hover_color=self.COLOR_SUCCESS_HOVER
                    )
                    self.lbl_status_badge.configure(text="● Отключено", text_color=self.COLOR_TEXT_MUTED)
                    self.status_badge.configure(border_color=self.COLOR_BORDER)

            self.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def disconnect_proxy(self):
        """Отключает системный прокси, восстанавливает TTL и Git."""
        self.is_busy = True
        self.btn_toggle.configure(state="disabled", text="⏳ Отключение...")
        self.log("INFO", "Сброс системных настроек и возврат к прямому интернету...")

        def worker():
            target_service = self.configured_service or self.var_service.get().strip() or self.active_service

            # 1. Отключение прокси в networksetup
            _, logs = MacOSNetworkManager.disable_proxy(target_service)

            # 2. Восстановление исходного TTL (обычно 64) только если он менялся
            if self.ttl_applied:
                target_ttl = self.initial_ttl if self.initial_ttl != 65 else 64
                _, ttl_msg = MacOSNetworkManager.set_system_ttl(target_ttl)
                logs.append(f"TTL: {ttl_msg}")
                self.ttl_applied = False

            # 3. Отключение Git прокси
            if self.git_applied or self.var_git.get():
                _, git_msg = MacOSNetworkManager.disable_git_proxy()
                logs.append(f"Git: {git_msg}")
                self.git_applied = False

            # 4. Очистка кэша DNS
            MacOSNetworkManager.flush_dns()

            def finish():
                self.is_busy = False
                self.is_connected = False
                self.btn_toggle.configure(
                    state="normal",
                    text="⚡ Подключить системный прокси",
                    fg_color=self.COLOR_SUCCESS,
                    hover_color=self.COLOR_SUCCESS_HOVER
                )
                self.lbl_status_badge.configure(text="● Отключено", text_color=self.COLOR_TEXT_MUTED)
                self.status_badge.configure(border_color=self.COLOR_BORDER)
                self.lbl_diag.configure(
                    text="💡 Прокси отключен. Прямое соединение macOS восстановлено.",
                    text_color=self.COLOR_TEXT_MUTED
                )
                for l in logs:
                    self.log("OK", l)
                self.log("OK", "🌟 Сетевые параметры успешно возвращены в исходное состояние.")

            self.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def copy_terminal_export(self):
        """Копирует в буфер обмена команду экспорта переменных прокси для Терминала macOS."""
        ip = self.var_ip.get().strip() or "192.168.43.1"
        port = self.var_port.get().strip() or "8080"
        proxy_url = f"http://{ip}:{port}"
        cmd = f'export http_proxy="{proxy_url}"; export https_proxy="{proxy_url}"; export all_proxy="{proxy_url}"; export HTTP_PROXY="{proxy_url}"; export HTTPS_PROXY="{proxy_url}"; export ALL_PROXY="{proxy_url}"'

        copied = False
        # На macOS нативный способ записи в системный буфер обмена — pbcopy
        if platform.system() == "Darwin":
            try:
                proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
                proc.communicate(cmd.encode("utf-8"))
                copied = True
            except Exception:
                pass

        try:
            self.clipboard_clear()
            self.clipboard_append(cmd)
            self.update()
            copied = True
        except Exception:
            pass

        if copied:
            self.log("INFO", f"Команда для Терминала скопирована: export http_proxy={proxy_url}...")
            messagebox.showinfo(
                "Скопировано в буфер",
                f"Команда экспорта для Терминала скопирована в буфер обмена:\n\n{cmd}"
            )
        else:
            self.log("WARN", "Не удалось скопировать команду в буфер обмена")

    def safe_cleanup_on_exit(self):
        """Гарантированный откат настроек сети при завершении процесса."""
        if self.is_connected:
            self.is_connected = False
            service = self.configured_service or self.var_service.get().strip() or self.active_service or "Wi-Fi"
            print(f"[TetherVPN] Завершение работы: откат системных настроек macOS для '{service}'...")
            MacOSNetworkManager.disable_proxy(service)
            if self.ttl_applied:
                target_ttl = self.initial_ttl if self.initial_ttl != 65 else 64
                MacOSNetworkManager.set_system_ttl(target_ttl)
                self.ttl_applied = False
            if self.git_applied:
                MacOSNetworkManager.disable_git_proxy()
                self.git_applied = False
            MacOSNetworkManager.flush_dns()

    def on_window_close(self):
        """Обработка закрытия окна пользователем."""
        if self.is_connected:
            if messagebox.askyesno(
                "Прокси активен",
                "Системный прокси TetherVPN сейчас активен.\n\nОтключить проксирование и вернуть исходные настройки сети перед выходом?"
            ):
                self.safe_cleanup_on_exit()
            else:
                # Пользователь сознательно решил оставить прокси включенным — сбрасываем флаг,
                # чтобы atexit не отменил это решение при выходе процесса
                self.is_connected = False

        try:
            self.destroy()
        except Exception:
            pass
        sys.exit(0)


# ==============================================================================
# 4. ТОЧКА ВХОДА (CLI И GUI)
# ==============================================================================

def main():
    if platform.system() != "Darwin":
        print(f"[!] Предупреждение: Текущая ОС — {platform.system()}. Скрипт спроектирован для macOS (networksetup, sysctl).")

    app = TetherVPNMacApp()
    app.mainloop()


if __name__ == "__main__":
    main()
