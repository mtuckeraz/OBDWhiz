import asyncio
import contextlib
from pathlib import Path
import sys
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from core.bluetooth import ADAPTER, DEVICE, Bluetooth, make_agent


RADIO = '/org/bluez/hci0'
RECEIVER = RADIO + '/dev_AA_BB_CC_DD_EE_FF'
ADDRESS = 'AA:BB:CC:DD:EE:FF'


class BluetoothTests(unittest.IsolatedAsyncioTestCase):
    def manager(self):
        manager = Bluetooth()
        manager.controller = RADIO
        manager.objects = {RADIO: {ADAPTER: {'Powered': True}}, RECEIVER: {DEVICE: {
            'Address': ADDRESS, 'Name': 'Carista EVO', 'Adapter': RADIO, 'Paired': False,
            'UUIDs': ['000018f0-0000-1000-8000-00805f9b34fb'], 'RSSI': -42}}}
        manager.refresh = AsyncMock()
        return manager

    async def test_identity_and_pair_state_use_actual_bluez_properties(self):
        manager = self.manager()
        device = manager.devices()[0]
        self.assertEqual(device['id'], ADDRESS)
        self.assertEqual(device['rssi'], -42)
        self.assertFalse(device['paired'])
        self.assertIn('UART', device['hint'])
        with self.assertRaisesRegex(ValueError, 'Discover receivers'):
            manager.device_path('11:22:33:44:55:66')

    async def test_pin_and_confirmation_are_answered_from_web_prompts(self):
        manager = self.manager()
        manager.target = RECEIVER
        pending = asyncio.create_task(manager.prompt(RECEIVER, 'pin'))
        await asyncio.sleep(0)
        prompt_id = manager.state['prompt']['id']
        with self.assertRaisesRegex(ValueError, 'expired'):
            await manager.answer('old-prompt', '1234')
        await manager.answer(prompt_id, '0123')
        self.assertEqual(await pending, '0123')
        self.assertIsNone(manager.state['prompt'])
        pending = asyncio.create_task(manager.prompt(RECEIVER, 'confirm', passkey='123456'))
        await asyncio.sleep(0)
        await manager.answer(manager.state['prompt']['id'], False)
        with self.assertRaisesRegex(ValueError, 'rejected'):
            await pending

    async def test_passkey_validation_and_unselected_device_rejection(self):
        manager = self.manager()
        manager.target = RECEIVER
        with self.assertRaisesRegex(ValueError, 'selected receiver'):
            await manager.prompt('/org/bluez/other', 'pin')
        pending = asyncio.create_task(manager.prompt(RECEIVER, 'passkey'))
        await asyncio.sleep(0)
        prompt_id = manager.state['prompt']['id']
        for value in ('1234567', 'abc', 1234, '１２３４'):
            with self.assertRaises(ValueError):
                await manager.answer(prompt_id, value)
        await manager.answer(prompt_id, '000123')
        self.assertEqual(await pending, 123)

    async def test_pairing_error_is_reported_without_leaving_a_stuck_prompt(self):
        manager = self.manager()
        manager.call = AsyncMock(side_effect=RuntimeError('AuthenticationFailed'))
        manager.target = RECEIVER
        await manager.pair(RECEIVER)
        self.assertEqual(manager.state['status'], 'error')
        self.assertIn('AuthenticationFailed', manager.state['error'])
        self.assertIsNone(manager.state['prompt'])
        self.assertIsNone(manager.target)

    async def test_selected_receiver_survives_restart_without_saving_pins(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'receiver.json'
            manager = Bluetooth(path)
            manager.remember({'id': ADDRESS, 'name': 'Carista EVO', 'transport': 'ble', 'pin': '1234'})
            saved = Bluetooth(path)
            self.assertEqual(saved.selected['id'], ADDRESS)
            self.assertNotIn('pin', path.read_text())

    @unittest.skipUnless(sys.platform == 'linux', 'dbus-fast is installed on Linux')
    async def test_bluez_agent_and_pairing_lifecycle(self):
        manager = self.manager()
        manager.bus = SimpleNamespace(export=lambda *_: None)
        manager.call = AsyncMock(return_value=[])
        manager.set_property = AsyncMock()
        state = await manager.start_pair(ADDRESS)
        self.assertEqual(state['status'], 'pairing')
        await manager.task
        self.assertEqual(manager.state['status'], 'paired')
        self.assertIsNone(manager.target)
        manager.call.assert_any_await('/org/bluez', 'org.bluez.AgentManager1', 'RegisterAgent', 'os', ['/org/obdwhiz/agent', 'KeyboardDisplay'])
        manager.set_property.assert_awaited_once_with(RECEIVER, DEVICE, 'Trusted', 'b', True)

    @unittest.skipUnless(sys.platform == 'linux', 'dbus-fast is installed on Linux')
    async def test_radio_power_requires_an_enumerated_controller(self):
        manager = self.manager()
        manager.set_property = AsyncMock()
        with self.assertRaisesRegex(ValueError, 'available Pi Bluetooth radio'):
            await manager.power('/org/bluez/arbitrary', True)
        manager.set_property.assert_not_awaited()

    @unittest.skipUnless(sys.platform == 'linux' and shutil.which('dbus-daemon'),
                         'a private D-Bus daemon is required for the protocol integration test')
    async def test_pairing_pin_round_trip_over_real_dbus_without_desktop_agent(self):
        from dbus_fast import DBusError, Message, MessageType
        from dbus_fast.aio import MessageBus
        from dbus_fast.constants import PropertyAccess
        from dbus_fast.service import ServiceInterface, dbus_property, method

        daemon = subprocess.Popen(['dbus-daemon', '--session', '--nofork', '--print-address=1'],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        manager = self.manager()
        server = None
        try:
            address = daemon.stdout.readline().strip()
            self.assertTrue(address)
            server = await MessageBus(bus_address=address).connect()
            await server.request_name('org.bluez')
            manager.bus = await MessageBus(bus_address=address).connect()

            class AgentManager(ServiceInterface):
                def __init__(self):
                    super().__init__('org.bluez.AgentManager1')
                    self.capability = None

                @method()
                def RegisterAgent(self, path: 'o', capability: 's'):
                    self.capability = capability

                @method()
                def UnregisterAgent(self, path: 'o'):
                    pass

            class Receiver(ServiceInterface):
                def __init__(self):
                    super().__init__(DEVICE)
                    self.trusted = False

                @method()
                async def Pair(self):
                    reply = await server.call(Message(destination=manager.bus.unique_name,
                        path='/org/obdwhiz/agent', interface='org.bluez.Agent1',
                        member='RequestPinCode', signature='o', body=[RECEIVER]))
                    if reply.message_type != MessageType.METHOD_RETURN or reply.body != ['0123']:
                        raise DBusError('org.bluez.Error.AuthenticationFailed', 'Incorrect test PIN')

                @dbus_property(access=PropertyAccess.READWRITE)
                def Trusted(self) -> 'b':
                    return self.trusted

                @Trusted.setter
                def Trusted(self, value: 'b'):
                    self.trusted = value

            agent_manager = AgentManager()
            receiver = Receiver()
            server.export('/org/bluez', agent_manager)
            server.export(RECEIVER, receiver)
            await manager.start_pair(ADDRESS)

            async def wait_for_prompt():
                while not manager.state['prompt']:
                    if manager.task.done():
                        self.fail(str(manager.state))
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_prompt(), timeout=5)
            self.assertEqual(manager.state['prompt']['kind'], 'pin')
            await manager.answer(manager.state['prompt']['id'], '0123')
            await asyncio.wait_for(manager.task, timeout=5)
            self.assertEqual(manager.state['status'], 'paired', manager.state)
            self.assertEqual(agent_manager.capability, 'KeyboardDisplay')
            self.assertTrue(receiver.trusted)
            self.assertIsNone(manager.state['prompt'])

            # Unsolicited prompts from any other receiver must be rejected.
            unsolicited = await server.call(Message(destination=manager.bus.unique_name,
                path='/org/obdwhiz/agent', interface='org.bluez.Agent1',
                member='RequestPinCode', signature='o', body=['/unselected/receiver']))
            self.assertEqual(unsolicited.message_type, MessageType.ERROR)
            self.assertEqual(unsolicited.error_name, 'org.bluez.Error.Rejected')
        finally:
            if manager.bus:
                await manager.close()
            if server:
                server.disconnect()
            daemon.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                daemon.wait(timeout=5)
            if daemon.poll() is None:
                daemon.kill()
                daemon.wait()
            daemon.stdout.close()
            daemon.stderr.close()
