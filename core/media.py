"""Private evidence storage. Uploaded bytes and generated previews have separate lifecycles."""
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import threading

from .dossiers import clean, now, uid

MAX_BYTES = 50 * 1024 * 1024
MIN_FREE_BYTES = 256 * 1024 * 1024
PHOTO_TYPES = {'JPEG': 'image/jpeg', 'PNG': 'image/png', 'HEIF': 'image/heif'}
CATEGORIES = ('Arrival condition', 'Departure condition', 'Existing damage', 'New damage',
              'Mechanical', 'Parts', 'Interior', 'Exterior', 'Service documentation', 'Miscellaneous')


def location(latitude, longitude, source, accuracy=None):
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in (latitude, longitude)):
        raise ValueError('Location requires numeric latitude and longitude.')
    if abs(latitude) > 90 or abs(longitude) > 180:
        raise ValueError('Coordinates are outside valid limits.')
    if accuracy is not None and (type(accuracy) not in (int, float) or not math.isfinite(accuracy) or accuracy < 0):
        raise ValueError('Invalid location accuracy.')
    return {'latitude': latitude, 'longitude': longitude, 'source': source,
            'accuracyMeters': accuracy, 'verified': False}


def exif_location(gps):
    try:
        def degrees(values):
            return sum(float(value) / 60**index for index, value in enumerate(values))
        north, east = gps[1], gps[3]
        if north not in ('N', 'S') or east not in ('E', 'W') or len(gps[2]) != 3 or len(gps[4]) != 3:
            return None
        return location(degrees(gps[2]) * (-1 if north == 'S' else 1),
                        degrees(gps[4]) * (-1 if east == 'W' else 1), 'original_exif')
    except (KeyError, ValueError, TypeError, ZeroDivisionError, OverflowError):
        return None


def capture_time(original, offset, subseconds=None):
    if not isinstance(original, str):
        return None, 'missing'
    try:
        timestamp = datetime.strptime(original, '%Y:%m:%d %H:%M:%S')
        if isinstance(subseconds, str) and subseconds.isascii() and subseconds.isdigit():
            timestamp = timestamp.replace(microsecond=int(subseconds[:6].ljust(6, '0')))
    except ValueError:
        return None, 'invalid'
    if offset:
        try:
            timestamp = timestamp.replace(tzinfo=datetime.strptime(offset, '%z').tzinfo)
            return timestamp.isoformat(), 'exif_offset'
        except (ValueError, TypeError):
            return timestamp.isoformat(), 'invalid_offset'
    return timestamp.isoformat(), 'unknown_timezone'


def inspect_photo(original, temporary):
    from PIL import Image, ImageOps, UnidentifiedImageError
    from pillow_heif import register_heif_opener

    register_heif_opener()
    try:
        with Image.open(original) as image:
            if image.format not in PHOTO_TYPES:
                raise ValueError('Choose a JPEG, PNG or HEIC/HEIF photo.')
            if image.width * image.height > 64_000_000:
                raise ValueError('Photo exceeds 64 megapixels.')
            exif = image.getexif()
            fields = exif.get_ifd(34665)
            gps = exif.get_ifd(34853)
            captured, zone = capture_time(fields.get(36867, exif.get(36867)),
                                          fields.get(36881, exif.get(36881)),
                                          fields.get(37521, exif.get(37521)))
            capture_location = exif_location(gps)
            # The complete original metadata remains embedded in the untouched original.
            metadata = {'contentType': PHOTO_TYPES[image.format], 'width': image.width, 'height': image.height,
                        'capturedAt': captured, 'captureTimezone': zone, 'captureLocation': capture_location,
                        'exifPresent': bool(exif)}
            preview = ImageOps.exif_transpose(image).convert('RGB')
            preview.info.clear()
            preview.thumbnail((1600, 1600))
            preview.save(temporary / 'preview.jpg', quality=85)
            preview.thumbnail((360, 360))
            preview.save(temporary / 'thumb.jpg', quality=82)
            return metadata
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError, SyntaxError) as exc:
        raise ValueError('This photo could not be decoded.') from exc


