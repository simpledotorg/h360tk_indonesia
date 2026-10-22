import csv
import io
import json
import logging
import os
import shutil
import stat
import sys
import tempfile
import time
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone

import paramiko
import psycopg2
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from import_versions import get_importer
from import_versions.base import BaseImportVersion
from orgunit_mapping import MappingRule, load_mapping_config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger(__name__)

# Suppress APScheduler's verbose job execution logs so the 5s poller doesn't flood stdout
apscheduler_logger = logging.getLogger('apscheduler')
apscheduler_logger.setLevel(logging.WARNING)
apscheduler_logger.propagate = False

apscheduler_exec_logger = logging.getLogger('apscheduler.executors.default')
apscheduler_exec_logger.setLevel(logging.WARNING)
apscheduler_exec_logger.propagate = False

apscheduler_sched_logger = logging.getLogger('apscheduler.scheduler')
apscheduler_sched_logger.setLevel(logging.WARNING)
apscheduler_sched_logger.propagate = False

# import_run_log.log_type values (enforced by a CHECK constraint).
LOG_TYPE_LEAF_NODE = 'leaf_node'
LOG_TYPE_INFRASTRUCTURE = 'infrastructure'

# import_run_log.source_key for infrastructure rows — names the failing layer.
SOURCE_SFTP = 'SFTP'
SOURCE_POSTGRES = 'Postgres'
SOURCE_MAPPING_FILE = 'Mapping file'
SOURCE_IMPORTER = 'Importer'


def _int_env(name: str, default: str) -> int | None:
    """Parse an integer env var; None when it is not a number (see validate_config)."""
    try:
        return int(os.getenv(name, default).strip())
    except ValueError:
        return None


IMPORT_CRON = os.getenv('IMPORT_CRON', '0 * * * *').strip()
IMPORT_AGGREGATE_DATA = os.getenv('IMPORT_AGGREGATE_DATA', 'false').strip().lower() == 'true'
IMPORT_PROTOCOL = os.getenv('IMPORT_PROTOCOL', 'sftp').strip().lower()
IMPORT_FOLDER_PATH = os.getenv('IMPORT_FOLDER_PATH', '/export').strip()

SFTP_HOST = os.getenv('SFTP_HOST', '').strip()
SFTP_PORT = _int_env('SFTP_PORT', '22')
SFTP_USER = os.getenv('SFTP_USER', '').strip()
SFTP_PASSWORD = os.getenv('SFTP_PASSWORD', '').strip()
SFTP_TIMEOUT_SECONDS = _int_env('SFTP_TIMEOUT', '60')

DB_CONNECTION_PARAMS = {
    'host': os.getenv('POSTGRES_HOST', 'postgres'),
    'database': os.getenv('POSTGRES_DB', 'heart360tk_database'),
    'user': os.getenv('POSTGRES_USER', 'heart360tk'),
    'password': os.getenv('POSTGRES_PASSWORD', ''),
}


def validate_config():
    required = {
        'POSTGRES_PASSWORD': DB_CONNECTION_PARAMS['password'],
    }

    if IMPORT_PROTOCOL == 'sftp':
        required.update({
            'SFTP_HOST': SFTP_HOST,
            'SFTP_USER': SFTP_USER,
            'SFTP_PASSWORD': SFTP_PASSWORD,
        })

    problems = []
    missing = [k for k, v in required.items() if not v]
    if missing:
        problems.append(f'Missing required environment variables: {", ".join(missing)}')
    not_numeric = [
        name
        for name, value in (('SFTP_PORT', SFTP_PORT), ('SFTP_TIMEOUT', SFTP_TIMEOUT_SECONDS))
        if value is None
    ]
    if not_numeric:
        problems.append(f'Environment variables must be integers: {", ".join(not_numeric)}')

    if problems:
        exit_with_importer_error('; '.join(problems), started_at=time.time())

    log.info('Config validated OK.')
    log.info('  IMPORT_CRON            : %s', IMPORT_CRON)
    log.info('  IMPORT_AGGREGATE_DATA : %s', IMPORT_AGGREGATE_DATA)
    log.info('  IMPORT_PROTOCOL        : %s', IMPORT_PROTOCOL)
    log.info('  IMPORT_FOLDER_PATH     : %s', IMPORT_FOLDER_PATH)
    if IMPORT_PROTOCOL == 'sftp':
        log.info('  SFTP_HOST              : %s', SFTP_HOST)
        log.info('  SFTP_PORT              : %d', SFTP_PORT)
        log.info('  SFTP_USER              : %s', SFTP_USER)
        log.info('  SFTP_TIMEOUT           : %ds', SFTP_TIMEOUT_SECONDS)


