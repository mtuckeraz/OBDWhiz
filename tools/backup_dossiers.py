"""Create private backup directories, and verify or restore them into a new data directory."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.storage import Storage
from core.store import DATABASE_FILENAME, LEGACY_DATABASE_FILENAME

CONFIG = ('access.key', 'receiver.json')


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def database(path, immutable=False):
    uri = path.resolve().as_uri() + '?mode=ro'
    return sqlite3.connect(uri + ('&immutable=1' if immutable else ''), uri=True)


def media_records(db):
    return db.execute('SELECT vehicle_id,id,kind,sha256 FROM dossier_media').fetchall()


def copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def backup(data, media, destination, settings=None):
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.backup-', dir=destination) as directory:
        staging = Path(directory)
        source_file = data / DATABASE_FILENAME
        if not source_file.exists():
            source_file = data / LEGACY_DATABASE_FILENAME
        source = database(source_file)
        snapshot = sqlite3.connect(staging / DATABASE_FILENAME)
        try:
            source.backup(snapshot)
            snapshot.execute('PRAGMA journal_mode=DELETE')
            for vehicle_id, media_id, kind, sha in media_records(snapshot):
                relative = Path(vehicle_id) / media_id
                variants = ('original', 'thumb.jpg', 'preview.jpg') if kind == 'photo' else ('original',)
                for variant in variants:
                    copy(media / relative / variant, staging / 'media' / relative / variant)
                if digest(staging / 'media' / relative / 'original') != sha:
                    raise ValueError(f'Original integrity check failed for {media_id}.')
            for relative, payload in snapshot.execute('SELECT path,payload FROM diagnostic_files WHERE written_at IS NOT NULL'):
                file = staging / relative
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(payload.encode('utf-8'))
        finally:
            snapshot.close()
            source.close()
        for name in CONFIG:
            if (data / name).exists():
                copy(data / name, staging / name)
        if settings and settings.is_file():
            copy(settings, staging / 'deployment.env')
        manifest = {p.relative_to(staging).as_posix(): digest(p) for p in staging.rglob('*') if p.is_file()}
        (staging / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        verify(staging)
        published = destination / ('proactive-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ'))
        staging.rename(published)
        return published


def verify(directory):
    directory = directory.resolve()
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    files = {p.relative_to(directory).as_posix() for p in directory.rglob('*')
             if p.is_file() and p.relative_to(directory).as_posix() != 'manifest.json'}
    database_name = DATABASE_FILENAME if DATABASE_FILENAME in files else LEGACY_DATABASE_FILENAME
    if set(manifest) != files or database_name not in files:
        raise ValueError('Backup files do not match the manifest.')
    for relative, sha in manifest.items():
        path = directory / relative
        if directory not in path.resolve().parents or digest(path) != sha:
            raise ValueError(f'Backup verification failed for {relative}.')
    db = database(directory / database_name, immutable=True)
    try:
        if db.execute('PRAGMA integrity_check').fetchone() != ('ok',) or db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Backup database integrity check failed.')
        for vehicle_id, media_id, kind, sha in media_records(db):
            base = directory / 'media' / vehicle_id / media_id
            if digest(base / 'original') != sha or (kind == 'photo' and
                    any(not (base / name).is_file() for name in ('thumb.jpg', 'preview.jpg'))):
                raise ValueError(f'Backup media integrity check failed for {media_id}.')
    finally:
        db.close()
    return manifest


def restore(source, target):
    manifest = verify(source)
    if target.exists():
        raise ValueError('Choose a new directory for restoration.')
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.restore-', dir=target.parent) as directory:
        staging = Path(directory)
        for relative in manifest:
            name = DATABASE_FILENAME if relative == LEGACY_DATABASE_FILENAME else relative
            copy(source / relative, staging / name)
        staging.rename(target)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    create = actions.add_parser('create')
    create.add_argument('destination', type=Path)
    create.add_argument('--data', type=Path)
    create.add_argument('--media', type=Path)
    actions.add_parser('verify').add_argument('source', type=Path)
    recover = actions.add_parser('restore')
    recover.add_argument('source', type=Path)
    recover.add_argument('target', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.action == 'create':
            from dotenv_settings import deployment_settings
            settings = ROOT / '.env'
            values = deployment_settings(settings)
            data, media = Storage(ROOT, values).paths() if not args.data else (
                args.data, Path(values.get('OBDWHIZ_MEDIA_DIR') or args.data / 'media'))
            media = args.media or media
            print(backup(data, media, args.destination, settings))
        elif args.action == 'verify':
            print(f'Verified {len(verify(args.source))} files.')
        else:
            print(restore(args.source, args.target))
    except (OSError, ValueError, sqlite3.Error) as exc:
        sys.exit(str(exc))


if __name__ == '__main__':
    main()
