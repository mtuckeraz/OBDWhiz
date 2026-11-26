# Phase 1 testing

Run `python -m unittest discover -s tests -v` in the project virtual environment. The suite covers additive migration, partial VIN and plate search, ownership changes, authenticated API access, named staff attribution, 20-photo uploads with HEIC, EXIF GPS and timezone, distinct browser location, original byte integrity and restoration on a separate data directory.

Local verification on Windows ran 87 tests, with 83 passing and four Linux-only tests skipped. A Chromium browser at 390 px width signed in, created a dossier, selected and uploaded a JPEG, and showed its thumbnail, missing-GPS flag and original download link.

For shop acceptance:

1. Set up an isolated test instance. Change both default account passwords, then create a named User account.
2. Sign in on an iPhone, find a test vehicle by plate, add a documentation visit and upload 20 photos, including an original HEIC with GPS. Take another photo using the camera picker and upload a document.
3. Check capture time and timezone, upload time, EXIF GPS and separately authorized upload location. Test permission denial and a file with missing GPS. Download the original and compare its hash.
4. Reopen on a desktop and restart the test server. Verify the same dossier, photos, notes, visit labels and scan history remain. Test administrator edits and ensure staff without sessions cannot fetch original URLs.
5. Run and verify a backup, then restore to a new directory and use a separate instance to open the recovered dossier. If SMB is enabled, verify read-only permissions using a staff account.

Awaiting physical iPhone, trusted shop HTTPS and Pi deployment acceptance. Phase 2 starts after testing feedback.

## Phase 2 acceptance

1. Connect a scanner to a test vehicle and verify its VIN from the scanner profile. Open the profile from the QR using a second authenticated LAN device.
2. Queue a scan from that vehicle's dossier. Verify the job reaches complete or partial, the snapshot references the scanner and vehicle, and the dated JSON is downloadable from scan history.
3. Move the scanner to a second test vehicle. Verify the new VIN, confirm the move as Administrator, and check that the first vehicle retains its scans. Repeat with an already assigned target to check the conflict prompt.
4. Test an absent or sleeping ECU, and a vehicle that does not report a VIN. Check the job status and scan history for failed or unverified observations.
5. Compare two scans and verify that a code can be described as no longer reported only for an ECU and code mode that responded. The automated tests use synthetic adapter responses for these cases.
