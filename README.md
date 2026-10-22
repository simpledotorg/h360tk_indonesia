# HEARTS360 Indonesia Central Node (0.6.0)

This repository contains the central-node Compose configuration from
`simpledotorg/h360tk_central_dashboard` branch `0.6.0`, with the tested
Indonesia org-unit mapping, an atomic-import fix, and a National Details
dashboard update. It is **not** a database backup or an export of production
patient records. Leaf-node/vendor software lives in a different project.

## What is included

- Grafana and PostgreSQL central images at version `0.6.0`, an SFTP receiver,
  and a locally built importer layered on the pinned 0.6.0 importer image.
- `config/orgunit_mapping/mapping.yaml` for Semarang (Jawa Tengah), Gunung
  Kidul and Kulon Progo (Daerah Istimewa Yogyakarta).
- A provisioned program dashboard whose National Details tables respect
  National/location filters, display an available reporting month instead
  of `NaN`, and offer XLSX/CSV export.
- Guarded SQL migrations for Indonesia's existing hierarchy and Grafana's
  legacy PostgreSQL datasource metadata. **Never run these automatically on
  a fresh database or more than once on an upgraded database.**

No `.env`, SFTP account file, ZIP export, database, private key, or credentials
are distributed. This branch intentionally replaces the previous root
application in `h360tk_indonesia`; merging it does not deploy production.
The previous root files remain in Git history: administrators should audit
and rotate any credentials previously committed there.

## Existing central-node installation

1. Back up the PostgreSQL database and existing `.env`, Compose overrides,
   SFTP account file and upload area. Keep an encrypted off-host copy and
   verify a full restore in an isolated environment. Record source counts and
   dashboard metrics before changing services.
2. Copy `.env.example` to `.env` and fill it with **your existing** database,
   Grafana and SFTP credentials; do not commit it. Set `GF_DATABASE_URL` to
   the Grafana database role's URL (URL-encode its password). Copy
   `sftp_config/users.conf.example` to `sftp_config/users.conf` and replace
   the example account with the existing account definitions, retaining
   vendor mounts in your private Compose override. Restore the trusted SFTP
   server's `ssh_host_ed25519_key` and `ssh_host_rsa_key` under
   `sftp_config/host_keys/` before starting SFTP; these files are private
   and ignored by Git. Do not overwrite existing production keys or accounts
   with examples.
3. Confirm that `HEART360TK_VERSION=0.6.0`, PostgreSQL has
   `app.is_central_node=true`, the Indonesia mapping agrees with the
   existing org-unit IDs, and `IMPORT_AGGREGATE_DATA=true`. This Compose file
   is a portable baseline: compare and retain any production-specific
   volumes, SFTP host keys, network settings and monitoring configuration.
   PostgreSQL listens inside the Compose network only; if database access
   from outside Docker is required, authorize it separately rather than
   publishing port 5432 to all interfaces.
   The published PostgreSQL image initializes its own roles; `.env.example`
   is **not** a credential-rotation or fresh database initialization script.
4. Validate `docker compose config --quiet`. Build the patched importer with
   `docker compose build importer` in an isolated environment first. Do not
   point its first import at production until a complete frozen-ZIP rehearsal
   has succeeded and all pre/post-import aggregates match.
5. For an existing **0.5.1** database only: stop the importer, back up and
   restore-test the database, run the upstream `0.5.1_to_0.6.0.sql` migration
   once, then review/run
   `pg_init_scripts/migrations/indonesia_central_orgunit_0.6.0.sql` once.
   Apply `grafana_postgresql_datasources_0.6.0.sql` only when its guarded
   legacy-UID precondition holds. These SQL files require an already
   initialized database and administrator review; neither is mounted as
   an auto-run init script.
6. Recreate only the intended services after `docker compose config --quiet`.
   Confirm source-level import status, the same pre/post-upgrade figures,
   Grafana location filters, diabetes data, both XLSX downloads and the
   reference-month title before concluding an upgrade succeeded. Roll back
   the database **and** code/configuration/image together if a gate fails;
   never run a 0.5.1 importer against a 0.6.0 database.

The importer image patch protects the all-sources refresh against a failed
source and drops staging tables between sources. A paused source or malformed
ZIP blocks the full refresh rather than silently removing existing reporting
data. The production SFTP uploader configuration and patient ZIPs are not
recreated by this repository. The existing SFTP transport does not verify a
pinned host key; address that separately before relying on transport security.

## Validation

```text
python -m unittest discover -s tests -p test_dashboard_details.py -v
python -m unittest discover -s importer_scripts -p test_atomic_import.py -v
python -m unittest discover -s importer_scripts -p test_facility_conflicts.py -v
docker compose --env-file .env.example config --quiet
```

Importer tests require the dependencies pinned in the importer Docker base
image (`psycopg2`, `paramiko`, `APScheduler` and `PyYAML`) and
`PYTHONPATH=importer_scripts` when run outside that image. Test with the
actual production database only through read-only queries; no patient data
is necessary in this repository. Semarang's current export has hypertension
data but no diabetes reporting rows, so a zero for Semarang is not evidence
of a broken national diabetes aggregate. Cohort charts require longitudinal
data that is not currently available here.