class Media:
    def __init__(self, dossiers, root):
        self.dossiers = dossiers
        self.root = Path(root).resolve()
        self.slots = threading.BoundedSemaphore(2)

    def upload(self, vehicle_id, stream, length, options, actor):
        self.dossiers.get(vehicle_id)
        if not 0 < length <= MAX_BYTES:
            raise ValueError('Each upload must contain 1 byte to 50 MB.')
        if not self.slots.acquire(blocking=False):
            raise ValueError('The server is processing other uploads. Retry this file shortly.')
        try:
            return self.store_upload(vehicle_id, stream, length, options, actor)
        finally:
            self.slots.release()

    def store_upload(self, vehicle_id, stream, length, options, actor):
        filename = clean(options.get('name', ''), 255, 'Filename').replace('\\', '/').rsplit('/', 1)[-1]
        kind = options.get('kind')
        if not filename or kind not in ('photo', 'document'):
            raise ValueError('Choose a photo or document with a filename.')
        category = clean(options.get('category', ''), 80, 'Category')
        if category and category not in CATEGORIES:
            raise ValueError('Choose a listed category.')
        metadata = {'caption': clean(options.get('caption', ''), 1000, 'Caption'), 'category': category,
                    'manualLocation': clean(options.get('manualLocation', ''), 250, 'Manual location') or None,
                    'uploadLocation': None}
        if options.get('uploadLocation') is not None:
            position = options['uploadLocation']
            if not isinstance(position, dict):
                raise ValueError('Invalid browser location.')
            metadata['uploadLocation'] = location(position.get('latitude'), position.get('longitude'),
                                                  'upload_browser', position.get('accuracy'))
        visit_id = options.get('visitId') or None
        if visit_id is not None and not isinstance(visit_id, str):
            raise ValueError('Invalid documentation visit.')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if shutil.disk_usage(self.root).free < MIN_FREE_BYTES + length:
            raise ValueError('Storage is low. Free space or change the media location before uploading.')
        media_id = uid()
        vehicle_dir = self.root / vehicle_id
        vehicle_dir.mkdir(exist_ok=True, mode=0o700)
        destination = vehicle_dir / media_id
        with tempfile.TemporaryDirectory(prefix='.upload-', dir=vehicle_dir) as name:
            temporary = Path(name)
            digest = hashlib.sha256()
            original = temporary / 'original'
            with original.open('wb') as output:
                remaining = length
                while remaining:
                    chunk = stream.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        raise ValueError('The upload ended before all bytes arrived. Retry this file.')
                    output.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
                output.flush()
                os.fsync(output.fileno())
            if kind == 'photo':
                metadata.update(inspect_photo(original, temporary))
            else:
                metadata['contentType'] = 'application/octet-stream'
            checksum = digest.hexdigest()
            db, lock = self.dossiers.db, self.dossiers.lock
            moved = False
            try:
                with lock, db:
                    if visit_id and not db.execute('SELECT 1 FROM dossier_visits WHERE id=? AND vehicle_id=?',
                                                  (visit_id, vehicle_id)).fetchone():
                        raise ValueError('Choose a documentation visit belonging to this vehicle.')
                    duplicate = db.execute('SELECT 1 FROM dossier_media WHERE sha256=? LIMIT 1', (checksum,)).fetchone()
                    metadata['duplicate'] = bool(duplicate)
                    db.execute('INSERT INTO dossier_media VALUES(?,?,?,?,?,?,?,?,?,?)',
                               (media_id, vehicle_id, visit_id, kind, filename, json.dumps(metadata),
                                checksum, length, now(), actor))
                    self.dossiers.audit(vehicle_id, actor, 'media_uploaded', {'id': media_id, 'sha256': checksum})
                    temporary.rename(destination)
                    moved = True
            except Exception:
                if moved:
                    shutil.rmtree(destination)
                raise
        return self.get(media_id)

    def usage(self):
        with self.dossiers.lock:
            count, size = self.dossiers.db.execute(
                'SELECT count(*),coalesce(sum(size),0) FROM dossier_media').fetchone()
        root = self.root
        while not root.exists() and root != root.parent:
            root = root.parent
        return {'files': count, 'originalBytes': size, 'freeBytes': shutil.disk_usage(root).free}

    def unpack(self, row, names, flag):
        metadata = json.loads(row[5])
        return {'id': row[0], 'vehicleId': row[1], 'visitId': row[2], 'kind': row[3], 'filename': row[4],
                **metadata, 'sha256': row[6], 'bytes': row[7], 'uploadedAt': row[8], 'uploader': row[9],
                'uploaderName': names.get(row[9], row[9]),
                'captureGpsMissing': row[3] == 'photo' and not metadata.get('captureLocation')
                and flag}

    def list(self, vehicle_id):
        with self.dossiers.lock:
            rows = self.dossiers.db.execute(
                'SELECT * FROM dossier_media WHERE vehicle_id=? ORDER BY uploaded DESC,id', (vehicle_id,)).fetchall()
        names, flag = self.dossiers.actor_names(), self.dossiers.flag_missing_gps()
        return [self.unpack(row, names, flag) for row in rows]

    def get(self, media_id):
        with self.dossiers.lock:
            row = self.dossiers.db.execute('SELECT * FROM dossier_media WHERE id=?', (media_id,)).fetchone()
        if not row:
            raise LookupError('Media not found.')
        return self.unpack(row, self.dossiers.actor_names(), self.dossiers.flag_missing_gps())

    def revise(self, media_id, changes, actor):
        with self.dossiers.lock, self.dossiers.db:
            item = self.get(media_id)
            row = self.dossiers.db.execute('SELECT metadata FROM dossier_media WHERE id=?', (media_id,)).fetchone()
            previous = json.loads(row[0])
            updated = dict(previous)
            for field, limit in (('caption', 1000), ('category', 80), ('manualLocation', 250)):
                if field in changes:
                    updated[field] = clean(changes[field], limit, field)
            if updated['category'] and updated['category'] not in CATEGORIES:
                raise ValueError('Choose a listed category.')
            visit_id = changes.get('visitId', item['visitId']) or None
            if visit_id and (not isinstance(visit_id, str) or not self.dossiers.db.execute(
                    'SELECT 1 FROM dossier_visits WHERE id=? AND vehicle_id=?', (visit_id, item['vehicleId'])).fetchone()):
                raise ValueError('Choose a documentation visit belonging to this vehicle.')
            self.dossiers.db.execute('UPDATE dossier_media SET metadata=?,visit_id=? WHERE id=?',
                                     (json.dumps(updated), visit_id, media_id))
            self.dossiers.audit(item['vehicleId'], actor, 'media_metadata_revised',
                                {'id': media_id, 'previous': previous, 'previousVisitId': item['visitId']})
        return self.get(media_id)

    def path(self, item, variant):
        if variant not in ('original', 'thumb.jpg', 'preview.jpg'):
            raise LookupError('Unknown file variant.')
        if variant != 'original' and item['kind'] != 'photo':
            raise LookupError('Document has no photo preview.')
        path = (self.root / item['vehicleId'] / item['id'] / variant).resolve()
        if self.root not in path.parents or not path.is_file():
            raise LookupError('Archived file is unavailable.')
        return path
