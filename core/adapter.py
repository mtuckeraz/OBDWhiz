"""Native Pi/Linux and Windows BLE/serial bridge; no browser Bluetooth required."""
import asyncio
import datetime as dt
import re
import sys
import time
from . import obd
from .store import validate_scan


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


class Adapter:
    def __init__(self, save_scan, save_samples):
        self.save_scan, self.save_samples = save_scan, save_samples
        self.operation = asyncio.Lock()
        self.client = self.serial = self.tx = self.rx = None
        self.device_cache = {}
        self.waiter = None
        self.buffer = ''
        self.trace = []
        self.protocol = '6'
        self.state = {'connected': False, 'ready': False, 'device': None, 'transport': None, 'profile': None,
                      'error': None, 'polling': False, 'selectedPids': [], 'interval': 1000,
                      'supported': {}, 'characteristics': [], 'latest': [], 'scanId': None}
        self.poll_task = None

    async def devices(self):
        from bleak import BleakScanner
        from serial.tools import list_ports
        ports = [{'id': p.device, 'name': p.description, 'transport': 'serial'} for p in list_ports.comports()]
        try:
            discovered = await BleakScanner.discover(timeout=6, return_adv=True)
            self.device_cache = {address: d for address, (d, a) in discovered.items()}
            ble = [{'id': d.address, 'name': a.local_name or d.name or 'Unnamed Bluetooth device',
                    'transport': 'ble', 'rssi': a.rssi} for d, a in discovered.values()]
            ble.sort(key=lambda d: ('carista' not in d['name'].lower(), -(d['rssi'] or -100)))
            return {'devices': ble + ports, 'warning': None}
        except Exception as exc:
            hint = ('Run bash install-pi.sh, log out/in, then run .venv/bin/python tools/check_bluetooth.py --scan.'
                    if sys.platform == 'linux' else 'Enable Bluetooth in system settings and check adapter permissions.')
            return {'devices': ports, 'warning': f'Bluetooth LE discovery failed: {exc}. {hint} Available serial ports are still listed.'}

    def disconnected(self, _client):
        self.state.update(connected=False, ready=False, polling=False, error='Adapter disconnected. Reconnect before scanning.')
        if self.waiter and not self.waiter.done():
            self.waiter.set_exception(ConnectionError('Bluetooth disconnected'))

    def received(self, _sender, data):
        self.buffer += bytes(data).decode('ascii', errors='replace')
        if len(self.buffer) > 65536:
            self.buffer = self.buffer[-65536:]
        if '>' in self.buffer and self.waiter and not self.waiter.done():
            self.waiter.set_result(self.buffer.split('>', 1)[0])

    async def connect(self, spec):
        await self.disconnect()
        async with self.operation:
            transport = spec.get('transport')
            if transport == 'serial':
                import serial
                from serial.tools import list_ports
                if spec.get('id') not in [p.device for p in list_ports.comports()]:
                    raise ValueError('Select an enumerated serial port.')
                baud = int(spec.get('baud', 38400))
                if baud not in (9600, 19200, 38400, 57600, 115200):
                    raise ValueError('Unsupported baud rate.')
                self.serial = await asyncio.to_thread(serial.Serial, spec['id'], baud, timeout=0.1, write_timeout=3)
                self.state.update(connected=True, transport='serial', device=spec.get('name', spec['id']), error=None, characteristics=[])
            elif transport == 'ble':
                from bleak import BleakClient
                device = self.device_cache.get(spec.get('id'))
                if not device:
                    raise ValueError('Discover devices and select one from the list first.')
                self.client = BleakClient(device, disconnected_callback=self.disconnected, timeout=20)
                try:
                    await self.client.connect()
                    chars = [{'service': s.uuid, 'uuid': c.uuid, 'handle': c.handle, 'properties': c.properties}
                             for s in self.client.services for c in s.characteristics]
                    self.state.update(connected=True, transport='ble', device=spec.get('name') or device.name,
                                      error=None, characteristics=chars)
                    evo_service = '000018f0-0000-1000-8000-00805f9b34fb'
                    if any(c['service'] == evo_service and c['uuid'].startswith('00002af1-') for c in chars) and any(c['service'] == evo_service and c['uuid'].startswith('00002af0-') for c in chars):
                        self.state['profile'] = 'Carista EVO UART (validated on this unit)'
                except Exception:
                    await self._close()
                    raise
            else:
                raise ValueError('Choose Bluetooth LE or a paired serial port.')
        return dict(self.state)

    async def initialize(self, spec):
        await self.stop_poll()
        async with self.operation:
            if not self.state['connected']:
                raise ValueError('Connect an adapter first.')
            self.state['ready'] = False
            if self.client:
                chars = self.state['characteristics']
                tx = next((c for c in chars if c['handle'] == spec.get('tx')), None)
                rx = next((c for c in chars if c['handle'] == spec.get('rx')), None)
                if not tx and not rx and self.state.get('profile'):
                    tx = next((c for c in chars if c['uuid'].startswith('00002af1-')), None)
                    rx = next((c for c in chars if c['uuid'].startswith('00002af0-')), None)
                if not tx or not rx or not set(tx['properties']) & {'write', 'write-without-response'} or not set(rx['properties']) & {'notify', 'indicate'}:
                    raise ValueError('Choose an actual writable TX and notifying RX characteristic from this adapter.')
                if self.rx is not None:
                    await self.client.stop_notify(self.rx)
                self.tx, self.rx = self.client.services.get_characteristic(tx['handle']), self.client.services.get_characteristic(rx['handle'])
                await self.client.start_notify(self.rx, self.received)
            self.trace = []
            try:
                identity = await self.command('ATZ', timeout=8)
                if not re.search(r'ELM|CARISTA|OBD|STN', identity, re.I):
                    raise ValueError('The selected Bluetooth endpoint did not identify as an ELM-compatible adapter. Try another TX/RX pair.')
                for cmd in ('ATE0', 'ATL0', 'ATS1', 'ATH1', 'ATSP0', 'ATCAF1', 'ATAT1', 'ATST96'):
                    response = await self.command(cmd)
                    if 'OK' not in response.upper():
                        raise ValueError(f'Adapter rejected {cmd}: {response.strip()}')
                # First vehicle request allows automatic protocol search.
                raw = await self.command('0100', timeout=25)
                protocol_reply = await self.command('ATDPN')
                match = re.search(r'(?:^|[\r\n])\s*(A?[1-9A-C])\s*(?:$|[\r\n])', protocol_reply.strip(), re.I)
                if not match:
                    raise ValueError('Could not determine the vehicle protocol. Turn the ignition on and try again.')
                self.protocol = match[1].upper()
                if len(self.protocol) > 1:
                    self.protocol = self.protocol[1:]
                if self.protocol not in ('1', '2', '3', '4', '5', '6', '7', '8', '9'):
                    raise ValueError('This version supports standard OBD protocols 1–9 only.')
                supported = obd.supported(raw, 0, self.protocol)
                if not supported:
                    raise ValueError('No valid standard OBD response. Check ignition, adapter seating, and vehicle support. Raw trace is available.')
                for base in range(32, 225, 32):
                    if not any(f'{base:02X}' in values for values in supported.values()):
                        break
                    extra = obd.supported(await self.command(f'01{base:02X}'), base, self.protocol)
                    for ecu, values in extra.items():
                        supported.setdefault(ecu, []).extend(values)
                self.state.update(ready=True, identity=identity.strip(), protocol=self.protocol, supported=supported, error=None)
            except Exception as exc:
                self.state.update(ready=False, error=str(exc))
                raise
            return dict(self.state)

    async def command(self, cmd, timeout=10):
        # Fixed read-only command boundary; never accept arbitrary car commands.
        if not (cmd in {'ATZ', 'ATE0', 'ATL0', 'ATS1', 'ATH1', 'ATSP0', 'ATCAF1', 'ATAT1', 'ATST96', 'ATDPN', 'ATRV', '03', '07', '0A', '0902'}
                or re.fullmatch(r'01[0-9A-F]{2}', cmd) or re.fullmatch(r'02[0-9A-F]{2}00', cmd)):
            raise ValueError('Command is outside the diagnostic read allowlist.')
        started = time.monotonic()
        raw = ''
        try:
            if self.serial:
                raw = await asyncio.wait_for(asyncio.to_thread(self._serial_command, cmd, timeout), timeout + 1)
            elif self.client and self.client.is_connected and self.tx:
                self.buffer = ''
                self.waiter = asyncio.get_running_loop().create_future()
                await self.client.write_gatt_char(self.tx, (cmd + '\r').encode('ascii'), response='write' in self.tx.properties)
                raw = await asyncio.wait_for(self.waiter, timeout)
            else:
                raise ConnectionError('Adapter transport is not ready.')
            return raw
        except Exception as exc:
            self.state.update(ready=False, error=f'Adapter command failed ({type(exc).__name__}). Reconnect to avoid mixing delayed replies.')
            await self._close()
            raise ConnectionError(self.state['error']) from exc
        finally:
            self.waiter = None
            self.trace.append({'time': now(), 'command': cmd, 'response': raw, 'ms': round((time.monotonic() - started) * 1000)})
            self.trace = self.trace[-2000:]

    def _serial_command(self, cmd, timeout):
        self.serial.reset_input_buffer()
        self.serial.write((cmd + '\r').encode('ascii'))
        deadline, response = time.monotonic() + timeout, bytearray()
        while time.monotonic() < deadline:
            response.extend(self.serial.read(max(1, self.serial.in_waiting)))
            if b'>' in response:
                return response.decode('ascii', errors='replace').split('>', 1)[0]
            if len(response) > 65536:
                raise ConnectionError('Response exceeded the diagnostic buffer.')
        raise TimeoutError('Serial response timed out')

    async def scan(self, vehicle):
        validated = validate_scan({'vehicle': dict(vehicle), 'codes': []})
        vehicle = validated['vehicle']
        expected_vin = validated.get('vin')
        async with self.operation:
            if not self.state['ready']:
                raise ValueError('Initialize the adapter with ignition on first.')
            trace_start = len(self.trace)
            record = {'vehicle': vehicle, 'source': 'Carista EVO / ELM-compatible', 'demo': False,
                      'protocol': self.protocol, 'codes': [], 'readings': [], 'warnings': [],
                      'supported': self.state['supported'], 'scope': 'Standard emissions OBD-II; not a full manufacturer module scan.'}
            detected_vin = obd.vin(await self.command('0902'), self.protocol)
            if detected_vin and expected_vin and detected_vin != expected_vin:
                raise ValueError('The connected vehicle VIN does not match the selected vehicle. Select the correct vehicle or clear the VIN field before scanning.')
            record['vin'] = detected_vin or expected_vin
            record['vinSource'] = 'obd' if detected_vin else 'manual' if expected_vin else 'unavailable'
            if not detected_vin:
                record['warnings'].append('The vehicle did not return a VIN. ' + ('Using the manually entered VIN.' if expected_vin else 'VIN has not been captured for this scan.'))
            record['monitors'] = obd.monitor(await self.command('0101'), self.protocol)
            for mode in ('03', '07', '0A'):
                raw = await self.command(mode)
                parsed = obd.payloads(raw, self.protocol)
                expected = int(mode, 16) + 0x40
                if not any(r['data'] and r['data'][0] == expected for r in parsed):
                    record['warnings'].append(f'Mode {mode} did not provide a valid positive response; coverage is incomplete.')
                record['codes'].extend(obd.dtcs(raw, mode, self.protocol))
            for pid in ('04', '05', '0C', '0D', '42', '21', '30', '31', '5B'):
                if any(pid in values for values in self.state['supported'].values()):
                    record['readings'].extend(obd.readings(await self.command('01' + pid), pid, self.protocol))
            record['readings'].extend(obd.supply_voltage(await self.command('ATRV')))
            # Preserve frame-zero evidence, without claiming it describes every DTC.
            freeze = []
            for pid in ('02', '04', '05', '0C', '0D', '06', '07'):
                freeze.append({'pid': pid, 'raw': await self.command('02' + pid + '00')})
            record['freezeFrame'] = freeze
            record['trace'] = self.trace[trace_start:]
            saved = self.save_scan(record)
            self.state.update(scanId=saved['id'], latest=record['readings'])
            return saved

    async def polling(self, spec):
        if not spec.get('enabled'):
            await self.stop_poll()
            return dict(self.state)
        if not self.state['ready'] or not self.state['scanId']:
            raise ValueError('Run a scan before starting live polling.')
        pids = list(dict.fromkeys(spec.get('pids', [])))
        allowed = (set(p for values in self.state['supported'].values() for p in values) & set(obd.PIDS)) | {'RV'}
        if not pids or len(pids) > 12 or not set(pids) <= allowed:
            raise ValueError('Choose between 1 and 12 supported live readings.')
        interval = int(spec.get('interval', 1000))
        if not 250 <= interval <= 30000:
            raise ValueError('Polling interval must be 250–30000 ms per cycle.')
        await self.stop_poll()
        self.state.update(polling=True, selectedPids=pids, interval=interval, error=None)
        self.poll_task = asyncio.create_task(self._poll())
        return dict(self.state)

    async def _poll(self):
        try:
            while self.state['polling'] and self.state['ready']:
                started = time.monotonic()
                async with self.operation:
                    values = []
                    for pid in self.state['selectedPids']:
                        if pid == 'RV':
                            values.extend(obd.supply_voltage(await self.command('ATRV')))
                        else:
                            values.extend(obd.readings(await self.command('01' + pid), pid, self.protocol))
                    stamp = now()
                    for value in values:
                        value['time'] = stamp
                    self.state['latest'] = values
                    self.state['lastSampleAt'] = stamp
                    self.save_samples(self.state['scanId'], values)
                # Requested period is a lower bound; the serial bus can be slower.
                await asyncio.sleep(max(0.1, self.state['interval'] / 1000 - (time.monotonic() - started)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.state['error'] = f'Polling stopped: {exc}'
        finally:
            self.state['polling'] = False

    async def stop_poll(self):
        self.state['polling'] = False
        task = self.poll_task
        self.poll_task = None
        if task and not task.done():
            # Let an in-flight ELM response finish before stopping the loop.
            await task

    async def _close(self):
        client, serial = self.client, self.serial
        self.client = self.serial = self.tx = self.rx = None
        self.state.update(connected=False, ready=False, polling=False)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        if serial:
            await asyncio.to_thread(serial.close)

    async def disconnect(self):
        await self.stop_poll()
        async with self.operation:
            await self._close()
            self.state.update(device=None, characteristics=[], supported={}, latest=[], scanId=None, profile=None)
        return dict(self.state)
