# OBDWhiz feature matrix

| Feature | Phase 0 baseline | Phase 1 scope | Edition direction |
| --- | --- | --- | --- |
| OBD diagnostics | Existing standard OBD reads, VIN, history and files | Preserve and link history to dossiers | Both |
| BLE and serial | Existing Bleak, BlueZ and pySerial | Preserve hardware communication | Both |
| Fleet automation | Existing Pi reader monitoring and event API | Preserve current policy | Editions and policies planned |
| Vehicle records | Scan-derived VIN groups | Persistent dossiers, drafts, owner and plate search | Both |
| Photos and documents | Missing | Local originals, previews, EXIF, timeline and notes | Both |
| Visits | Bluetooth presence events only | Explicit documentation visit references | Service visits planned for Pro |
| Access | Shared installation key | Named staff roles, audit attribution and configurable TLS | Licensing planned |
| Backup | Existing data directory and manual copies | Database and file backup with restore verification | Both |
| Licensing, QR assignments, maintenance and work orders | Planned | Outside milestone | Per specification |

Phase 1 implementation is staged for acceptance testing. See TESTING.md for shop-device validation.

Phase 2 adds scanner profiles, QR labels, VIN verification, assignment history, queued scans and snapshot comparison for both editions. Physical adapter validation is pending.
