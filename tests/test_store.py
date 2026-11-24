from pathlib import Path
import tempfile
import unittest
from core.store import Store, normalize_vin, validate_scan


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'test.sqlite3')

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_unknown_oem_code_preserved(self):
        scan = self.store.save_scan({'vehicle': {'make': 'McLaren', 'model': 'Artura', 'year': 2026},
                                    'codes': [{'code': 'a1b2c3', 'ecu': 'PCM', 'definition': None}], 'trace': [{'response': 'raw bytes'}]})
        restored = self.store.get(scan['id'])
        self.assertEqual(restored['codes'][0]['code'], 'A1B2C3')
        self.assertIsNone(restored['codes'][0]['definition'])
        self.assertEqual(restored['trace'], [{'response': 'raw bytes'}])

    def test_definitions_require_exact_vehicle_ecu_and_year(self):
        record = {'make': 'BMW', 'model': '750e', 'yearFrom': 2025, 'yearTo': 2026, 'ecu': 'DME', 'code': '123456',
                  'description': 'Supplied by test', 'sourceUrl': 'https://example.com/service', 'reference': 'Test only'}
        self.store.import_definitions({'definitions': [record]})
        scan = {'vehicle': {'make': 'BMW', 'model': '750e', 'year': 2026}, 'codes': [{'code': '123456', 'ecu': 'DME'}]}
        self.assertEqual(len(self.store.matches(scan)), 1)
        scan['vehicle']['model'] = 'M5'
        self.assertEqual(self.store.matches(scan), [])
        scan['vehicle']['model'] = '750e'
        scan['codes'][0]['ecu'] = 'EGS'
        self.assertEqual(self.store.matches(scan), [])

    def test_batch_definition_import_is_atomic(self):
        record = {'make': 'BMW', 'model': '750e', 'yearFrom': 2025, 'yearTo': 2026, 'ecu': 'DME', 'code': '123456',
                  'description': 'Test', 'sourceUrl': 'https://example.com/service', 'reference': 'Test'}
        with self.assertRaises(ValueError):
            self.store.import_definitions({'definitions': [record, {'code': 'bad'}]})
        self.assertEqual(self.store.definitions(), [])

    def test_schema_rejects_invalid_nested_input(self):
        for data in ({}, {'vehicle': [], 'codes': []}, {'vehicle': {}, 'codes': ['P0300']}, {'vehicle': {'year': 9999}, 'codes': []}):
            with self.assertRaises((ValueError, TypeError)):
                validate_scan(data)

    def test_malformed_optional_fields_rejected(self):
        for field, value in (('readings', {}), ('readings', [{'value': float('nan')}]),
                             ('warnings', [None]), ('monitors', ['bad']), ('research', []), ('demo', 'false')):
            with self.assertRaises(ValueError):
                validate_scan({'vehicle': {}, 'codes': [], field: value})

    def test_interrupted_research_recovers_after_restart(self):
        scan = self.store.save_scan({'vehicle': {}, 'codes': [], 'research': {'status': 'running'}})
        self.store.db.close()
        self.store = Store(Path(self.temp.name) / 'test.sqlite3')
        self.assertEqual(self.store.get(scan['id'])['research']['status'], 'error')

    def test_default_vehicle_and_scan_order_is_make_then_model(self):
        for make, model, vin in [('Mercedes-Benz', 'EQE', 'WBA12345678901231'), ('bmw', 'X5', 'WBA12345678901232'), ('BMW', '320i', 'WBA12345678901233')]:
            self.store.save_scan({'vehicle': {'make': make, 'model': model}, 'vin': vin, 'codes': []})
        expected = ['320i', 'X5', 'EQE']
        self.assertEqual([g['vehicle']['model'] for g in self.store.vehicles()], expected)
        self.assertEqual([s['vehicle']['model'] for s in self.store.list()], expected)

    def test_vin_groups_sessions_but_keeps_same_model_cars_separate(self):
        for vin in ('WBA12345678901234', 'WBA12345678901234', 'WBA12345678901235'):
            self.store.save_scan({'vehicle': {'make': 'Hyundai', 'model': 'IONIQ 5'}, 'vin': vin, 'codes': []})
        vehicles = self.store.vehicles()
        self.assertEqual(len(vehicles), 2)
        self.assertEqual(len(vehicles[0]['scans']), 2)
        self.assertEqual(vehicles[0]['latestScanId'], vehicles[0]['scans'][0]['id'])

    def test_no_vin_does_not_merge_unidentified_cars(self):
        for _ in range(2):
            self.store.save_scan({'vehicle': {'make': 'BMW', 'model': 'X5'}, 'codes': []})
        self.assertEqual(len(self.store.vehicles()), 2)

    def test_vin_validation_and_nested_import_normalization(self):
        self.assertEqual(normalize_vin(' wba12345678901234 '), 'WBA12345678901234')
        for invalid in ('bad-vin', 'WBI12345678901234', 123):
            with self.assertRaises(ValueError):
                normalize_vin(invalid)
        record = self.store.save_scan({'vehicle': {'make': 'BMW', 'vin': 'wba12345678901234'}, 'codes': []})
        self.assertEqual(self.store.get(record['id'])['vin'], 'WBA12345678901234')
        self.assertNotIn('vin', record['vehicle'])
        self.assertEqual(self.store.list()[0]['vin'], 'WBA12345678901234')

    def test_context_correction_preserves_scan_evidence_and_samples(self):
        vin = 'WBA12345678901234'
        scan = self.store.save_scan({'vehicle': {'make': 'Unspecified', 'model': 'Connected vehicle', 'symptoms': 'Keep this'},
                                    'vin': vin, 'codes': [{'code': 'U0594'}], 'trace': [{'response': 'preserve'}]})
        self.store.samples(scan['id'], [{'time': '2026-11-23T12:00:00Z', 'pid': 'RV', 'value': 14.6}])
        profile = {'make': 'Hyundai', 'model': 'IONIQ 5', 'year': 2023, 'trim': 'SEL', 'drivetrain': '2WD', 'engine': 'Electric'}
        self.store.update_vehicle(vin, profile)
        restored = self.store.get(scan['id'])
        self.assertEqual(restored['vehicle']['trim'], 'SEL')
        self.assertEqual(restored['vehicle']['symptoms'], 'Keep this')
        self.assertEqual(restored['trace'], scan['trace'])
        self.assertEqual(restored['codes'], scan['codes'])
        self.assertEqual(len(self.store.samples(scan['id'])), 1)
        self.assertEqual(restored['createdAt'], scan['createdAt'])
        # A later scan can recover an identity from its captured VIN.
        later = self.store.save_scan({'vehicle': {'make': 'Unspecified', 'model': 'Connected vehicle'}, 'vin': vin, 'codes': []})
        self.assertEqual(later['vehicle']['make'], 'Hyundai')
        self.assertEqual(later['vehicle']['drivetrain'], '2WD')


if __name__ == '__main__':
    unittest.main()
