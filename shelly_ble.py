#!/usr/bin/env python3
"""Minimal BLE RPC controller and script API for Shelly Plug S Gen 3."""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import struct
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml
from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic

if sys.platform == "darwin":
    from CoreBluetooth import CBCharacteristicWriteWithoutResponse
    from bleak.assigned_numbers import gatt_char_props_to_strs
    from bleak.backends.corebluetooth.client import BleakClientCoreBluetooth
    from bleak.backends.corebluetooth.utils import cb_uuid_to_str
    from bleak.backends.service import BleakGATTService, BleakGATTServiceCollection


    class ShellyBleakClientCoreBluetooth(BleakClientCoreBluetooth):
        """Skip descriptor discovery on macOS for Shelly BLE RPC."""

        async def _get_services(self) -> BleakGATTServiceCollection:
            if self.services is not None:
                return self.services

            services = BleakGATTServiceCollection()

            assert self._delegate is not None
            assert self._peripheral is not None
            cb_services = await self._delegate.discover_services(self._requested_services)

            for service in cb_services:
                bleak_service = BleakGATTService(
                    service,
                    service.startHandle(),
                    cb_uuid_to_str(service.UUID()),
                )
                services.add_service(bleak_service)

                characteristics = await self._delegate.discover_characteristics(service)
                for characteristic in characteristics:
                    bleak_characteristic = BleakGATTCharacteristic(
                        characteristic,
                        characteristic.handle(),
                        cb_uuid_to_str(characteristic.UUID()),
                        list(gatt_char_props_to_strs(characteristic.properties())),
                        functools.partial(
                            self._peripheral.maximumWriteValueLengthForType_,
                            CBCharacteristicWriteWithoutResponse,
                        ),
                        bleak_service,
                    )
                    services.add_characteristic(bleak_characteristic)

            self.services = services
            return self.services

REQUIRED_SHARED_CONFIG_KEYS = [
    "service_uuid",
    "data_uuid",
    "tx_ctl_uuid",
    "rx_ctl_uuid",
    "rpc_src",
]
EXPECTED_DEVICE_NAMES = ("Radar TX", "Radar RX")


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with path.open("r", encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file) or {}

    if not isinstance(config, dict):
        raise ValueError("Config must be a YAML object at the top level.")

    missing = [key for key in REQUIRED_SHARED_CONFIG_KEYS if key not in config]
    if missing:
        raise ValueError(f"Missing required shared config keys: {', '.join(missing)}")

    devices = config.get("devices")
    if not isinstance(devices, list):
        raise ValueError("Config key 'devices' must be a list of device objects.")

    if len(devices) != len(EXPECTED_DEVICE_NAMES):
        raise ValueError(
            f"Config must define exactly {len(EXPECTED_DEVICE_NAMES)} devices: "
            f"{', '.join(EXPECTED_DEVICE_NAMES)}."
        )

    devices_by_name: dict[str, dict[str, str]] = {}
    for index, device in enumerate(devices, start=1):
        if not isinstance(device, dict):
            raise ValueError(f"Device entry #{index} must be a YAML object.")

        name = device.get("name")
        address = device.get("address")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Device entry #{index} has an invalid 'name'.")
        if not isinstance(address, str) or not address.strip():
            raise ValueError(f"Device '{name}' has an invalid 'address'.")

        normalized_name = name.strip()
        if normalized_name in devices_by_name:
            raise ValueError(f"Duplicate device name found: '{normalized_name}'.")

        devices_by_name[normalized_name] = {
            "name": normalized_name,
            "address": address.strip(),
        }

    expected_names = set(EXPECTED_DEVICE_NAMES)
    configured_names = set(devices_by_name)
    missing_names = expected_names - configured_names
    unexpected_names = configured_names - expected_names
    if missing_names or unexpected_names:
        details: list[str] = []
        if missing_names:
            details.append(f"missing: {', '.join(sorted(missing_names))}")
        if unexpected_names:
            details.append(f"unexpected: {', '.join(sorted(unexpected_names))}")
        raise ValueError(
            "Config devices must contain exactly "
            f"{', '.join(EXPECTED_DEVICE_NAMES)} ({'; '.join(details)})."
        )

    config["devices"] = [devices_by_name[name] for name in EXPECTED_DEVICE_NAMES]

    return config


