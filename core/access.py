"""Shared-key browser sessions for the Pi's trusted local network."""
from http.cookies import SimpleCookie
import hmac
import os
from pathlib import Path
import secrets
import threading
import time


def load_access_key(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        key = path.read_text(encoding='utf-8').strip()
    else:
        key = secrets.token_urlsafe(24)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(key + '\n')
    if len(key) < 20:
        raise ValueError('The LAN access key must contain at least 20 characters.')
    return key


class BrowserAccess:
    lifetime = 12 * 60 * 60

    def __init__(self, key):
        self.key = key
        self.sessions = {}
        self.attempts = {}
        self.lock = threading.Lock()

    def login(self, key, address):
        with self.lock:
            now = time.monotonic()
            self.sessions = {k: expiry for k, expiry in self.sessions.items() if expiry > now}
            self.attempts = {ip: item for ip, item in self.attempts.items() if item[0] > now - 60}
            attempts = self.attempts.get(address, (now, 0))
            if attempts[1] >= 5:
                raise ValueError('Too many attempts. Wait one minute before trying again.')
            if not isinstance(key, str) or not hmac.compare_digest(key.encode(), self.key.encode()):
                self.attempts[address] = (attempts[0], attempts[1] + 1)
                return None
            self.attempts.pop(address, None)
            token = secrets.token_urlsafe(32)
            self.sessions[token] = now + self.lifetime
            return token

    def token(self, cookie):
        try:
            parsed = SimpleCookie()
            parsed.load(cookie or '')
            return parsed['obdwhiz_session'].value if 'obdwhiz_session' in parsed else ''
        except Exception:
            return ''

    def allowed(self, cookie):
        with self.lock:
            return self.sessions.get(self.token(cookie), 0) > time.monotonic()

    def logout(self, cookie):
        with self.lock:
            self.sessions.pop(self.token(cookie), None)
