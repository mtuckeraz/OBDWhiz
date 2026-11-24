import unittest
from core import obd


class DecoderTests(unittest.TestCase):
    def test_can_dtc_count_not_decoded_as_code(self):
        raw = '7E8 06 43 02 03 00 01 71 00\r7E9 02 43 00 00 00 00 00 00'
        codes = obd.dtcs(raw, '03')
        self.assertEqual([c['code'] for c in codes], ['P0300', 'P0171'])
        self.assertEqual([c['ecu'] for c in codes], ['7E8', '7E8'])

    def test_pending_and_permanent_status(self):
        self.assertEqual(obd.dtcs('7E8 04 47 01 C1 23 00 00 00', '07')[0]['code'], 'U0123')
        self.assertEqual(obd.dtcs('7E8 04 4A 01 01 71 00 00 00', '0A')[0]['status'], 'permanent')

    def test_isotp_interleaved_ecus_and_vin(self):
        vin = 'WBA12345678901234'
        data = [0x49, 2, 1] + list(vin.encode())
        raw = f'7E8 10 14 {bytes(data[:6]).hex(" ")}\r'
        raw += '7E9 04 41 0C 1F 40 00 00 00\r'
        raw += f'7E8 21 {bytes(data[6:13]).hex(" ")}\r'
        raw += f'7E8 22 {bytes(data[13:20]).hex(" ")}\r'
        self.assertEqual(obd.vin(raw), vin)
        self.assertEqual(obd.readings(raw, '0C')[0]['value'], 2000)

    def test_missing_continuation_not_silently_accepted(self):
        raw = '7E8 10 14 49 02 01 57 42 41\r7E8 22 31 32 33 34 35 36 37'
        self.assertIsNone(obd.vin(raw))
        self.assertEqual(obd.payloads(raw), [])

    def test_29_bit_compact_can(self):
        row = obd.readings('18DAF11004410C1F40000000', '0C', '7')[0]
        self.assertEqual(row['ecu'], '18DAF110')
        self.assertEqual(row['value'], 2000)

    def test_short_payload_and_adapter_text_ignored(self):
        raw = '010C\rSEARCHING...\rNO DATA\r7E8 02 41 0C\r>'
        self.assertEqual(obd.readings(raw, '0C'), [])
        self.assertEqual(obd.payloads('CAN ERROR\rSTOPPED\r?'), [])

    def test_support_bitmap_and_multiple_ecus(self):
        raw = '7E8 06 41 00 80 18 00 01 00\r7E9 06 41 00 00 10 00 00 00'
        supported = obd.supported(raw)
        self.assertIn('01', supported['7E8'])
        self.assertIn('0C', supported['7E8'])
        self.assertIn('20', supported['7E8'])
        self.assertEqual(supported['7E9'], ['0C'])

    def test_legacy_headers_and_no_can_count(self):
        raw = '48 6B 10 43 01 33 00 00 00 00 3A'
        code = obd.dtcs(raw, '03', '3')[0]
        self.assertEqual(code['code'], 'P0133')
        self.assertEqual(code['ecu'], '486B10')

    def test_signed_trim_and_temperature(self):
        self.assertEqual(obd.readings('7E8 03 41 06 80 00 00 00 00', '06')[0]['value'], 0)
        self.assertEqual(obd.readings('7E8 03 41 05 00 00 00 00 00', '05')[0]['value'], -40)

    def test_mil_and_count(self):
        row = obd.monitor('7E8 06 41 01 82 07 65 00 00')[0]
        self.assertTrue(row['mil'])
        self.assertEqual(row['dtcCount'], 2)

    def test_connected_vehicle_counter_and_voltage_decoders(self):
        self.assertEqual(obd.readings('7EC 04 41 31 01 00 00 00 00', '31')[0]['value'], 256)
        self.assertEqual(obd.readings('7EC 03 41 30 05 00 00 00 00', '30')[0]['value'], 5)
        self.assertEqual(obd.readings('7EC 03 41 5B FF 00 00 00 00', '5B')[0]['value'], 100)
        self.assertEqual(obd.supply_voltage('\r12.6V\r')[0]['value'], 12.6)
        self.assertEqual(obd.supply_voltage('NO DATA'), [])


if __name__ == '__main__':
    unittest.main()
