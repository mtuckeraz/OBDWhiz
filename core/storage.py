"""Pi storage selection, with a staged copy performed before the server starts."""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import uuid

from .store import DATABASE_FILENAME, LEGACY_DATABASE_FILENAME, data_directory


class Storage:
    def __init__(self, root, settings):
        self.root = Path(root).resolve()
        self.settings = settings
        self.control = self.root / '.local' / 'storage.json'
        self.lock = threading.Lock()

    def config(self):
        return json.loads(self.control.read_text(encoding='utf-8')) if self.control.is_file() else {}

    def save(self, config):
        self.control.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(dir=self.control.parent) as directory:
            staged = Path(directory) / self.control.name
            with staged.open('w', encoding='utf-8') as stream:
                json.dump(config, stream)
                stream.flush()
                os.fsync(stream.fileno())
            staged.replace(self.control)

    def volumes(self):
        command = ['findmnt', '--json', '--list', '--types', 'ext4,xfs,btrfs,f2fs',
                   '--output', 'TARGET,UUID']
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        mounted = (json.loads(result.stdout).get('filesystems') or []) if result.stdout else []
        volumes = [{'id': 'system', 'label': 'Pi system storage', 'root': str(self.control.parent)}]
        for item in mounted:
            target = Path(item['target'])
            if item.get('uuid') and target.is_dir() and not target.is_relative_to('/boot') and target != Path('/'):
                volumes.append({'id': item['uuid'] + ':' + str(target), 'label': str(target), 'root': str(target)})
        return volumes

    def paths(self, config=None):
        location = (self.config() if config is None else config).get('location')
        if location:
            data = Path(location['directory'])
            self.check_volume(location['volume'], data)
            if not any((data / name).is_file() for name in (DATABASE_FILENAME, LEGACY_DATABASE_FILENAME)):
                raise ValueError(f'ProActive database is missing from {data}.')
            return data, data / 'media'
        data = data_directory(self.root, self.settings)
        return data, Path(self.settings.get('OBDWHIZ_MEDIA_DIR') or data / 'media').resolve()

    def check_volume(self, volume_id, path=None):
        volume = next((item for item in self.volumes() if item['id'] == volume_id), None)
        if not volume or (path and not path.resolve().is_relative_to(Path(volume['root']).resolve())):
            raise ValueError('The selected ProActive storage drive is unavailable.')
        return volume

    def overview(self):
        config = self.config()
        data, media = self.paths(config)
        drives = self.volumes()
        current = max((drive for drive in drives if data.is_relative_to(drive['root'])),
                      key=lambda drive: len(drive['root']), default=None)
        for drive in drives:
            drive['writable'] = os.access(self.destination_root(drive), os.W_OK | os.X_OK)
        return {'directory': str(data), 'database': str(data / DATABASE_FILENAME), 'media': str(media),
                'current': current['id'] if current else None, 'drives': drives,
                'pending': bool(config.get('pending')), 'error': config.get('error')}

    @staticmethod
    def destination_root(drive):
        root = Path(drive['root'])
        private = root / 'proactive'
        if drive['id'] != 'system' and private.is_dir() and not private.is_symlink():
            if os.access(private, os.W_OK | os.X_OK):
                return private
        return root

    def request(self, drive):
        with self.lock:
            status = self.overview()
            if status['pending']:
                raise ValueError('A storage move is already scheduled.')
            if drive == status['current']:
                raise ValueError('ProActive already uses that storage drive.')
            volume = self.check_volume(drive)
            root = self.destination_root(volume)
            if drive == 'system':
                root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='.proactive-check-', dir=root):
                pass
            config = self.config()
            config.pop('error', None)
            config['pending'] = drive
            self.save(config)

    def prepare(self):
        with self.lock:
            config = self.config()
            source, media = self.paths(config)
            drive = config.get('pending')
            if not drive:
                return source, media
            try:
                volume = self.check_volume(drive)
                destination = self.copy(source, media, drive, self.destination_root(volume))
            except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
                config['error'] = f'Storage move failed. The original data is still in use. {exc}'
            else:
                config['location'] = {'volume': drive, 'directory': str(destination)}
            del config['pending']
            self.save(config)
            return self.paths(config)

    def copy(self, source, media, drive, root):
        if root.resolve().is_relative_to(media) or root.resolve().is_relative_to(source / 'diagnostics'):
            raise ValueError('Choose a drive outside the current media and diagnostics folders.')
        previous = source / DATABASE_FILENAME
        if not previous.is_file():
            previous = source / LEGACY_DATABASE_FILENAME
        destination = root / ('proactive-' + uuid.uuid4().hex[:12])
        with tempfile.TemporaryDirectory(prefix='.proactive-move-', dir=root) as scratch:
            staged = Path(scratch)
            with closing(sqlite3.connect(previous.as_uri() + '?mode=ro', uri=True)) as old, \
                    closing(sqlite3.connect(staged / DATABASE_FILENAME)) as new:
                old.backup(new)
                new.execute('PRAGMA journal_mode=DELETE')
                if new.execute('PRAGMA quick_check').fetchone() != ('ok',):
                    raise ValueError('The copied database did not pass its integrity check.')
            for name in ('access.key', 'receiver.json'):
                if (source / name).is_file():
                    shutil.copy2(source / name, staged / name)
            for name, path in (('diagnostics', source / 'diagnostics'), ('media', media)):
                if path.is_dir():
                    shutil.copytree(path, staged / name)
            self.check_volume(drive, destination)
            staged.rename(destination)
            if hasattr(os, 'sync'):
                os.sync()
        return destination
