# smart-plugs

Small Python utility to control **two Shelly smart plugs over Bluetooth Low Energy (BLE)** using Shelly’s **JSON-RPC over GATT** protocol. It targets **Shelly Plug S Gen 3** (and similar Gen2/Gen3 devices that expose the same BLE RPC service).

## What it does

- Connects to each plug by **BLE address** from a YAML config.
- Sends **JSON-RPC** requests on the Shelly BLE characteristics (length on TX control, payload on the data characteristic, reads response length from RX control, then reads the response body in chunks).
- Provides an **interactive menu** to turn both plugs on/off together or **toggle** one plug at a time.
- Exposes simple **Python functions** so you can script the same actions from your own code.

On **macOS**, the script uses a small `BleakClient` subclass so Shelly RPC works without full GATT descriptor discovery (CoreBluetooth quirk).

## Requirements

- Python 3
- Bluetooth hardware on the machine you run the script from
- Plugs in BLE range, with BLE RPC available (as in Shelly’s BLE documentation for your model)

## Setup

```bash
cd smart-plugs
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Dependencies: **bleak** (BLE), **PyYAML** (config).

## Configuration (`config.yaml`)

You must define **exactly two devices**, with these **names** (the code validates them):

- `Radar TX`
- `Radar RX`

Each entry needs a **`name`** and BLE **`address`** (UUID on macOS, or the address format your OS uses). Discover addresses with a scan (see below).

The file also holds the **GATT UUIDs** for the Shelly BLE RPC service, plus tuning options such as timeouts and `rpc_processing_delay_seconds`. Defaults match typical Gen2/Gen3 Shelly BLE RPC.

## Usage

**Scan for nearby BLE devices** (uses `scan_timeout_seconds` from config):

```bash
python shelly_ble.py --scan
```

**Interactive control** (connects to both plugs, then shows a menu):

```bash
python shelly_ble.py
```

Use a different config path:

```bash
python shelly_ble.py --config /path/to/config.yaml
```

### Using it from Python

The module exposes synchronous helpers (they run `asyncio.run` internally; do not call them from inside an already running event loop):

```python
from shelly_ble import turn_all_on, turn_all_off, toggle_device

turn_all_on()
turn_all_off()
toggle_device("Radar TX")
```

For async code, use `async_turn_all_on`, `async_turn_all_off`, and `async_toggle_device` instead.

## Security / privacy

`config.yaml` contains **device identifiers** (BLE addresses) and names. If you publish a fork, consider replacing real addresses with placeholders or using a local, git-ignored config.