def is_import_enabled():
    if not IMPORT_AGGREGATE_DATA:
        log.info('IMPORT_AGGREGATE_DATA=false — import is disabled. Skipping.')
        return False
    return True


def get_paused_source_keys() -> set[str]:
    paused_keys = set()
    with psycopg2.connect(**DB_CONNECTION_PARAMS) as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT source_key FROM heart360tk_reporting.import_source_control WHERE is_paused = true')
            for row in cur.fetchall():
                paused_keys.add(row[0])
    return paused_keys


def log_import_run(
    source_key,
    started_at,
    status,
    log_type,
    duration_seconds=None,
    error_message=None,
):
    try:
        with psycopg2.connect(**DB_CONNECTION_PARAMS) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    '''
                    INSERT INTO heart360tk_reporting.import_run_log
                        (source_key, started_at, finished_at, status,
                         duration_seconds, error_message, log_type)
                    VALUES (%s, %s, NOW(), %s, %s, %s, %s)
                    ''',
                    (
                        source_key,
                        datetime.fromtimestamp(started_at, tz=timezone.utc),
                        status,
                        duration_seconds,
                        error_message,
                        log_type,
                    ),
                )
        log.info(
            '  Import run logged to DB — source_key=%s, log_type=%s, status=%s, '
            'duration=%.2fs',
            source_key,
            log_type,
            status,
            duration_seconds or 0,
        )
    except Exception as e:
        log.warning('Could not write import run log to DB (non-fatal): %s', e)

