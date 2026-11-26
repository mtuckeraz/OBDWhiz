# OBDWhiz implementation plan

## Phase 0 assessment

The existing Python 3.11+ server uses ThreadingHTTPServer and responsive HTML, CSS and JavaScript. SQLite in WAL mode holds scan JSON, diagnostic samples and definitions, the diagnostic-file index, and fleet readers, settings, events and faults. The default data directory is `.local`. It contains the database, browser access key, receiver preferences and dated diagnostic JSON files.

Bleak handles Bluetooth LE GATT, BlueZ provides Linux discovery and pairing, and pySerial supports serial adapters. The ELM-compatible path reads supported PIDs and standard OBD codes, VIN, readiness and available snapshots. Fleet scanning already runs on a separate asyncio loop. It persists reader presence and scan evidence. A live Pi-to-Carista scan was still unverified at the prior deployment. Windows has a manual scanner path. Linux fleet monitoring and pairing use BlueZ.

The current vehicle overview groups scan records by VIN. It lacks permanent vehicle records before a scan, owner history, indexed plate data, media, named users and complete backups. The existing access key authenticates a shared browser session. Preserve these scanner paths, public API shapes and SQLite data. Keep HTTP and the existing frontend for the first milestone. Add Pillow and pillow-heif for image processing.

## Phase 1 implementation

Add an idempotent, versioned SQLite migration for dossiers, owners, dated ownership, notes, documentation visits, media, users and audit records. Backfill only valid VINs from non-demo, non-failed scans. Link subsequent scans by VIN and leave unknown scans separate. Use UUIDs and database references, with private originals and separate previews under configurable storage.

Use named Administrator and User accounts with server-side role checks and mandatory changes to initial passwords. Provide optional TLS configuration and keep localhost development available. Keep upload location, original EXIF location and manually entered location distinct. Support one authenticated upload per request so a mobile batch can report individual failures and retry them.

Provide a verified local backup and restore workflow and optional read-only SMB configuration. Major risks are incorrect VIN associations, large uploads, GPS privacy, incomplete backups during writes and mobile secure-context restrictions. Test migration against populated databases, original-byte retention, authentication, backup restoration and browser uploads. Validate the shop workflow on actual phones before expanding the next phase.

## Later phases

Phase 2 implements independent scanner records and dated assignments. Existing reader addresses provide migration input. VIN and scan records remain stable. The manual queue verifies the vehicle VIN before attribution, and staff can compare scan coverage and DTCs. Physical adapter acceptance remains pending. Phase 3 revisits fleet scheduling and alerts. Licensing, dedicated battery hardware, work orders and broader shop workflows follow the supplied roadmap. They are outside this testing milestone.
