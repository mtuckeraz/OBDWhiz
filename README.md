# OBDWhiz — Raspberry Pi diagnostic workbench

A local diagnostic application primarily packaged for **Raspberry Pi 4 (8 GB)**, with a direct **Carista EVO → Pi Bluetooth → OBDWhiz** workflow. The Python server, SQLite history and Bluetooth connection run on the Pi. Open the web interface from a phone or laptop on the same local network. Receiver setup, Bluetooth power, pairing PINs and confirmations are handled in the browser; users do not need the Pi desktop or SSH. AI research uses an external API; no local AI model is required.

The installed Pi service uses **HTTP on port 8787** in authenticated LAN mode. Open `http://YOUR_PI_HOSTNAME.local:8787` (or the Pi's IP address) and sign in with the access key printed during installation. No browser Bluetooth support or phone Bluetooth pairing is needed. Windows remains a secondary supported launch path.

## Install on Raspberry Pi

Use **Raspberry Pi OS 64-bit, Bookworm or newer**, Python **3.11+**, BlueZ **5.55+**, working Bluetooth, and internet access for installation. Both Pi OS Lite and Desktop work; the application runs without a desktop session. Keep the Pi within Bluetooth range of the vehicle.

### One-command install from GitHub

Run this in a Pi terminal as your normal user (not root):

```bash
sudo apt-get update && sudo apt-get install -y git && git clone https://github.com/mtuckeraz/OBDWhiz.git "$HOME/OBDWhiz" && bash "$HOME/OBDWhiz/install-pi.sh" --service
```

This installs Git, clones the public repository into `~/OBDWhiz`, installs all Python and Bluetooth dependencies, and starts OBDWhiz now and at boot. The destination must not already exist. The installer prints the LAN web address and a private browser access key. Share those with your users. The boot service immediately receives Bluetooth permissions, so users can complete receiver setup through the web interface without logging into the Pi. Only the installing owner needs the terminal. Log out/in only if you will run optional manual Bluetooth checks from that terminal.

To update an existing GitHub installation, preserve `.local` and `.env` and run:

```bash
cd "$HOME/OBDWhiz" && sudo systemctl stop obdwhiz && git pull --ff-only && bash install-pi.sh --service
```

If Git reports local source changes, resolve them before updating; the command does not overwrite them.

### Install from an archive

Copy `obdwhiz-pi-0.3.0.tar.gz` and its `.sha256` file from this project's `output` directory to your Pi. In a Pi terminal, as your normal user:

```bash
sha256sum -c obdwhiz-pi-0.3.0.tar.gz.sha256
tar -xzf obdwhiz-pi-0.3.0.tar.gz
cd obdwhiz
bash install-pi.sh
```

The source checkout can also be installed directly with `bash install-pi.sh`. Keep the directory in its final location if enabling the boot service. **Do not run the whole installer with sudo**: it requests sudo only for system setup and keeps the app and virtual environment owned by your user.

The installer explicitly installs:

| Layer | Dependencies / setup |
| --- | --- |
| Pi OS packages | `python3`, `python3-venv`, `python3-pip`, `bluez`, `bluetooth`, `dbus`, `rfkill`, `ca-certificates`, `avahi-daemon` |
| Python virtual environment | `bleak==3.0.2`, `pyserial==3.5` and their transitive dependencies, including Bleak's Linux D-Bus backend |
| Bluetooth startup | Enables and starts `bluetooth.service`, unblocks Bluetooth and requests controller power-on |
| Device permissions | Adds the installing user to `bluetooth` and `dialout` without replacing existing groups |

Python packages are installed in `.venv`, as required by [modern Raspberry Pi OS](https://www.raspberrypi.com/documentation/computers/os.html). Bleak uses [BlueZ over D-Bus on Linux](https://bleak.readthedocs.io/en/stable/backends/linux.html). There is no need to install a separate Python OBD library or use browser Web Bluetooth.

For the manual launcher only, **log out and back in after installing** to apply device group membership. The boot service applies its groups immediately and does not require a desktop login. To launch manually:

```bash
bash Start-OBDWhiz.sh --lan
```

Keep the terminal open; stop with Ctrl+C. Read the installation key with `.venv/bin/python tools/configure_access.py`. For localhost-only development, omit `--lan` and optionally pass `--open`. Use `--port 8788` if needed. A Windows `.venv` cannot be reused on Linux: transfer the clean release archive, not the Windows environment.

### Optional automatic startup

To install dependencies and enable OBDWhiz at boot:

```bash
bash install-pi.sh --service
```

The installer validates the generated service using `systemd-analyze verify` before installing it. The systemd service runs as the installing user, with Bluetooth and serial groups, and listens on the local network on port 8787 with access-key authentication. It starts the server without opening a browser or auto-connecting to a vehicle. Open the web address from a phone/laptop, sign in and select the receiver. Avoid starting a second copy while the service is running.

```bash
sudo systemctl status obdwhiz --no-pager
journalctl -u obdwhiz -n 50 --no-pager
sudo systemctl restart obdwhiz
sudo systemctl stop obdwhiz
```

Disable automatic startup with `sudo systemctl disable --now obdwhiz`. Re-run `bash install-pi.sh --service` after moving the app directory. The service uses SIGINT for normal application shutdown and a private file-creation mask for new scan/key files.

### Receiver setup in a phone/laptop browser

1. Connect the phone/laptop to the Pi's local network and open `http://YOUR_PI_HOSTNAME.local:8787`. The installer enables Avahi for `.local` discovery; the Pi's LAN IP also works.
2. Sign in with the access key the owner received from the installer. The key is saved privately in `.local/access.key` and survives reinstallation. Sessions expire after 12 hours or server restart; **Sign out** revokes the current session.
3. Open **Adapter & vehicle**. Select the Pi Bluetooth radio and turn it on if needed. The installer already starts Bluetooth and grants the service device permissions.
4. Click **Discover receivers**. Match the receiver name, Bluetooth address, signal strength and advertised services to your own adapter. A name alone is not proof that a device is an OBD receiver.
5. Click **Pair** if your receiver requires bonding. Enter the receiver's PIN/passkey or confirm its displayed code in the web interface. Pairing never depends on a Pi desktop agent. Cancel a pending request on the same page if needed. Many BLE receivers can use **Connect** directly without pairing.
6. Click **Connect**, review the UART endpoints and **Initialize diagnostics**. ELM identity is checked before vehicle reads. The last connected receiver is remembered locally; paired receivers remain paired in BlueZ across restarts.

The supported wireless diagnostic path is **Bluetooth LE with an ELM-compatible UART**, including the Carista EVO profile. Bluetooth Classic/SPP-only receivers are not provided a new wireless transport by this release; use supported BLE hardware or an enumerated serial connection. The browser does not connect to the receiver itself.

The installed service uses `--lan` by default. Run `server.py` without `--lan` for localhost-only development. If you prefer SSH forwarding instead of LAN access, run the server in localhost-only mode and forward port 8787 with `ssh -N -L 8787:127.0.0.1:8787 YOUR_USER@PI_HOSTNAME`.

### Optional owner Bluetooth diagnostics

These terminal checks are for the owner when diagnosing hardware problems; normal users set up receivers entirely in the web interface.

```bash
.venv/bin/python tools/check_bluetooth.py --scan
```

This checks Python dependencies, BlueZ version, the Bluetooth service, controller power and a six-second BLE discovery as the current user. It reports counts without printing device addresses. Successful discovery does not establish a working Carista diagnostic session; use **Discover receivers → Connect → Initialize diagnostics** with the adapter powered and vehicle ignition on.

If setup needs attention:

- Missing libraries or tools: re-run `bash install-pi.sh`.
- Permission denied: log out/in after installation; check `id -nG` includes `bluetooth` and `dialout`. The boot service applies these groups immediately.
- Bluetooth off: run `sudo rfkill unblock bluetooth`, `sudo systemctl enable --now bluetooth`, then `bluetoothctl power on`.
- No controller: check `bluetoothctl list` and Pi boot settings; ensure Bluetooth has not been disabled.
- No EVO: check ignition, range, adapter power, and that another app is not connected. Use the web interface for pairing when required; BLE does not require creating a serial port or opening Pi Bluetooth Settings.
- Persistent Pi Bluetooth/Wi-Fi interference: try Ethernet or 5 GHz Wi-Fi; see [Bleak's Raspberry Pi troubleshooting](https://bleak.readthedocs.io/en/stable/troubleshooting.html).

## Windows (secondary)

Requires Windows 10/11, Python 3.11+ and working Bluetooth. Double-click **Start-Apex.cmd**, or run `Start-Apex.ps1`. The legacy launcher name remains for compatibility; it installs the Python libraries in a Windows `.venv`, starts OBDWhiz and opens a browser. Linux system packages and the Pi installer are not used on Windows.

## Build a distributable Pi archive

From this source directory, run `python tools/build_release.py` (or `.venv/bin/python tools/build_release.py` on Linux). This creates `output/obdwhiz-pi-0.3.0.tar.gz` plus a SHA-256 checksum. Shell scripts are packaged with Linux line endings and executable permissions.

The archive uses an explicit source-file allowlist. It includes the application, installer, launchers, Bluetooth checker, tests and documentation. It excludes `.env`, `.local`, scan history/VINs, `.venv`, caches, screenshots and previous output. Installation downloads dependencies; this is not an offline image. Rebuilding replaces the same versioned archive.

For upgrades, stop OBDWhiz and back up `.local` and your private `.env` before replacing source files in the existing app directory. Re-run the installer and restart the service. Preserve those local files; do not copy a Windows `.venv`. To move existing scan history from Windows, stop the Windows server before copying its complete `.local` directory into the Pi app directory; history is not included in the release archive.

## Connect and scan

1. Plug in the Carista EVO, turn the ignition on and keep the Pi within Bluetooth range. Close Carista or other apps that are using the adapter.
2. Open **Adapter & vehicle → Discover receivers** and select your adapter. Discovery includes Bluetooth LE advertisements and available operating-system serial ports.
3. For BLE, OBDWhiz automatically selects the **Carista EVO UART pair verified on your adapter**: service `000018f0-0000-1000-8000-00805f9b34fb`, TX `00002af1-0000-1000-8000-00805f9b34fb`, RX `00002af0-0000-1000-8000-00805f9b34fb`. Handles are discovered dynamically. Other adapters use a suggested writable/notifying pair that you must confirm. Initialization validates an ELM-compatible identity before vehicle reads.
4. If the EVO exposes a Bluetooth serial profile, configure the serial connection in your operating system first, then select its port (for example /dev/ttyUSB0 on Linux or an outgoing COM port on Windows) and baud rate. BLE is the primary path; serial availability depends on the hardware. Do not select unrelated devices or COM ports.
5. Click **Initialize diagnostics**. OBDWhiz sets ELM auto protocol selection, enables headers, checks for responding OBD ECUs and reads supported PID bitmaps.
6. Enter exact make, model, year, powertrain, platform/software and symptoms. Click **Read diagnostic codes**.
7. Select supported live readings and a cycle interval. Scan first so samples have a saved session. The requested polling period is a minimum cycle period, not a guarantee of bus throughput.

All adapter operations are serialized. A timeout or transport error closes the connection to prevent a late reply from being treated as the next command's result. Reconnect after these errors. Raw ELM request/response evidence is retained with each scan. Unsupported/negative responses produce coverage warnings instead of an all-clear claim.

## Implemented capability and limits

| Capability | Status |
| --- | --- |
| Native Pi/Linux and Windows BLE discovery and GATT inspection | Implemented with Bleak; Linux uses BlueZ / D-Bus; physical Pi verification pending |
| Serial adapter connection | Implemented with pySerial; requires a serial profile exposed by the adapter |
| ELM-compatible initialization and OBD auto protocol selection | Verified on your physical EVO: ELM327 v2.2, automatic protocol A6 |
| Standard stored / pending / permanent emissions DTCs | Implemented: modes 03 / 07 / 0A, where supported |
| Live PID discovery and selected live values | Implemented: mode 01 bitmaps and a curated decoder catalog |
| VIN / monitor status / frame-zero evidence | Implemented where supplied; VIN reassembly currently targets CAN; freeze frames and readiness bytes are preserved raw |
| Vehicles sorted by make/model, VIN grouping and scan history | Implemented; each VIN keeps its own scan sessions |
| Unknown OEM code preservation, JSON imports/exports | Implemented |
| OEM definition library | Exact make/model/year/ECU/code matching; source provenance required; supplied content remains unverified |
| Internet-backed AI causes, checks and resolution research | Implemented integration; needs your API key and provider connectivity |
| Complete OEM module scan across all makes | **Not implemented**. Standard OBD is not a manufacturer-wide module scan. |
| Protected gateway access, DoIP, OEM credentials or licensed service databases | **Not provided** |
| Tire-size coding, ECU coding, adaptation writes or actuator tests | **Not implemented**. Tire calculator and compatibility planning only. |
| Physical adapter / vehicle verification | EVO connected, initialized, completed two real standard OBD scans, and saved nine live polling cycles. This does not establish all vehicle/module coverage. |

Current-generation Mercedes-Benz, BMW, Rolls-Royce, Ferrari, Lamborghini and McLaren are supported as explicit **research contexts and imported code records**, not as verified full diagnostic/coding packages. Universal access and unpublished code resolution cannot be guaranteed. OBDWhiz does not recover proprietary definitions from adapter firmware, bypass gateway authentication or fabricate manufacturer command sequences.

The adapter command boundary allows a fixed set of ELM configuration commands and diagnostic reads only. Code clearing (mode 04), ECU writes, security access, resets and arbitrary raw commands are not exposed. AI research has no route to issue adapter commands.

Historical Windows testing verified real scans, VIN grouping and live polling on one EVO/vehicle combination. Those private scan records remain in the original installation; they are not bundled in the release. This does not establish Pi Bluetooth or other vehicle/module coverage. PID 5B's standard legacy name is "hybrid battery remaining life"; do not treat it as a verified battery state-of-health assessment.

## Saved vehicles and VINs

The Overview lists vehicles alphabetically by **make, then model**. A VIN identifies one vehicle, and the **Scan session** selector opens its previous scans with newer sessions first. Cars with the same make/model but different VINs remain separate. Scans without a VIN are not automatically merged. The saved vehicle count excludes demo records.

Mode 09 PID 02 captures the VIN automatically on supported CAN vehicles. The full VIN is stored in SQLite and JSON exports, and displayed on vehicle cards and scan details. Imported VINs are normalized to uppercase and validated as 17 letters/digits excluding I, O and Q. VINs remain local and are omitted from AI requests.

Selecting a vehicle fills the make, model, year, trim, drivetrain, VIN and powertrain fields. **Edit vehicle → Save vehicle details** updates the identity on all scans for the same VIN while preserving their dates, codes, raw evidence, symptoms and polling samples. Completed research is archived and marked for rerun when its vehicle context changes. If an automatic VIN read fails, a manually entered VIN can be used and is labeled as manual. If a selected VIN differs from the connected car's VIN, the scan is not saved under that identity.

## AI setup

Open **Setup**, enter an OpenAI API key, choose a model that supports Responses API web search (default `gpt-5-mini`), and apply. Keys entered through the UI live in server memory until restart. Alternatively copy `.env.example` to `.env`, set `OPENAI_API_KEY`, and restart. `.env` is ignored by Git and contains plaintext secrets if you use it; keep it private.

Automatic research is enabled by default once a key is supplied. Every real scan/import with codes starts one billable research job when none is active; otherwise its report records that research could not start. You can turn automatic research off in Setup. Manual **Research codes** reruns incur another provider request. Demo scans never call the provider. AI is not locally installed or included in a ChatGPT subscription.

Each research request includes the first 30 codes, up to 30 snapshot readings, vehicle context, symptoms, scan warnings and exact matching imported references. VIN, adapter addresses, complete history and raw frames are omitted. Provider response storage is disabled with `store: false`; provider retention policies still apply. Scan information in free-text symptoms/references is supplied as written, so avoid inserting unrelated sensitive data there.

The Responses API performs web search and returns source citations. Reports separate verified definitions, possible causes and missing OEM evidence. They are diagnostic suggestions, not a verified repair determination. No report can change the vehicle. No provider call was made during local validation because no key was supplied.

The **website** stays on HTTP. The server's outbound call to OpenAI uses HTTPS as required by that provider.

## OEM imports

OBDWhiz imports JSON in its own schema; arbitrary Carista/Xentry/ISTA/vendor PDFs and exports need conversion first. Preserve module names, raw codes, fault status and source coverage. In **OEM reference library**, use **Show scan JSON format** for the schema.

Minimum scan:

```json
{
  "vehicle": {"make": "McLaren", "model": "Exact model", "year": 2026},
  "codes": [{"code": "RAW_OEM_CODE", "ecu": "Exact module", "status": "stored", "definition": null}],
  "scope": "State which modules the source tool covered",
  "warnings": [],
  "trace": []
}
```

Definitions require provenance and match exact model/ECU context:

```json
{
  "definitions": [{
    "make": "Exact make", "model": "Exact model", "yearFrom": 2025, "yearTo": 2026,
    "ecu": "Exact module or response ID", "code": "RAW_OEM_CODE",
    "description": "Definition from a documented source",
    "sourceUrl": "https://manufacturer.example/reference",
    "reference": "Document number, revision and page"
  }]
}
```

The example URL above is a placeholder, not an OEM database. No licensed definitions are bundled. Library matching does not verify a code against ECU firmware versions; keep software applicability in the reference description and verify it before use.

## Storage and security

SQLite data lives in `.local/apex.sqlite3`. This legacy filename, the internal `X-Apex-Client` header and browser storage keys are retained to preserve existing installations; the application is named OBDWhiz. Up to 10,000 polling cycles are retained per scan; UI/JSON exports return the most recent 200. Scan snapshots and their request/response traces remain saved. The runtime adapter trace is bounded to 2,000 entries. Export JSON includes VIN/raw evidence, so review it before sharing.

The installed service listens on the LAN and requires a shared access key for all private API routes, including Bluetooth operations, even from localhost. Static sign-in assets are public. Sessions use HttpOnly/SameSite cookies, failed sign-ins are rate limited, Host names are limited to the Pi hostname or the receiving interface address, cross-origin requests are blocked, and mutations require a custom header. It does not expose keys through its API. HTML is escaped and report/source links are restricted to HTTP(S). No remote scripts, fonts or analytics are loaded. This is a shared local workbench, not separate per-user accounts: signed-in users share receiver control and scan history. Use it on a trusted local network; HTTP does not encrypt traffic. Do not expose it through public router port forwarding. The access key and Bluetooth receiver preferences live under the ignored `.local` directory; pairing PINs are not saved by OBDWhiz.

## Validation

```bash
.venv/bin/python -m unittest discover -s tests -v
# Optional development check when Node.js is installed (not required to run OBDWhiz):
node --check web/app.js
```

On Windows, use `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`.

The automated suite covers the application, LAN authentication and access controls, browser pairing API routes, PIN/passkey validation, receiver persistence, Bluetooth setup checks, service generation and private-data exclusion from the archive. Linux CI also validates a generated unit with systemd's actual parser and verifies that it rejects the earlier quoted `WorkingDirectory` defect. This parser check is skipped on Windows. Both Pi shell scripts are checked with Bash, and the browser JavaScript is checked with `node --check`. Prior physical adapter verification was performed on Windows; Pi OS installation, systemd service execution and Pi-to-Carista communication still require validation on the actual Pi.

Tests also cover multi-ECU CAN frames, CAN DTC counts, ISO-TP VIN assembly, incomplete-frame rejection, 29-bit headers, legacy headers, signed readings, bitmap discovery, storage/schema validation, definition applicability, API isolation, read-only command boundaries, mocked AI source/citation handling, scan/poll lifecycles, vehicle sorting, VIN validation/grouping, mismatched VIN rejection and evidence-preserving context corrections. Earlier browser checks covered demo review, tire calculation, adapter discovery/connection, vehicle/session selection and responsive layout.

## Technical references

- [Carista: device and app compatibility](https://help.carista.com/hc/en-us/articles/360009856479-Which-devices-and-apps-work-with-Carista) — ELM app compatibility is not an EVO GATT specification.
- [ELM Electronics: ELM327L datasheet](https://www.elmelectronics.com/wp-content/uploads/2016/07/ELM327L_Data_Sheet.pdf) — adapter commands and OBD framing.
- [BlueZ: pairing agent API](https://bluez.readthedocs.io/en/latest/agent-api/) — application-owned pairing prompts.
- [BlueZ: adapter API](https://bluez.readthedocs.io/en/latest/adapter-api/) — radio power and BLE discovery.
- [Bleak: Linux backend](https://bleak.readthedocs.io/en/stable/backends/linux.html) — Pi/Linux BlueZ and D-Bus support.
- [Bleak: Windows backend](https://bleak.readthedocs.io/en/stable/backends/windows.html) — secondary Windows BLE support.
- [pySerial: port enumeration](https://pyserial.readthedocs.io/en/latest/tools.html) — COM port discovery.
- [python-OBD command definitions](https://github.com/brendan-w/python-OBD/blob/master/obd/commands.py) — corroborating standard counter and PID 5B decoder labels.
- [OpenAI: web search](https://developers.openai.com/api/docs/guides/tools-web-search) — Responses API search and citation handling.