def update_import_source_control(source_key, data_status, review_note=None):
    try:
        with psycopg2.connect(**DB_CONNECTION_PARAMS) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                if review_note is not None:
                    cur.execute(
                        """
                        INSERT INTO heart360tk_reporting.import_source_control
                            (source_key, data_status, review_note)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (source_key) DO UPDATE SET
                            data_status = EXCLUDED.data_status,
                            review_note = EXCLUDED.review_note
                        """,
                        (source_key, data_status, review_note),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO heart360tk_reporting.import_source_control
                            (source_key, data_status)
                        VALUES (%s, %s)
                        ON CONFLICT (source_key) DO UPDATE SET
                            data_status = EXCLUDED.data_status
                        """,
                        (source_key, data_status),
                    )
        log.info(
            "Import source status updated — source_key=%s, data_status=%s, review_note=%s",
            source_key, data_status, review_note
        )
    except Exception as e:
        log.warning("Could not update import source status in DB (non-fatal): %s", e)

def set_global_review_note(review_note: str):
    try:
        with psycopg2.connect(**DB_CONNECTION_PARAMS) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE heart360tk_reporting.import_source_control SET review_note = %s",
                    (review_note,)
                )
    except Exception as e:
        log.warning("Could not set global review note: %s", e)

def _log_import_failure(source_key: str, log_type: str, started_at: float, error) -> None:
    log_import_run(
        source_key=source_key,
        started_at=started_at,
        status='failed',
        log_type=log_type,
        duration_seconds=round(time.time() - started_at, 2),
        error_message=str(error),
    )


def log_leaf_node_failure(source_key: str, started_at: float, error) -> None:
    """Record a failure in one leaf node's data under that node's source_key."""
    _log_import_failure(source_key, LOG_TYPE_LEAF_NODE, started_at, error)


def log_infrastructure_failure(layer: str, started_at: float, error) -> None:
    """Record a failure not tied to a leaf node, e.g. layer='SFTP'."""
    _log_import_failure(layer, LOG_TYPE_INFRASTRUCTURE, started_at, error)


def exit_with_importer_error(error, started_at: float) -> None:
    """Log a startup configuration failure and stop the container."""
    log.error('Importer configuration error: %s', error)
    log_infrastructure_failure(SOURCE_IMPORTER, started_at, error)
    sys.exit(1)


class InfrastructureError(Exception):
    """A job-level failure tagged with the infrastructure layer that raised it."""

    def __init__(self, layer: str, cause: Exception):
        super().__init__(str(cause))
        self.layer = layer


@contextmanager
def infrastructure_layer(layer: str):
    """Attribute any exception raised inside the block to ``layer``."""
    try:
        yield
    except InfrastructureError:
        raise
    except Exception as e:
        raise InfrastructureError(layer, e) from e


def update_schedule_status(
    next_run_at=None,
    last_run_started_at=None,
    last_run_finished_at=None,
):
    try:
        with psycopg2.connect(**DB_CONNECTION_PARAMS) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    '''
                    INSERT INTO heart360tk_reporting.import_schedule_status
                        (id, cron_expression, next_run_at,
                         last_run_started_at, last_run_finished_at, updated_at)
                    VALUES (1, %s, %s, %s, %s, now())
                    ON CONFLICT (id) DO UPDATE SET
                        cron_expression = EXCLUDED.cron_expression,
                        next_run_at = COALESCE(
                            EXCLUDED.next_run_at,
                            heart360tk_reporting.import_schedule_status.next_run_at
                        ),
                        last_run_started_at = COALESCE(
                            EXCLUDED.last_run_started_at,
                            heart360tk_reporting.import_schedule_status.last_run_started_at
                        ),
                        last_run_finished_at = COALESCE(
                            EXCLUDED.last_run_finished_at,
                            heart360tk_reporting.import_schedule_status.last_run_finished_at
                        ),
                        updated_at = now()
                    ''',
                    (
                        IMPORT_CRON,
                        next_run_at,
                        last_run_started_at,
                        last_run_finished_at,
                    ),
                )
        log.info('  Schedule status updated — next_run_at=%s', next_run_at)
    except Exception as e:
        log.warning('Could not write import schedule status to DB (non-fatal): %s', e)


def _open_sftp_client():
    log.warning(
        'SFTP host key verification is disabled — '
        'set a known_hosts file in production for security.'
    )

    transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
    transport.banner_timeout = SFTP_TIMEOUT_SECONDS
    transport.auth_timeout = SFTP_TIMEOUT_SECONDS
    transport.connect(username=SFTP_USER, password=SFTP_PASSWORD)

    sftp = paramiko.SFTPClient.from_transport(transport)
    sftp.get_channel().settimeout(SFTP_TIMEOUT_SECONDS)
    return transport, sftp


def _is_import_zip(filename: str) -> bool:
    return (
        filename.lower().endswith('.zip')
        and not filename.startswith('.')
        and not filename.endswith('.tmp')
    )


def fetch_sftp_zip_names() -> list[str]:
    remote_dir = IMPORT_FOLDER_PATH.rstrip('/')
    transport = None
    sftp = None

    try:
        transport, sftp = _open_sftp_client()
        entries = sftp.listdir_attr(remote_dir)
        zip_names = sorted(
            entry.filename
            for entry in entries
            if not stat.S_ISDIR(entry.st_mode) and _is_import_zip(entry.filename)
        )
        log.info(
            'Found %d zip file(s) in %s@%s:%s',
            len(zip_names),
            SFTP_USER,
            SFTP_HOST,
            remote_dir,
        )
        for name in zip_names:
            log.info('  %s', name)
        return zip_names

    finally:
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass
        if transport is not None:
            transport.close()


def download_sftp_zip(zip_name: str, local_zip_path: str) -> None:
    remote_dir = IMPORT_FOLDER_PATH.rstrip('/')
    remote_path = f'{remote_dir}/{zip_name}'
    transport = None
    sftp = None

    try:
        transport, sftp = _open_sftp_client()
        sftp.get(remote_path, local_zip_path)
        log.info('  Downloaded %s', zip_name)
    finally:
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass
        if transport is not None:
            transport.close()


_SFTP_RETRY_MAX_ATTEMPTS = 3
_SFTP_RETRY_BACKOFF_SECONDS = [0, 5, 10]


def download_sftp_zip_with_retry(zip_name: str, local_zip_path: str) -> None:
    last_exc: Exception | None = None
    for attempt in range(1, _SFTP_RETRY_MAX_ATTEMPTS + 1):
        delay = _SFTP_RETRY_BACKOFF_SECONDS[attempt - 1]
        if delay:
            log.warning(
                '  Retrying download of %s (attempt %d/%d) in %ds...',
                zip_name, attempt, _SFTP_RETRY_MAX_ATTEMPTS, delay,
            )
            time.sleep(delay)
        try:
            download_sftp_zip(zip_name, local_zip_path)
            return
        except Exception as e:
            last_exc = e
            log.warning(
                '  Download attempt %d/%d failed for %s: %s',
                attempt, _SFTP_RETRY_MAX_ATTEMPTS, zip_name, e,
            )
    assert last_exc is not None
    raise last_exc


def load_metadata(extract_dir: str) -> dict:
    metadata_path = os.path.join(extract_dir, 'metadata.json')
    if not os.path.isfile(metadata_path):
        raise FileNotFoundError('metadata.json not found in zip')

    with open(metadata_path, 'r', encoding='utf-8') as metadata_file:
        metadata = json.load(metadata_file)

    source_key = metadata.get('source_key', '').strip()
    if not source_key:
        raise ValueError('metadata.json is missing source_key')

    version = metadata.get('import_export_version')
    if version is None:
        raise ValueError('metadata.json is missing import_export_version')

    return metadata


def read_zip_source_key(zip_path: str) -> str:
    with zipfile.ZipFile(zip_path, 'r') as zf:
        with zf.open('metadata.json') as f:
            metadata = json.load(f)
    source_key = metadata.get('source_key', '').strip()
    if not source_key:
        raise ValueError('metadata.json is missing source_key')
    return source_key


def zip_source_key_or_name(zip_path: str, zip_name: str) -> str:
    """The zip's source_key, or its file name when metadata.json is unreadable."""
    try:
        return read_zip_source_key(zip_path)
    except Exception:
        return os.path.splitext(zip_name)[0]


def validate_zip(zip_path: str, zip_name: str) -> tuple[bool, str]:
    """Return (True, '') when the ZIP is safe to import, (False, reason) otherwise.

    A ZIP is considered valid when:
      1. It is a well-formed ZIP archive.
      2. metadata.json is present with a non-empty source_key and a recognised
         import_export_version.
      3. At least one CSV file inside the archive contains at least one data
         row (beyond the header line).
    """
    if not zipfile.is_zipfile(zip_path):
        return False, 'not a valid ZIP archive'

    with zipfile.ZipFile(zip_path, 'r') as zf:
        names_in_zip = zf.namelist()

        if 'metadata.json' not in names_in_zip:
            return False, 'metadata.json is missing'

        try:
            with zf.open('metadata.json') as f:
                metadata = json.load(f)
        except Exception as exc:
            return False, f'metadata.json could not be parsed: {exc}'

        source_key = metadata.get('source_key', '').strip()
        if not source_key:
            return False, 'metadata.json is missing source_key'

        if metadata.get('import_export_version') is None:
            return False, 'metadata.json is missing import_export_version'

        csv_files = [n for n in names_in_zip if n.lower().endswith('.csv')]
        has_data = False
        for csv_name in csv_files:
            with zf.open(csv_name) as raw:
                reader = csv.reader(io.TextIOWrapper(raw, encoding='utf-8'))
                try:
                    next(reader)  # header row
                    next(reader)  # first data row — proves file is non-empty
                    has_data = True
                    break
                except StopIteration:
                    continue

        if not has_data:
            return False, 'no CSV files contain any data rows'

    return True, ''


def import_zip_file(
    conn,
    zip_path: str,
    source_key: str,
    mapping_config: dict[str, list[MappingRule]] | None = None,
) -> None:
    extract_dir = tempfile.mkdtemp(prefix='h360tk_import_')
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(extract_dir)

        metadata = load_metadata(extract_dir)
        version = int(metadata['import_export_version'])

        mapping_rules = (mapping_config or {}).get(source_key)
        if mapping_config and mapping_rules is None:
            log.info(
                '  No org unit mapping block for source_key=%s — '
                'importing its org units unmapped.',
                source_key,
            )

        importer = get_importer(version)
        importer.import_zip(conn, extract_dir, metadata, mapping_rules=mapping_rules)

    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)


