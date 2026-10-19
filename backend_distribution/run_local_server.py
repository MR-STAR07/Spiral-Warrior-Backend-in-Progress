#!/usr/bin/env python3
"""Start the HTTP and WebSocket services for local Spiral Warrior testing."""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT
DATA_ROOT = ROOT / "data"
LOG_ROOT = ROOT / "logs"
ACCOUNT_DB = DATA_ROOT / "local_accounts.sqlite3"
LOOPBACK_AREA_RESPONSE = ROOT / "experiments" / "area-list-localhost.base64"
HTTP_PORT = 18765
WS_PORT = 18766


def _varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1
    output = bytearray()
    while value > 0x7F:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def _field_varint(number: int, value: int) -> bytes:
    return _varint((number << 3) | 0) + _varint(value)


def _field_string(number: int, value: str) -> bytes:
    encoded = value.encode("utf-8")
    return _varint((number << 3) | 2) + _varint(len(encoded)) + encoded


def _field_message(number: int, payload: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(payload)) + payload


def _discover_lan_ipv4() -> str:
    # A UDP route lookup selects the IPv4 address used by the active default route.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route_probe:
        route_probe.connect(("192.0.2.1", 9))
        candidate = route_probe.getsockname()[0]
    address = ipaddress.ip_address(candidate)
    if not isinstance(address, ipaddress.IPv4Address) or not address.is_private or address.is_loopback or address.is_link_local:
        raise RuntimeError(f"No se detectó una IPv4 privada de LAN (resultado: {candidate}). Usa --lan-ip con la IP Wi-Fi.")
    return str(address)


def _validate_lan_ipv4(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise ValueError(f"IP de LAN inválida: {value}") from error
    if not isinstance(address, ipaddress.IPv4Address) or not address.is_private or address.is_loopback or address.is_link_local:
        raise ValueError("La IP anunciada debe ser una IPv4 privada de la LAN.")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as local_check:
        try:
            local_check.bind((str(address), 0))
        except OSError as error:
            raise ValueError(f"La IP {address} no está asignada actualmente a esta PC.") from error
    return str(address)


def _lan_area_response(address: str) -> Path:
    entry = f"http://{address}:{HTTP_PORT}/"
    area = _field_varint(1, 1) + _field_string(2, entry) + _field_varint(3, 1)
    message = _field_varint(1, 200) + _field_varint(3, 1) + _field_message(4, area)
    output = ROOT / "experiments" / f"area-list-lan-{address.replace('.', '_')}.base64"
    content = base64.b64encode(message) + b"\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    if not output.is_file() or output.read_bytes() != content:
        output.write_bytes(content)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lan", action="store_true", help="Listen on the LAN and advertise this PC's private IPv4 address.")
    parser.add_argument("--lan-ip", help="Override the detected LAN IPv4 address advertised to the client.")
    args = parser.parse_args()

    if args.lan:
        try:
            advertised_host = _validate_lan_ipv4(args.lan_ip) if args.lan_ip else _discover_lan_ipv4()
        except (OSError, RuntimeError, ValueError) as error:
            print(f"No se pudo preparar el backend para LAN: {error}")
            return 2
        bind_host = "0.0.0.0"
        area_response = _lan_area_response(advertised_host)
        http_url = f"http://{advertised_host}:{HTTP_PORT}"
        ws_url = f"ws://{advertised_host}:{WS_PORT}"
        allow_lan = ["--allow-lan"]
    else:
        if args.lan_ip:
            parser.error("--lan-ip requiere --lan.")
        advertised_host = "127.0.0.1"
        bind_host = "127.0.0.1"
        area_response = LOOPBACK_AREA_RESPONSE
        http_url = f"http://{advertised_host}:{HTTP_PORT}"
        ws_url = f"ws://{advertised_host}:{WS_PORT}"
        allow_lan = []

    if not area_response.is_file():
        print(f"Falta la respuesta local de servidores: {area_response}")
        return 2
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    http_log = LOG_ROOT / "http_requests.jsonl"
    ws_log = LOG_ROOT / "websocket_events.jsonl"

    commands = [
        [
            sys.executable, str(ROOT / "server.py"),
            "--host", bind_host, "--port", str(HTTP_PORT),
            *allow_lan,
            "--area-response", str(area_response),
            "--accounts-db", str(ACCOUNT_DB),
            "--ws-url", ws_url,
            "--log-file", str(http_log),
        ],
        [
            sys.executable, str(ROOT / "ws_probe_server.py"),
            "--host", bind_host, "--port", str(WS_PORT),
            *allow_lan,
            "--public-http-url", http_url,
            "--public-ws-url", ws_url,
            "--accounts-db", str(ACCOUNT_DB),
            "--log-file", str(ws_log),
        ],
    ]
    processes: list[subprocess.Popen] = []
    try:
        service_names = ("HTTP", "WebSocket")
        for service_name, command in zip(service_names, commands):
            processes.append(subprocess.Popen(command, cwd=PROJECT_ROOT))
            time.sleep(0.8)
            if processes[-1].poll() is not None:
                return_code = int(processes[-1].returncode or 1)
                print(
                    f"El servicio {service_name} no pudo iniciar (código {return_code}). "
                    "Revisa el mensaje de error anterior; puede señalar un archivo o "
                    "ajuste faltante, o un puerto ocupado."
                )
                return return_code

        ready = False
        for _ in range(30):
            try:
                with urlopen(f"http://127.0.0.1:{HTTP_PORT}/healthz", timeout=1) as response:
                    health = json.loads(response.read().decode("utf-8"))
                if health.get("status") == "ok" and health.get("accounts_enabled") is True:
                    ready = True
                    break
            except Exception:
                pass
            if any(process.poll() is not None for process in processes):
                break
            time.sleep(0.25)
        if not ready:
            print("El servicio HTTP no confirmó que las cuentas están activadas.")
            return 1

        print("Backend local listo para beta testing.")
        print(f"HTTP:      {http_url}")
        print(f"WebSocket: {ws_url}")
        print(f"Cuentas y perfiles: {ACCOUNT_DB} y {ACCOUNT_DB.parent / 'accounts'}")
        if args.lan:
            print("El teléfono debe estar en la misma red Wi-Fi; el firewall se limita a la subred local.")
        else:
            print("Los servicios aceptan conexiones solo desde esta computadora.")
        print("Pulsa Ctrl+C para cerrar los servicios.")
        while True:
            for process in processes:
                if process.poll() is not None:
                    print(f"Un servicio terminó con código {process.returncode}; cerrando el otro.")
                    return int(process.returncode or 1)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("Cerrando el backend local…")
        return 0
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            if process.poll() is None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()


if __name__ == "__main__":
    raise SystemExit(main())


