from pathlib import Path
import smtplib
import tempfile
import unittest
from unittest.mock import Mock, patch

from core.notifications import Notifications
from core.store import Store


class NotificationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / 'obdwhiz.sqlite3')
        self.addCleanup(self.store.db.close)
        self.ai = Mock(return_value='Check the intake for leaks.')
        self.notifications = Notifications(self.store, self.ai)
        self.store.scan_linker = self.notifications.record_scan
        self.rule = self.notifications.save_rule(dict(name='Misfire and lean', enabled=True, includeAI=False,
            codes=['p0300', 'P0171'], recipients=['fleet@example.com', 'shop@example.com']))
        self.smtp = dict(host='smtp.example.com', port=587, security='starttls', sender='pi@example.com',
                         username='pi@example.com', password='test-password')

    def scan(self, *codes, status='complete', source='Adapter', vin='WBA12345678901234', demo=False):
        return self.store.save_scan(dict(vehicle={'make': 'BMW'}, scanStatus=status, source=source,
            vin=vin, demo=demo, codes=[{'code': code} for code in codes]))

    def test_live_codes_match_in_one_scan_and_rearm_per_vehicle(self):
        self.scan('P0300')
        self.scan('P0171')
        self.scan('P0300', 'P0171', demo=True)
        self.scan('P0300', 'P0171', source='Imported scan')
        self.assertEqual(self.notifications.overview()['deliveries'], [])
        first = self.scan('P0300', 'P0171', status='partial')
        self.scan('P0300', status='partial')
        self.scan('P0300', 'P0171')
        second = self.scan('P0300', 'P0171', vin='WBA12345678901235')
        self.scan('P0300')
        third = self.scan('P0300', 'P0171')
        self.assertEqual([item['scanId'] for item in self.notifications.overview()['deliveries']],
                         [third['id'], second['id'], first['id']])
        self.notifications.save_rule({**self.rule, 'enabled': False})
        self.scan('P0300')
        self.scan('P0300', 'P0171')
        self.assertEqual(len(self.notifications.overview()['deliveries']), 3)

    @patch('core.notifications.send_mail')
    def test_delivery_uses_saved_password_and_optional_ai(self, send):
        saved = self.notifications.configure_smtp(self.smtp)
        self.assertNotIn('password', saved)
        self.assertTrue(saved['hasPassword'])
        self.notifications.configure_smtp({**self.smtp, 'password': ''})
        self.scan('P0300', 'P0171')
        self.assertTrue(self.notifications.deliver_next())
        self.ai.assert_not_called()
        self.assertEqual(send.call_args.args[0]['password'], self.smtp['password'])
        self.assertIn('Matched codes: P0171, P0300', send.call_args.args[3])
        self.assertEqual(self.notifications.overview()['deliveries'][0]['status'], 'sent')

        self.notifications.save_rule({**self.rule, 'includeAI': True})
        self.scan()
        self.scan('P0300', 'P0171')
        self.assertTrue(self.notifications.deliver_next())
        self.ai.assert_called_once()
        self.assertIn('Check the intake for leaks.', send.call_args.args[3])

    @patch('core.notifications.send_mail')
    def test_retries_only_unsent_recipients_when_ai_is_unavailable(self, send):
        self.notifications.configure_smtp(self.smtp)
        self.notifications.save_rule({**self.rule, 'includeAI': True})
        self.ai.side_effect = TimeoutError
        self.scan('P0300', 'P0171')
        send.side_effect = [None, smtplib.SMTPException('relay details'), None]
        self.assertTrue(self.notifications.deliver_next())
        self.assertFalse(self.notifications.deliver_next())
        with self.store.db:
            self.store.db.execute('UPDATE notification_outbox SET due=0')
        restarted = Notifications(self.store, self.ai)
        self.assertTrue(restarted.deliver_next())
        self.ai.assert_called_once()
        self.assertEqual([call.args[1] for call in send.call_args_list],
                         ['fleet@example.com', 'shop@example.com', 'shop@example.com'])
        self.assertIn('AI suggestions were unavailable', send.call_args.args[3])
        self.assertEqual(restarted.overview()['deliveries'][0]['status'], 'sent')
