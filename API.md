# Internal API

Send `X-Apex-Client: 1` on POST requests and use the session cookie from `POST /api/login`. Supply `username` and `password`. When the session reports `mustChangePassword`, submit matching `password` and `confirmation` to `POST /api/password` to activate the session. `GET /api/session` returns the current identity. Dossier resources require login on localhost and the LAN.

| Endpoint | Operation |
| --- | --- |
| GET /api/dossiers?q=… | Case-insensitive search. Returns up to 100 matches. |
| POST /api/dossiers | Create a dossier with optional normalized VIN or draft vehicle details. |
| GET/POST /api/dossiers/UUID | Read or edit a dossier. Includes ownership, notes, visits, scans and media. |
| POST /api/dossiers/UUID/note or /visit | Add a note or explicit documentation visit using `value`. |
| POST /api/dossiers/UUID/media | Raw file body, up to 50 MiB. Set `X-Upload-Metadata` to URI-encoded JSON containing `name`, `kind` (photo or document), optional `caption`, `category`, `visitId`, `manualLocation` and `uploadLocation` with latitude, longitude and accuracy. |
| GET /api/media/UUID/original | Download the original as an attachment. |
| GET /api/media/UUID/thumb.jpg or /preview.jpg | View a generated photo derivative. |
| POST /api/media/UUID/metadata | Revise caption, category, visit or manual location with audit history. |
| GET/POST /api/dossiers/policy | Read categories and GPS flag policy, or change `flagMissingGps` as administrator. |
| GET/POST /api/staff | List or create named accounts as administrator. |
| GET /api/staff/storage | Read original file count, stored original bytes and available disk space as administrator. |

Users can create dossiers, run diagnostics and add notes, visits and media. Users can edit descriptions for their own uploads. Administrators can manage accounts, fleet settings, existing dossiers and all media descriptions. Authenticated staff share a repository and can read originals and location metadata. Legacy diagnostics and fleet routes remain available.

## Scanner registry and queues

| Endpoint | Operation |
| --- | --- |
| GET /api/adapters | List scanner profiles and current assignments. |
| POST /api/adapters | Register or update a scanner by `transport` and `address`. Administrator required. |
| GET /api/adapters/UUID | Read scanner details, assignment history, capabilities and jobs. |
| GET /api/adapters/UUID/qr | Generate an authenticated QR label using the current server address. |
| POST /api/adapters/UUID/release | End the active assignment and preserve its history. Administrator required. |
| POST /api/scan-jobs | Enqueue `verify` or `scan` for `scannerId`, with `vehicleId` required for a dossier scan. Returns HTTP 202. |
| GET /api/scan-jobs/UUID | Read job state, any confirmation request and the resulting scan ID. |
| POST /api/scan-jobs/UUID/confirm | Confirm with the indicated `vehicleId` or cancel with `cancel: true`. Administrator required. |
| GET /api/dossiers/UUID/diagnostics | Read the current assignment, scan history and recent jobs. |
| GET /api/dossiers/UUID/compare?before=ID&after=ID | Compare two scans linked to this dossier. Missing ECU coverage stays unconfirmed. |
