from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from core.research import Research
from core.store import Store


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'research.sqlite3')
        self.research = Research(self.store)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_request_privacy_search_and_citation_roundtrip(self):
        scan = self.store.save_scan({'vehicle': {'make': 'BMW', 'model': '750e', 'year': 2026},
                                    'vin': 'WBA12345678901234', 'trace': [{'response': 'PRIVATE_RAW'}],
                                    'codes': [{'code': 'P0300', 'ecu': '7E8'}], 'readings': []})
        result = {'status': 'completed', 'output': [
            {'type': 'web_search_call'},
            {'content': [{'type': 'output_text', 'text': 'Evidence [source].', 'annotations': [
                {'type': 'url_citation', 'url': 'https://example.com/reference', 'title': 'Reference', 'start_index': 9, 'end_index': 17}]}]}]}
        captured = []
        def provider(request, **kwargs):
            captured.append(json.loads(request.data))
            return BytesIO(json.dumps(result).encode())
        with patch('urllib.request.urlopen', side_effect=provider):
            self.research._run(scan['id'], 'fake-key', 'gpt-5-mini')
        request = captured[0]
        self.assertNotIn('WBA12345678901234', request['input'])
        self.assertNotIn('PRIVATE_RAW', request['input'])
        self.assertEqual(request['tools'], [{'type': 'web_search'}])
        self.assertEqual(request['tool_choice'], 'required')
        self.assertFalse(request['store'])
        report = self.store.get(scan['id'])['research']
        self.assertEqual(report['status'], 'complete')
        self.assertTrue(report['searched'])
        self.assertEqual(report['annotations'][0]['start'], 9)
        self.assertEqual(report['citations'][0]['url'], 'https://example.com/reference')

    def test_empty_provider_response_is_not_a_successful_report(self):
        scan = self.store.save_scan({'vehicle': {}, 'codes': [{'code': 'P0300'}]})
        with patch('urllib.request.urlopen', return_value=BytesIO(b'{"output": []}')):
            self.research._run(scan['id'], 'fake-key', 'gpt-5-mini')
        report = self.store.get(scan['id'])['research']
        self.assertEqual(report['status'], 'error')
        self.assertIn('no report', report['error'])


if __name__ == '__main__':
    unittest.main()
