# Database schema

The existing SQLite database stays in place. The Phase 1 migration uses its own version table, `dossier_migrations`, and commits additive tables and backfill together. Legacy scans, diagnostics, samples and fleet tables keep their existing schema.

| Table | Relationships and contents |
| --- | --- |
| dossiers, dossier_owners | UUID records. VIN is normalized and unique when present. Owner names are shared across associated vehicles. |
| dossier_ownership | Dated owner associations. One current association per vehicle. |
| dossier_scans | References a scan and dossier. Valid VIN scans are linked while saving and old scans are backfilled. |
| dossier_visits, dossier_notes | Vehicle references, dates and documentation labels or notes. |
| dossier_media | Vehicle and optional same-vehicle visit foreign keys. Holds original filename, byte count, hash, uploader, timestamps and separate location fields. |
| dossier_users | Unique usernames, role, password-change flag and salted PBKDF2 password hashes. Migration 2 preserves existing password hashes and maps manager and technician accounts to User. |
| dossier_audit | Vehicle references where applicable, actor, timestamp, action and prior metadata on revisions. |
| dossier_settings | Missing capture GPS flag policy. |

A draft can receive a VIN later. Correcting a dossier VIN with linked scans requires reviewing the historical association outside the ordinary edit form. Same-name owners are represented by one owner record in Phase 1. Stable customer identities and contact records are a later milestone.

Originals are keyed by vehicle UUID and media UUID. Separate JPEG preview files contain no EXIF. Capture timestamps retain the original offset or carry an explicit unknown-timezone state. Original metadata stays embedded in uploaded bytes. Diagnostic snapshots remain in their existing dated archive.

Migration 3 adds `scanners`, `scanner_assignments`, `scanner_scans` and `scan_jobs`. A transport and hardware address identify one scanner. Its UUID and display number remain stable. Partial indexes protect the open assignment for each vehicle and scanner and prevent two active jobs for one scanner. Assignment changes close old rows. Scan links point to the vehicle and scanner observed at capture time.

Existing fleet readers are included in the registry. Their known vehicle associations are migrated as legacy assignments with no new VIN verification claim. Historical fleet scans with a recorded reader address are linked where possible. Original dated JSON files retain their bytes. Queued jobs resume after restart and interrupted running jobs become failed.
