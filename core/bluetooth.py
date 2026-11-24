"""Pi BlueZ discovery, power and interactive pairing through the web interface."""
import asyncio
import contextlib
import json
from pathlib import Path
import secrets
import sys

ADAPTER = 'org.bluez.Adapter1'
DEVICE = 'org.bluez.Device1'
PROPERTIES = 'org.freedesktop.DBus.Properties'
AGENT_PATH = '/org/obdwhiz/agent'


def unwrap(value):
    if hasattr(value, 'value'):
        return unwrap(value.value)
    if isinstance(value, dict):
        return {k: unwrap(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [unwrap(v) for v in value]
    return value


class Bluetooth:
    def __init__(self, preferences=None):
        self.preferences = Path(preferences) if preferences else None
        self.bus = self.agent = None
        self.objects = {}
        self.controller = None
        self.task = None
        self.target = None
        self.answer_future = None
        self.lock = asyncio.Lock()
        self.state = {'status': 'idle', 'device': None, 'prompt': None, 'error': None}
        self.selected = None
        if self.preferences and self.preferences.exists():
            with contextlib.suppress(ValueError, OSError):
                saved = json.loads(self.preferences.read_text(encoding='utf-8'))
                if isinstance(saved, dict) and isinstance(saved.get('id'), str):
                    self.selected = {k: saved.get(k) for k in ('id', 'name', 'transport')}

    async def ensure_bus(self):
        if sys.platform != 'linux':
            raise ValueError('Web Bluetooth pairing is available on the Raspberry Pi/Linux server.')
        if self.bus is None:
            from dbus_fast.aio import MessageBus
            from dbus_fast.constants import BusType
            self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    async def call(self, path, interface, member, signature='', body=None, timeout=20):
        await self.ensure_bus()
        from dbus_fast import Message, MessageType
        reply = await asyncio.wait_for(self.bus.call(Message(
            destination='org.bluez', path=path, interface=interface, member=member,
            signature=signature, body=body or [])), timeout)
        if reply.message_type == MessageType.ERROR:
            detail = str(reply.body[0]) if reply.body else reply.error_name
            raise RuntimeError(f'{reply.error_name}: {detail}')
        return reply.body

    async def refresh(self):
        result = await self.call('/', 'org.freedesktop.DBus.ObjectManager', 'GetManagedObjects')
        self.objects = unwrap(result[0])
        controllers = sorted(path for path, interfaces in self.objects.items() if ADAPTER in interfaces)
        if self.controller not in controllers:
            self.controller = controllers[0] if controllers else None

    def devices(self):
        result = []
        for path, interfaces in self.objects.items():
            props = interfaces.get(DEVICE)
            if not props or props.get('Adapter') != self.controller:
                continue
            uuids = props.get('UUIDs', [])
            name = props.get('Name') or props.get('Alias') or 'Unnamed Bluetooth receiver'
            hint = ('Carista EVO UART advertised' if '000018f0-0000-1000-8000-00805f9b34fb' in uuids
                    else 'Carista name detected' if 'carista' in name.lower() else 'Verify ELM identity after connecting')
            result.append({'id': props['Address'], 'name': name, 'transport': 'ble',
                           'rssi': props.get('RSSI'), 'paired': props.get('Paired', False),
                           'trusted': props.get('Trusted', False), 'connected': props.get('Connected', False),
                           'uuids': uuids, 'hint': hint, 'saved': bool(self.selected and self.selected['id'] == props['Address'])})
        return sorted(result, key=lambda d: (not d['saved'], 'carista' not in d['name'].lower(), -(d['rssi'] or -100)))

    def cache_devices(self, adapter):
        from bleak.backends.device import BLEDevice
        for path, interfaces in self.objects.items():
            props = interfaces.get(DEVICE)
            if props and props.get('Adapter') == self.controller:
                adapter.device_cache[props['Address']] = BLEDevice(
                    props['Address'], props.get('Name') or props.get('Alias'), {'path': path, 'props': props})

    async def snapshot(self):
        if sys.platform != 'linux':
            return {'available': False, 'controllers': [], 'devices': [], 'pairing': dict(self.state),
                    'selected': self.selected, 'error': None}
        try:
            async with self.lock:
                await self.refresh()
            controllers = [{'id': path, 'name': interfaces[ADAPTER].get('Alias', path.rsplit('/', 1)[-1]),
                            'address': interfaces[ADAPTER].get('Address'),
                            'powered': interfaces[ADAPTER].get('Powered', False)}
                           for path, interfaces in self.objects.items() if ADAPTER in interfaces]
            return {'available': True, 'controllers': controllers, 'controller': self.controller,
                    'devices': self.devices(), 'pairing': dict(self.state), 'selected': self.selected,
                    'error': None if controllers else 'No Bluetooth radio found on the Pi.'}
        except Exception as exc:
            return {'available': False, 'controllers': [], 'devices': [], 'pairing': dict(self.state),
                    'selected': self.selected, 'error': f'Pi Bluetooth unavailable: {exc}'}

    async def power(self, controller, powered):
        if not isinstance(powered, bool):
            raise ValueError('Choose whether Bluetooth should be on or off.')
        if self.task and not self.task.done():
            raise ValueError('Finish or cancel pairing before changing Bluetooth power.')
        async with self.lock:
            await self.refresh()
            if controller not in self.objects or ADAPTER not in self.objects[controller]:
                raise ValueError('Select an available Pi Bluetooth radio.')
            self.controller = controller
            await self.set_property(controller, ADAPTER, 'Powered', 'b', powered)
        return await self.snapshot()

    async def set_property(self, path, interface, name, signature, value):
        from dbus_fast import Variant
        await self.call(path, PROPERTIES, 'Set', 'ssv', [interface, name, Variant(signature, value)])

    async def discover(self, adapter, controller=None):
        if self.task and not self.task.done():
            raise ValueError('Finish or cancel pairing before scanning again.')
        from dbus_fast import Variant
        async with self.lock:
            await self.refresh()
            if controller is not None:
                if controller not in self.objects or ADAPTER not in self.objects[controller]:
                    raise ValueError('Select an available Pi Bluetooth radio.')
                self.controller = controller
            if not self.controller:
                raise ValueError('No Bluetooth radio found on the Pi.')
            await self.set_property(self.controller, ADAPTER, 'Powered', 'b', True)
            await self.call(self.controller, ADAPTER, 'SetDiscoveryFilter', 'a{sv}', [{'Transport': Variant('s', 'le')}])
            await self.call(self.controller, ADAPTER, 'StartDiscovery')
            try:
                await asyncio.sleep(6)
            finally:
                with contextlib.suppress(Exception):
                    await self.call(self.controller, ADAPTER, 'StopDiscovery')
            await self.refresh()
            self.cache_devices(adapter)
            from serial.tools import list_ports
            serial = [{'id': p.device, 'name': p.description, 'transport': 'serial'} for p in list_ports.comports()]
            return {'devices': self.devices() + serial, 'warning': None}

    def device_path(self, address):
        for path, interfaces in self.objects.items():
            props = interfaces.get(DEVICE, {})
            if props.get('Address') == address and props.get('Adapter') == self.controller:
                return path
        raise ValueError('Discover receivers and select one from the list first.')

    async def start_pair(self, address):
        if self.task and not self.task.done():
            raise ValueError('A receiver is already pairing.')
        async with self.lock:
            await self.refresh()
            path = self.device_path(address)
            if self.agent is None:
                self.agent = make_agent(self)
                self.bus.export(AGENT_PATH, self.agent)
                try:
                    await self.call('/org/bluez', 'org.bluez.AgentManager1', 'RegisterAgent', 'os', [AGENT_PATH, 'KeyboardDisplay'])
                except Exception:
                    self.bus.unexport(AGENT_PATH, self.agent)
                    self.agent = None
                    raise
            self.target = path
            self.state.update(status='pairing', device=address, prompt=None, error=None)
            self.task = asyncio.create_task(self.pair(path))
        return dict(self.state)

    async def pair(self, path):
        try:
            if not self.objects[path][DEVICE].get('Paired'):
                await self.call(path, DEVICE, 'Pair', timeout=120)
            await self.set_property(path, DEVICE, 'Trusted', 'b', True)
            self.state.update(status='paired', error=None)
        except asyncio.CancelledError:
            self.state.update(status='cancelled', error=None)
            raise
        except Exception as exc:
            self.state.update(status='error', error=f'Pairing failed: {exc}. Some BLE receivers do not require pairing; try Connect instead.')
        finally:
            if self.answer_future and not self.answer_future.done():
                self.answer_future.cancel()
            self.answer_future = None
            self.state['prompt'] = None
            self.target = None

    async def prompt(self, path, kind, **details):
        if path != self.target:
            raise ValueError('Pairing request is not from the selected receiver.')
        if self.answer_future and not self.answer_future.done():
            raise ValueError('A pairing response is already pending.')
        self.answer_future = asyncio.get_running_loop().create_future()
        self.state['prompt'] = {'id': secrets.token_urlsafe(12), 'kind': kind, **details}
        try:
            return await asyncio.wait_for(self.answer_future, timeout=90)
        finally:
            self.state['prompt'] = None
            self.answer_future = None

    async def answer(self, prompt_id, value):
        prompt = self.state['prompt']
        if not prompt or prompt['id'] != prompt_id or not self.answer_future or self.answer_future.done():
            raise ValueError('This pairing prompt has expired. Start pairing again.')
        if value is False:
            self.answer_future.set_exception(ValueError('Pairing rejected in the web interface.'))
        elif prompt['kind'] == 'pin':
            if not isinstance(value, str) or not 1 <= len(value) <= 16 or not value.isascii() or not value.isprintable():
                raise ValueError('Enter the receiver PIN (1–16 printable ASCII characters).')
            self.answer_future.set_result(value)
        elif prompt['kind'] == 'passkey':
            if not isinstance(value, str) or not value.isascii() or not value.isdigit() or not 1 <= len(value) <= 6:
                raise ValueError('Enter a numeric passkey of up to six digits.')
            self.answer_future.set_result(int(value))
        elif prompt['kind'] == 'confirm' and value is True:
            self.answer_future.set_result(True)
        else:
            raise ValueError('Confirm or reject the pairing request.')
        return {'accepted': True}

    async def cancel(self):
        if self.target:
            with contextlib.suppress(Exception):
                await self.call(self.target, DEVICE, 'CancelPairing')
        if self.task and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        self.state.update(status='cancelled', prompt=None)
        return dict(self.state)

    def remember(self, spec):
        self.selected = {k: spec.get(k) for k in ('id', 'name', 'transport')}
        if self.preferences:
            self.preferences.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.preferences.with_suffix('.tmp')
            temporary.write_text(json.dumps(self.selected), encoding='utf-8')
            temporary.replace(self.preferences)

    async def close(self):
        await self.cancel()
        if self.bus:
            if self.agent:
                with contextlib.suppress(Exception):
                    await self.call('/org/bluez', 'org.bluez.AgentManager1', 'UnregisterAgent', 'o', [AGENT_PATH])
            self.bus.disconnect()
            self.bus = self.agent = None


def make_agent(owner):
    # Import only on Linux: dbus-fast is a platform-specific Bleak dependency.
    from dbus_fast import DBusError
    from dbus_fast.service import ServiceInterface, method

    class PairingAgent(ServiceInterface):
        def __init__(self):
            super().__init__('org.bluez.Agent1')

        async def ask(self, device, kind, **details):
            try:
                return await owner.prompt(device, kind, **details)
            except (ValueError, asyncio.TimeoutError, asyncio.CancelledError) as exc:
                raise DBusError('org.bluez.Error.Rejected', str(exc) or 'Pairing cancelled') from exc

        @method()
        def Release(self):
            owner.state.update(prompt=None)

        @method()
        async def RequestPinCode(self, device: 'o') -> 's':
            return await self.ask(device, 'pin')

        @method()
        async def RequestPasskey(self, device: 'o') -> 'u':
            return await self.ask(device, 'passkey')

        @method()
        async def RequestConfirmation(self, device: 'o', passkey: 'u'):
            await self.ask(device, 'confirm', passkey=f'{passkey:06d}')

        @method()
        async def RequestAuthorization(self, device: 'o'):
            await self.ask(device, 'confirm')

        @method()
        async def AuthorizeService(self, device: 'o', uuid: 's'):
            await self.ask(device, 'confirm', service=uuid)

        @method()
        def DisplayPinCode(self, device: 'o', pincode: 's'):
            if device == owner.target:
                owner.state['prompt'] = {'kind': 'display', 'passkey': pincode}

        @method()
        def DisplayPasskey(self, device: 'o', passkey: 'u', entered: 'q'):
            if device == owner.target:
                owner.state['prompt'] = {'kind': 'display', 'passkey': f'{passkey:06d}', 'entered': entered}

        @method()
        def Cancel(self):
            if owner.answer_future and not owner.answer_future.done():
                owner.answer_future.set_exception(ValueError('Receiver cancelled pairing.'))

    return PairingAgent()
