"""Remove vehicle records and diagnostic sessions with retryable file cleanup."""

from pathlib import Path
import shutil


class Deletions:
    def __init__(self, store, dossiers, scanners, media_root):
        self.store = store
        self.db = store.db
        self.dossiers = dossiers
        self.scanners = scanners
        self.roots = {'diagnostic': store.directory, 'media': media_root}
        with store.lock, self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS deletion_files (
                kind TEXT NOT NULL, path TEXT NOT NULL, PRIMARY KEY(kind,path))""")
        self.clean_files()

    def remove_builtin_demos(self):
        with self.store.lock:
            scans = self.db.execute("""SELECT id FROM scans WHERE json_extract(data,'$.demo')=1
                AND json_extract(data,'$.source')='Built-in demonstration'
                AND json_extract(data,'$.vehicle.make')='BMW'
                AND json_extract(data,'$.vehicle.model')='Demonstration vehicle'""").fetchall()
            for (scan_id,) in scans:
                self.scan(scan_id, 'installation')

    def clean_files(self):
        with self.store.lock, self.db:
            paths = self.db.execute('SELECT kind,path FROM deletion_files').fetchall()
            for kind, relative in paths:
                root = self.roots[kind]
                parts = Path(relative).parts
                path = root / relative
                try:
                    resolved = path.resolve()
                    if (root not in resolved.parents or resolved != path
                            or (kind == 'diagnostic' and (len(parts) != 3 or parts[0] != 'diagnostics'))
                            or (kind == 'media' and len(parts) != 2)):
                        continue
                    if kind == 'media':
                        if path.exists():
                            shutil.rmtree(path)
                    else:
                        path.unlink(missing_ok=True)
                except (OSError, RuntimeError):
                    continue
                self.db.execute('DELETE FROM deletion_files WHERE kind=? AND path=?', (kind, relative))
            pending = self.db.execute('SELECT 1 FROM deletion_files LIMIT 1').fetchone()
        result = {'deleted': True}
        if pending:
            result['warning'] = 'Some archived files are pending cleanup. The server retries cleanup on restart.'
        return result

    def vehicle(self, vehicle_id, actor):
        with self.store.lock, self.db:
            vehicle = self.dossiers.get(vehicle_id)
            assignment = self.scanners.current(vehicle_id=vehicle_id)
            scanner_id = assignment['scannerId'] if assignment else None
            if self.db.execute("""SELECT 1 FROM scan_jobs
                WHERE (vehicle_id=? OR scanner_id=?) AND status IN ('queued','running','needs_confirmation')""",
                               (vehicle_id, scanner_id)).fetchone():
                raise ValueError('Resolve the active scan job before deleting this vehicle.')
            if self.db.execute("""SELECT 1 FROM scans
                WHERE json_extract(data,'$.vehicleId')=? AND json_extract(data,'$.research.status')='running'""",
                               (vehicle_id,)).fetchone():
                raise ValueError('Wait for active research to finish before deleting this vehicle.')
            if assignment:
                self.scanners.sync_fleet(self.scanners.get(scanner_id), None)
            if vehicle['vin']:
                self.db.execute("""UPDATE fleet_readers SET data=json_set(data,'$.enabled',json('false'),
                    '$.vin',NULL,'$.vehicle',json('{}'),'$.pausedForScannerAssignment',json('true'),
                    '$.lastScanId',NULL,'$.lastScanAt',NULL,'$.lastAttemptAt',NULL,'$.returnScanDueAt',NULL,
                    '$.lastCodeCount',NULL,'$.lastCodeCoverage',json('{}'),
                    '$.lastError',NULL,'$.lastScanStatus','waiting')
                    WHERE json_extract(data,'$.vin')=?""", (vehicle['vin'],))
            self.db.execute("""INSERT OR IGNORE INTO deletion_files
                SELECT 'media',vehicle_id||'/'||id FROM dossier_media WHERE vehicle_id=?""", (vehicle_id,))
            self.db.execute("""UPDATE scans SET data=json_remove(data,'$.vehicleId')
                WHERE json_extract(data,'$.vehicleId')=?""", (vehicle_id,))
            self.db.execute('UPDATE scan_jobs SET vehicle_id=NULL WHERE vehicle_id=?', (vehicle_id,))
            for table in ('scanner_assignments', 'dossier_media', 'dossier_visits', 'dossier_notes',
                          'dossier_scans', 'dossier_ownership', 'dossier_audit'):
                self.db.execute(f'DELETE FROM {table} WHERE vehicle_id=?', (vehicle_id,))
            self.db.execute('DELETE FROM dossiers WHERE id=?', (vehicle_id,))
            self.dossiers.audit(None, actor, 'vehicle_deleted', {'id': vehicle_id})
        return self.clean_files()

    def scan(self, scan_id, actor):
        with self.store.lock, self.db:
            scan = self.store.get(scan_id)
            if scan.get('research', {}).get('status') == 'running':
                raise ValueError('Wait for active research to finish before deleting this scan.')
            self.db.execute("""INSERT OR IGNORE INTO deletion_files
                SELECT 'diagnostic',path FROM diagnostic_files WHERE scan_id=?""", (scan_id,))
            self.db.execute('UPDATE scan_jobs SET scan_id=NULL WHERE scan_id=?', (scan_id,))
            for table in ('dossier_scans', 'scanner_scans', 'samples', 'diagnostic_files', 'notification_outbox'):
                self.db.execute(f'DELETE FROM {table} WHERE scan_id=?', (scan_id,))
            self.db.execute("""UPDATE fleet_readers SET data=json_set(data,
                '$.lastScanId',NULL,'$.lastCodeCount',NULL,'$.lastCodeCoverage',json('{}'),
                '$.lastScanStatus',CASE WHEN json_extract(data,'$.lastScanStatus') IN ('complete','partial')
                    THEN 'waiting' ELSE json_extract(data,'$.lastScanStatus') END)
                WHERE json_extract(data,'$.lastScanId')=?""", (scan_id,))
            self.db.execute("""UPDATE fleet_events SET data=json_remove(data,'$.scanId','$.diagnosticFile')
                WHERE json_extract(data,'$.scanId')=?""", (scan_id,))
            self.db.execute("""UPDATE fleet_events SET data=json_remove(data,'$.lastScanId')
                WHERE json_extract(data,'$.lastScanId')=?""", (scan_id,))
            self.db.execute("""UPDATE fleet_faults SET data=json_remove(data,'$.scanId')
                WHERE json_extract(data,'$.scanId')=?""", (scan_id,))
            self.db.execute('DELETE FROM scans WHERE id=?', (scan_id,))
            self.dossiers.audit(None, actor, 'scan_deleted', {'id': scan_id})
        return self.clean_files()