def run_import():
    if not is_import_enabled():
        return

    log.info('=== Import job started ===')
    job_start = time.time()
    work_dir = tempfile.mkdtemp(prefix='h360tk_import_work_')
    conn = None
    failed_source = None

    try:
        if IMPORT_PROTOCOL != 'sftp':
            raise ValueError(f'Unsupported IMPORT_PROTOCOL: {IMPORT_PROTOCOL}')

        # Read up front so a broken mapping file stops the job before anything
        # is downloaded or truncated.
        mapping_config = {}
        mapping_is_empty = True
        try:
            with infrastructure_layer(SOURCE_MAPPING_FILE):
                mapping_config = load_mapping_config()
                mapping_is_empty = len(mapping_config) == 0
        except Exception as e:
            from orgunit_mapping import OrgUnitMappingError
            if isinstance(e, OrgUnitMappingError) or (hasattr(e, 'layer') and isinstance(e.__cause__, OrgUnitMappingError)):
                set_global_review_note('Invalid mapping rule')
            raise

        with infrastructure_layer(SOURCE_POSTGRES):
            paused_keys = get_paused_source_keys()
        if paused_keys:
            raise ValueError(
                'Full refresh cannot run while sources are paused: truncating would '
                f'delete their data ({", ".join(sorted(paused_keys))}).'
            )

        with infrastructure_layer(SOURCE_SFTP):
            zip_names = fetch_sftp_zip_names()
        if not zip_names:
            log.info(
                'No zip files found at %s on SFTP server — nothing to import.',
                IMPORT_FOLDER_PATH,
            )
            return

        log.info('Phase 1 — Downloading %d zip file(s)...', len(zip_names))
        downloaded: list[tuple[str, str]] = []
        for zip_name in zip_names:
            local_zip_path = os.path.join(work_dir, zip_name)
            download_start = time.time()
            try:
                download_sftp_zip_with_retry(zip_name, local_zip_path)
                downloaded.append((zip_name, local_zip_path))
            except Exception as e:
                log.error(
                    'Failed to download %s after %d attempts — skipping: %s',
                    zip_name, _SFTP_RETRY_MAX_ATTEMPTS, e,
                )
                log_infrastructure_failure(
                    SOURCE_SFTP, download_start, f'Failed to download {zip_name}: {e}'
                )

        if not downloaded:
            log.error('All downloads failed — reporting tables will NOT be truncated.')
            return
        if len(downloaded) != len(zip_names):
            log.error('Some downloads failed — aborting full refresh without changing reporting data.')
            return

        log.info(
            'Phase 1 complete — %d/%d zip file(s) downloaded successfully.',
            len(downloaded),
            len(zip_names),
        )

        log.info('Phase 2 — Validating %d downloaded zip file(s)...', len(downloaded))
        valid_zips: list[tuple[str, str]] = []
        for zip_name, local_zip_path in downloaded:
            validate_start = time.time()
            ok, reason = validate_zip(local_zip_path, zip_name)
            if ok:
                log.info('  [VALID]   %s', zip_name)
                valid_zips.append((zip_name, local_zip_path))
            else:
                log.warning('  [SKIPPED] %s — %s', zip_name, reason)
                log_leaf_node_failure(
                    zip_source_key_or_name(local_zip_path, zip_name),
                    validate_start,
                    f'Validation failed for {zip_name}: {reason}',
                )

        if not valid_zips:
            log.error(
                'No zip files passed validation — '
                'reporting tables will NOT be truncated to avoid data loss.'
            )
            return
        if len(valid_zips) != len(downloaded):
            log.error('Some ZIPs failed validation — aborting full refresh without changing reporting data.')
            return

        log.info(
            'Phase 2 complete — %d/%d zip file(s) passed validation.',  # noqa: E501
            len(valid_zips),
            len(downloaded),
        )

        log.info(
            'Phase 3 — Truncating reporting tables and importing %d zip file(s)...',
            len(valid_zips),
        )
        with infrastructure_layer(SOURCE_POSTGRES):
            conn = psycopg2.connect(**DB_CONNECTION_PARAMS)
            conn.autocommit = False
            BaseImportVersion.truncate_reporting_tables(conn)

        completed = []
        for zip_name, local_zip_path in valid_zips:
            zip_start = time.time()
            source_key = None

            try:
                source_key = read_zip_source_key(local_zip_path)

                if source_key in paused_keys:
                    raise ValueError(f'Source {source_key} is paused; full refresh aborted')

                import_zip_file(conn, local_zip_path, source_key, mapping_config)
                completed.append((zip_name, source_key, zip_start))

            except Exception as e:
                failed_source = source_key or zip_source_key_or_name(local_zip_path, zip_name)
                log.error(
                    'Failed to import zip %s: %s',
                    zip_name,
                    e,
                    exc_info=True,
                )
                log_leaf_node_failure(
                    failed_source,
                    zip_start,
                    e,
                )
                raise

        conn.commit()
        for zip_name, source_key, zip_start in completed:
            duration = round(time.time() - zip_start, 2)
            log_import_run(
                source_key=source_key,
                started_at=zip_start,
                status='success',
                log_type=LOG_TYPE_LEAF_NODE,
                duration_seconds=duration,
            )
            if mapping_is_empty:
                review_note = 'Unmapped by design'
            elif source_key in mapping_config:
                review_note = 'Mapped'
            else:
                review_note = 'Missing central mapping'
            update_import_source_control(source_key, 'data_loaded', review_note)
            log.info('  Imported %s (source_key=%s) in %.2fs', zip_name, source_key, duration)
        duration = round(time.time() - job_start, 2)
        log.info('=== Import job complete — %d/%d zip(s) imported in %.2fs ===',
                 len(completed), len(valid_zips), duration)

    except Exception as e:
        if conn is not None:
            conn.rollback()
        if failed_source is not None:
            log.error('Full refresh aborted after failure in %s; previous data preserved.', failed_source)
            return
        layer = e.layer if isinstance(e, InfrastructureError) else SOURCE_IMPORTER
        log.error('Import job failed (source_key=%s): %s', layer, e, exc_info=True)
        log_infrastructure_failure(layer, job_start, e)
    finally:
        if conn is not None:
            conn.close()
        shutil.rmtree(work_dir, ignore_errors=True)