async def prompt_input(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


async def scan_devices(timeout_seconds: float) -> None:
    print(f"Scanning for BLE devices for {timeout_seconds} seconds...")
    devices = await BleakScanner.discover(timeout=timeout_seconds)
    if not devices:
        print("No BLE devices found.")
        return

    print("\nDiscovered devices:")
    print("-" * 80)
    for device in devices:
        name = device.name or "<unknown>"
        rssi = getattr(device, "rssi", "n/a")
        print(f"Address: {device.address} | RSSI: {rssi} | Name: {name}")
    print("-" * 80)


class ShellyBleRpcClient:
    def __init__(
        self, shared_config: dict[str, Any], device_name: str, device_address: str
    ) -> None:
        self.shared_config = shared_config
        self.device_name = device_name
        self.device_address = device_address
        self.request_id = 1
        self.client: BleakClient | None = None

    async def connect(self) -> None:
        print(f"[{self.device_name}] Connecting to Shelly device over BLE...")
        print(f"[{self.device_name}] Device address: {self.device_address}")
        print(f"[{self.device_name}] Service UUID:   {self.shared_config['service_uuid']}")
        print(f"[{self.device_name}] Data UUID:      {self.shared_config['data_uuid']}")
        print(f"[{self.device_name}] TX CTL UUID:    {self.shared_config['tx_ctl_uuid']}")
        print(f"[{self.device_name}] RX CTL UUID:    {self.shared_config['rx_ctl_uuid']}")

        timeout = float(self.shared_config.get("connect_timeout_seconds", 15))
        device = await BleakScanner.find_device_by_address(
            self.device_address,
            timeout=timeout,
        )
        if device is None:
            raise RuntimeError(
                f"[{self.device_name}] Could not find device {self.device_address} via BLE scan."
            )

        print(f"[{self.device_name}] Discovered device: {device.name or '<unknown>'}")

        self.client = BleakClient(
            device,
            timeout=timeout,
            backend=ShellyBleakClientCoreBluetooth if sys.platform == "darwin" else None,
        )
        await self.client.connect()
        print(f"[{self.device_name}] Connected: {self.client.is_connected}")
        print(f"[{self.device_name}] BLE connection established.")
        print(f"[{self.device_name}] Skipping service enumeration and using UUIDs from config.yaml.")

    async def disconnect(self) -> None:
        if self.client and self.client.is_connected:
            await self.client.disconnect()
            print(f"[{self.device_name}] Disconnected.")

    async def send_rpc(self, method: str, params: Any | None = None) -> dict[str, Any]:
        if self.client is None or not self.client.is_connected:
            raise RuntimeError("Not connected to a BLE device.")

        request: dict[str, Any] = {
            "id": self.request_id,
            "src": self.shared_config["rpc_src"],
            "method": method,
        }
        if params is not None:
            request["params"] = params

        self.request_id += 1

        payload = json.dumps(request, separators=(",", ":")).encode("utf-8")
        payload_length = struct.pack(">I", len(payload))
        rpc_delay = float(self.shared_config.get("rpc_processing_delay_seconds", 1.0))

        print(f"\n[{self.device_name}] Sending RPC request:")
        print(json.dumps(request, indent=2))

        await self.client.write_gatt_char(
            self.shared_config["tx_ctl_uuid"], payload_length, response=True
        )
        await asyncio.sleep(rpc_delay)
        await self.client.write_gatt_char(
            self.shared_config["data_uuid"], payload, response=True
        )
        await asyncio.sleep(rpc_delay)

        response_length_raw = await self.client.read_gatt_char(
            self.shared_config["rx_ctl_uuid"]
        )
        if len(response_length_raw) < 4:
            raise RuntimeError("Invalid response length received from device.")

        response_length = struct.unpack(">I", bytes(response_length_raw[:4]))[0]
        print(f"[{self.device_name}] Response length: {response_length} bytes")

        response_bytes = bytearray()
        while len(response_bytes) < response_length:
            chunk = await self.client.read_gatt_char(self.shared_config["data_uuid"])
            if not chunk:
                break
            response_bytes.extend(chunk)

            if len(response_bytes) < response_length:
                await asyncio.sleep(0.05)

        decoded = bytes(response_bytes[:response_length]).decode("utf-8", errors="replace")
        try:
            response_json = json.loads(decoded)
        except json.JSONDecodeError:
            response_json = {"raw_response": decoded}

        print(f"[{self.device_name}] RPC response:")
        print(json.dumps(response_json, indent=2))
        return response_json


def build_clients(config: dict[str, Any]) -> list[ShellyBleRpcClient]:
    return [
        ShellyBleRpcClient(
            shared_config=config,
            device_name=device["name"],
            device_address=device["address"],
        )
        for device in config["devices"]
    ]


def print_menu(clients: list[ShellyBleRpcClient]) -> str:
    print("\nSelect a command:")
    print("  1) Turn ALL configured plugs ON")
    print("  2) Turn ALL configured plugs OFF")

    option_number = 3
    for client in clients:
        print(f"  {option_number}) Toggle {client.device_name}")
        option_number += 1

    print(f"  {option_number}) Quit\n")
    return str(option_number)


def format_failures(
    action: str, failures: list[tuple[ShellyBleRpcClient, Exception]]
) -> str:
    details = "\n".join(
        f"  - {client.device_name} ({client.device_address}): {error}"
        for client, error in failures
    )
    return f"Failed to {action} one or more devices:\n{details}"


async def connect_clients(clients: list[ShellyBleRpcClient]) -> None:
    results = await asyncio.gather(
        *(client.connect() for client in clients),
        return_exceptions=True,
    )
    failures: list[tuple[ShellyBleRpcClient, Exception]] = []
    for client, result in zip(clients, results):
        if isinstance(result, Exception):
            failures.append((client, result))

    if failures:
        raise RuntimeError(format_failures("connect to", failures))


async def disconnect_clients(clients: list[ShellyBleRpcClient]) -> None:
    results = await asyncio.gather(
        *(client.disconnect() for client in clients),
        return_exceptions=True,
    )
    for client, result in zip(clients, results):
        if isinstance(result, Exception):
            print(
                f"[{client.device_name}] Disconnect error: {result}"
            )


async def set_power_for_all(clients: list[ShellyBleRpcClient], on: bool) -> None:
    command_label = "ON" if on else "OFF"
    print(f"\nRunning combined command: {command_label} on all configured plugs...")
    results = await asyncio.gather(
        *(
            client.send_rpc("Switch.Set", {"id": 0, "on": on})
            for client in clients
        ),
        return_exceptions=True,
    )

    failures: list[tuple[ShellyBleRpcClient, Exception]] = []
    for client, result in zip(clients, results):
        if isinstance(result, Exception):
            failures.append((client, result))

    if failures:
        print(f"Combined {command_label} command had failures:")
        for client, error in failures:
            print(f"  - {client.device_name}: {error}")
        raise RuntimeError(
            f"Combined {command_label} failed for: "
            f"{', '.join(client.device_name for client, _ in failures)}"
        )

    print(f"Combined {command_label} command succeeded on all configured plugs.")


async def toggle_power_for_client(client: ShellyBleRpcClient) -> None:
    print(f"\nRunning toggle command on {client.device_name}...")
    await client.send_rpc("Switch.Toggle", {"id": 0})
    print(f"Toggle command sent to {client.device_name}.")


def get_client_by_name(
    clients: list[ShellyBleRpcClient], device_name: str
) -> ShellyBleRpcClient:
    normalized_name = device_name.strip()
    for client in clients:
        if client.device_name == normalized_name:
            return client

    available_names = ", ".join(client.device_name for client in clients)
    raise ValueError(
        f"Unknown device name '{device_name}'. Available devices: {available_names}"
    )


async def with_connected_clients(
    config_path: str | Path,
    operation: Callable[[list[ShellyBleRpcClient]], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    config = load_config(Path(config_path))
    clients = build_clients(config)
    try:
        await connect_clients(clients)
        return await operation(clients)
    finally:
        await disconnect_clients(clients)


async def async_turn_all_on(config_path: str | Path = "config.yaml") -> dict[str, Any]:
    async def operation(clients: list[ShellyBleRpcClient]) -> dict[str, Any]:
        await set_power_for_all(clients, on=True)
        return {
            "ok": True,
            "action": "turn_all_on",
            "devices": [client.device_name for client in clients],
        }

    return await with_connected_clients(config_path, operation)


async def async_turn_all_off(config_path: str | Path = "config.yaml") -> dict[str, Any]:
    async def operation(clients: list[ShellyBleRpcClient]) -> dict[str, Any]:
        await set_power_for_all(clients, on=False)
        return {
            "ok": True,
            "action": "turn_all_off",
            "devices": [client.device_name for client in clients],
        }

    return await with_connected_clients(config_path, operation)


async def async_toggle_device(
    device_name: str, config_path: str | Path = "config.yaml"
) -> dict[str, Any]:
    normalized_name = device_name.strip()
    if not normalized_name:
        raise ValueError("Device name must be a non-empty string.")

    config = load_config(Path(config_path))
    clients = build_clients(config)
    selected_client = get_client_by_name(clients, normalized_name)

    try:
        await connect_clients(clients)
        await toggle_power_for_client(selected_client)
        return {
            "ok": True,
            "action": "toggle_device",
            "device": selected_client.device_name,
        }
    finally:
        await disconnect_clients(clients)


def _run_sync_api_call(coro: Awaitable[dict[str, Any]]) -> dict[str, Any]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError(
        "A running event loop is active. Use async_turn_all_on, async_turn_all_off, "
        "or async_toggle_device instead of the sync wrappers."
    )


def turn_all_on(config_path: str | Path = "config.yaml") -> dict[str, Any]:
    return _run_sync_api_call(async_turn_all_on(config_path=config_path))


def turn_all_off(config_path: str | Path = "config.yaml") -> dict[str, Any]:
    return _run_sync_api_call(async_turn_all_off(config_path=config_path))


def toggle_device(
    device_name: str, config_path: str | Path = "config.yaml"
) -> dict[str, Any]:
    return _run_sync_api_call(
        async_toggle_device(device_name=device_name, config_path=config_path)
    )


async def run_menu(clients: list[ShellyBleRpcClient]) -> None:
    while True:
        quit_option = print_menu(clients)
        selection = (await prompt_input("Enter choice: ")).strip()

        if selection == quit_option:
            print("Exiting.")
            return

        if selection == "1":
            try:
                await set_power_for_all(clients, on=True)
            except Exception as error:  # noqa: BLE001
                print(f"Error: {error}")
        elif selection == "2":
            try:
                await set_power_for_all(clients, on=False)
            except Exception as error:  # noqa: BLE001
                print(f"Error: {error}")
        else:
            try:
                selected_client_index = int(selection) - 3
            except ValueError:
                selected_client_index = -1

            if 0 <= selected_client_index < len(clients):
                selected_client = clients[selected_client_index]
                try:
                    await toggle_power_for_client(selected_client)
                except Exception as error:  # noqa: BLE001
                    print(f"Error: {error}")
            else:
                print("Invalid choice. Please enter one of the menu numbers.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Control Radar TX and Radar RX Shelly plugs over BLE via RPC."
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config YAML file (default: config.yaml).",
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help="Scan nearby BLE devices and exit.",
    )
    return parser.parse_args()


async def async_main() -> int:
    args = parse_args()
    config = load_config(Path(args.config))

    if args.scan:
        await scan_devices(float(config.get("scan_timeout_seconds", 5)))
        return 0

    clients = build_clients(config)
    try:
        await connect_clients(clients)
        await run_menu(clients)
    except Exception as error:  # noqa: BLE001
        print(f"Error: {error}")
        return 1
    finally:
        await disconnect_clients(clients)

    return 0


def main() -> None:
    try:
        exit_code = asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        exit_code = 130
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
