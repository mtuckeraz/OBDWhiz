import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from core.adapter import Adapter


class FakeClient:
    is_connected = True
    async def write_gatt_char(self, *_args, **_kwargs):
        pass
    async def disconnect(self):
        self.is_connected = False


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_bluez_failure_refreshes_the_device_on_the_same_radio(self):
        address = 'AA:BB:CC:DD:EE:FF'
        device = SimpleNamespace(address=address, details={'path': '/org/bluez/hci1/dev_AA_BB_CC_DD_EE_FF'})
        fresh = SimpleNamespace(address=address, details=device.details)
        first = SimpleNamespace(connect=AsyncMock(side_effect=RuntimeError('org.bluez.Error.InProgress')), disconnect=AsyncMock())
        second = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock())
        adapter = Adapter(lambda s: s, lambda *a: None)
        with patch('bleak.BleakClient', side_effect=[first, second]) as clients, \
                patch('bleak.BleakScanner.find_device_by_address', new=AsyncMock(return_value=fresh)) as discovery, \
                patch('core.adapter.sys.platform', 'linux'):
            result = await adapter.connect_ble(device)
        self.assertIs(result, fresh)
        discovery.assert_awaited_once_with(address, timeout=8, bluez={'adapter': 'hci1'})
        self.assertEqual(clients.call_count, 2)
        first.disconnect.assert_awaited_once()

    async def test_mutating_commands_rejected_at_transport_boundary(self):
        adapter = Adapter(lambda s: s, lambda *a: None)
        for command in ('04', '2701', '2EF1900000', '1101', 'ATSH7E0', '010C\r04'):
            with self.assertRaises(ValueError):
                await adapter.command(command)
        self.assertEqual(adapter.trace, [])

    async def test_timeout_disconnects_and_requires_reinitialization(self):
        adapter = Adapter(lambda s: s, lambda *a: None)
        client = FakeClient()
        adapter.client = client
        adapter.tx = SimpleNamespace(properties=['write'])
        adapter.state.update(connected=True, ready=True)
        with self.assertRaises(ConnectionError):
            await adapter.command('010C', timeout=0.01)
        self.assertFalse(adapter.state['ready'])
        self.assertFalse(adapter.state['connected'])
        self.assertFalse(client.is_connected)
        self.assertEqual(adapter.trace[-1]['command'], '010C')

    async def test_notification_fragments_resolve_only_at_prompt(self):
        adapter = Adapter(lambda s: s, lambda *a: None)
        adapter.waiter = asyncio.get_running_loop().create_future()
        adapter.received(None, b'7E8 04 41')
        self.assertFalse(adapter.waiter.done())
        adapter.received(None, b' 0C 1F 40\r>')
        self.assertEqual(await adapter.waiter, '7E8 04 41 0C 1F 40\r')

    async def test_scan_lifecycle_and_incomplete_coverage(self):
        saved = []
        def save(record):
            record['id'] = 'test-scan'
            saved.append(record)
            return record
        adapter = Adapter(save, lambda *a: None)
        adapter.state.update(ready=True, supported={'7E8': ['01', '05', '0C']})
        responses = {'0902': 'NO DATA', '0101': '7E8 06 41 01 81 00 00 00 00',
                     '03': '7E8 04 43 01 03 00 00 00 00', '07': '7E8 02 47 00 00 00 00 00 00',
                     '0A': 'NO DATA', '0105': '7E8 03 41 05 7D 00 00 00 00', '010C': '7E8 04 41 0C 1F 40 00 00 00'}
        async def command(cmd, **kwargs):
            raw = responses.get(cmd, 'NO DATA')
            adapter.trace.append({'command': cmd, 'response': raw})
            return raw
        adapter.command = command
        record = await adapter.scan({'make': 'BMW', 'model': 'Test vehicle', 'year': 2026})
        self.assertEqual(record['codes'][0]['code'], 'P0300')
        self.assertEqual(len(record['warnings']), 2)
        self.assertTrue(any('0A' in w for w in record['warnings']))
        self.assertTrue(any('VIN' in w for w in record['warnings']))
        self.assertEqual(record['readings'][1]['value'], 2000)
        self.assertEqual(adapter.state['scanId'], 'test-scan')
        self.assertEqual(len(record['freezeFrame']), 7)
        self.assertEqual(len(saved), 1)

    async def test_polling_only_allows_discovered_pids(self):
        cycles = []
        adapter = Adapter(lambda s: s, lambda *args: cycles.append(args))
        adapter.state.update(ready=True, scanId='test', supported={'7E8': ['0C']})
        with self.assertRaises(ValueError):
            await adapter.polling({'enabled': True, 'pids': ['42'], 'interval': 250})
        async def command(cmd, **kwargs):
            await asyncio.sleep(0.02)
            return '7E8 04 41 0C 1F 40 00 00 00'
        adapter.command = command
        await adapter.polling({'enabled': True, 'pids': ['0C'], 'interval': 250})
        await asyncio.sleep(0.01)
        await adapter.stop_poll()
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0][1][0]['value'], 2000)
        self.assertFalse(adapter.state['polling'])

    async def test_scanner_vin_is_stored_and_wrong_selected_vin_rejected(self):
        saved=[]
        def save(record):
            record['id']='vin-test'
            saved.append(record)
            return record
        adapter=Adapter(save,lambda *args:None)
        adapter.state.update(ready=True,supported={})
        vin='WBA12345678901234'
        data=[0x49,2,1]+list(vin.encode())
        response=f'7E8 10 14 {bytes(data[:6]).hex(" ")}\r7E8 21 {bytes(data[6:13]).hex(" ")}\r7E8 22 {bytes(data[13:]).hex(" ")}'
        async def command(cmd,**kwargs):
            return response if cmd=='0902' else 'NO DATA'
        adapter.command=command
        with self.assertRaisesRegex(ValueError,'VIN does not match'):
            await adapter.scan({'make':'BMW','vin':'WBA12345678901235'})
        self.assertEqual(saved,[])
        result=await adapter.scan({'make':'BMW','vin':vin.lower()})
        self.assertEqual(result['vin'],vin)
        self.assertEqual(result['vinSource'],'obd')
        self.assertNotIn('vin',result['vehicle'])

    async def test_manual_vin_fallback_is_labeled(self):
        def save(record):
            record['id']='manual-vin-test'
            return record
        adapter=Adapter(save,lambda *args:None)
        adapter.state.update(ready=True,supported={})
        async def command(cmd,**kwargs):
            return 'NO DATA'
        adapter.command=command
        result=await adapter.scan({'make':'BMW','vin':'WBA12345678901234'})
        self.assertEqual(result['vin'],'WBA12345678901234')
        self.assertEqual(result['vinSource'],'manual')
        self.assertTrue(any('manually entered VIN' in w for w in result['warnings']))


if __name__ == '__main__':
    unittest.main()