def _get_next_run_time(scheduler):
    job = scheduler.get_job('import_job')
    if job is None:
        return None
    try:
        return job.trigger.get_next_fire_time(None, datetime.now(timezone.utc))
    except Exception:
        # Absolute last-resort: attribute may exist post-start
        return getattr(job, 'next_run_time', None)


def scheduled_import_job(scheduler):
    last_run_started_at = datetime.now(timezone.utc)
    job_start = time.time()
    try:
        run_import()
    except Exception as e:
        log.error('Unexpected import failure (source_key=%s): %s', SOURCE_IMPORTER, e)
        log_infrastructure_failure(SOURCE_IMPORTER, job_start, e)
    finally:
        last_run_finished_at = datetime.now(timezone.utc)
        update_schedule_status(
            next_run_at=_get_next_run_time(scheduler),
            last_run_started_at=last_run_started_at,
            last_run_finished_at=last_run_finished_at,
        )


def poll_for_force_import(scheduler):
    try:
        requested = False
        with psycopg2.connect(**DB_CONNECTION_PARAMS) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute('SELECT force_import_requested FROM heart360tk_reporting.import_schedule_status WHERE id = 1')
                row = cur.fetchone()
                if row and row[0]:
                    requested = True
                    # Set it to false immediately so we don't double-trigger
                    cur.execute('UPDATE heart360tk_reporting.import_schedule_status SET force_import_requested = false WHERE id = 1')

        if requested:
            log.info('Force import requested via dashboard. Triggering import now...')
            import_job = scheduler.get_job('import_job')
            if import_job:
                # Modifying the next_run_time forces APScheduler to run the existing job immediately.
                # Because max_instances=1 on import_job, this natively prevents it from running
                # concurrently with a scheduled cron execution!
                import_job.modify(next_run_time=datetime.now(timezone.utc))

    except Exception as e:
        log.warning('Error polling for force import (non-fatal): %s', e)


def start_scheduler():
    try:
        trigger = CronTrigger.from_crontab(IMPORT_CRON)
    except Exception as e:
        exit_with_importer_error(
            f"Invalid IMPORT_CRON expression '{IMPORT_CRON}': {e}", started_at=time.time()
        )

    scheduler = BlockingScheduler()
    scheduler.add_job(
        lambda: scheduled_import_job(scheduler),
        trigger,
        id='import_job',
        name='h360tk import',
        max_instances=1,
    )

    # Fast polling job for manual force imports
    scheduler.add_job(
        lambda: poll_for_force_import(scheduler),
        'interval',
        seconds=5,
        id='poll_force_import',
        name='h360tk force import poller',
        max_instances=1,
    )

    log.info("Scheduler started. Import will run on cron: '%s'", IMPORT_CRON)
    update_schedule_status(next_run_at=_get_next_run_time(scheduler))

    try:
        scheduler.start()
    except KeyboardInterrupt:
        log.info('Importer stopped.')
        scheduler.shutdown()


if __name__ == '__main__':
    validate_config()

    if not is_import_enabled():
        log.info('Importer is disabled (IMPORT_AGGREGATE_DATA=false). Container will exit.')
        sys.exit(0)

    start_scheduler()
