"""Render a system service for the installing user; never run the app as root."""
from pathlib import Path
import re
import sys


def quoted(value, *, executable=False):
    if any(c in value for c in '\r\n\0'):
        raise ValueError('Service paths must not contain line breaks or NUL.')
    value = value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    if executable:
        value = value.replace('$', '$$')
    return f'"{value}"'


def working_directory(value):
    # Unlike ExecStart, WorkingDirectory does not strip shell-style quotes or
    # backslash escapes. Only systemd specifiers need escaping here.
    if any(ord(c) < 32 or ord(c) == 127 for c in value) or value != value.strip() or value.endswith('\\'):
        raise ValueError('Unsupported control characters or trailing whitespace/backslash in service directory.')
    return value.replace('%', '%%')


def render(directory, user, group):
    for name in (user, group):
        if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_-]*\$?', name):
            raise ValueError('Unsupported system user/group name.')
    root = Path(directory).resolve()
    return f'''[Unit]
Description=OBDWhiz Raspberry Pi diagnostic workbench
After=bluetooth.service network.target
Requires=bluetooth.service

[Service]
Type=simple
User={user}
Group={group}
SupplementaryGroups=bluetooth dialout
WorkingDirectory={working_directory(str(root))}
ExecStart={quoted(str(root / '.venv/bin/python'), executable=True)} {quoted(str(root / 'server.py'), executable=True)}
Restart=on-failure
RestartSec=5
KillSignal=SIGINT
TimeoutStopSec=30
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
'''


if __name__ == '__main__':
    if len(sys.argv) != 4:
        sys.exit('Usage: render_service.py DIRECTORY USER GROUP')
    print(render(*sys.argv[1:]), end='')
