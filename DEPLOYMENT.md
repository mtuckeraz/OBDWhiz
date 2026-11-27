# Deployment and backups

Use the existing Pi installer or Windows launcher. Install requirements after updating the checkout. On Windows, use `python server.py --lan` from the project virtual environment for authenticated LAN access. On a new database, sign in as `admin` or `user` with password `123456`. Set a new password for both accounts before sharing the server address. The administrator can add named accounts under Vehicle Scans.

## USB storage on Raspberry Pi

Mount a USB SSD to a fixed path such as `/mnt/proactive-data`. Use its filesystem UUID in `/etc/fstab` so the same drive is mounted after a reboot. An ext4 filesystem works with the Pi's Linux file permissions. Give the account that runs ProActive write access to its data directory.

Sign in as an administrator and open **Setup → Storage**. Select the drive, then choose **Move storage and restart**. ProActive copies the database, diagnostic files, and media into a new directory on the selected drive and keeps the previous copy. The dialog shows the active paths after you sign back in. Reinstall the generated unit with `bash install-pi.sh --service` if an older unit was explicitly tied to a different drive.

For a fresh setup using configuration files, set `OBDWHIZ_DATA_MOUNT=/mnt/proactive-data` in `.env`. That makes `/mnt/proactive-data/proactive` the default unless `OBDWHIZ_DATA_DIR` specifies another directory on the drive. The browser selection takes precedence after a migration. ProActive checks that the selected filesystem is mounted before opening the database. Keep a backup on a separate device.

Upgrades keep existing usernames and passwords. Administrator permissions are retained. Former Manager and Technician accounts become User accounts. If an existing User already owns the username `admin` and there is no Administrator, the migration creates a separate administrator under `obdwhiz-admin` (or an unused `obdwhiz-*` name if that is taken).

For production LAN use, set `OBDWHIZ_TLS_CERT` and `OBDWHIZ_TLS_KEY` to a certificate chain and private key trusted by shop devices. Set `OBDWHIZ_PUBLIC_HOST` to the browser host and port if using a custom hostname. Browse to the HTTPS URL. iPhone browser geolocation requires a trusted secure context and permission. Existing HTTP installations can use file upload, EXIF and manual location for testing. Verify TLS and phone permissions at the shop.

Run these commands using the project virtual environment while signed in as the app account. Use a private disk or network destination with access controls. Backups include account hashes, location data and project .env if present.

```text
python tools/backup_dossiers.py create BACKUP_DESTINATION
python tools/backup_dossiers.py verify BACKUP_DIRECTORY
python tools/backup_dossiers.py restore BACKUP_DIRECTORY NEW_DATA_DIRECTORY
```

The create command prints a directory only after database and hash verification. Schedule that command daily with cron on Linux or Task Scheduler on Windows and retain its output and exit status. The scheduler needs write access to the destination. For restoration, point a separate instance at the restored data directory and its media subdirectory. Review the recovered deployment.env before using it, especially any paths to the former storage and TLS files. Keep certificate files and environment-only secrets in their own secured backup. Stop production before swapping a verified restored directory into use.

## Optional read-only SMB

Share the repository only on a trusted company LAN and only with staff authorized to download originals and their embedded EXIF. Share the media directory, with no guest access. On Samba, use a share like:

```ini
[OBDWhiz-media]
    path = /mnt/ssd/obdwhiz-media
    read only = yes
    guest ok = no
    valid users = @obdwhiz-readers
    force user = obdwhiz
```

Replace `obdwhiz` with the actual application account. Samba authenticates members of the reader group and reads files as that account through the read-only share. Verify with a staff account that browsing and downloading work and that writes and deletes are refused. On Windows, grant staff Read in Share permissions and Read & execute in Security permissions, with inheritance to child files. Keep the application account's Modify permission. SMB paths use internal UUIDs. The browser provides vehicle names, notes and search.
