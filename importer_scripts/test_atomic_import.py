import os
import unittest
from unittest.mock import patch

import importer


class RecordingConnection:
    def __init__(self):
        self.committed = ['existing patient']
        self.pending = list(self.committed)
        self.autocommit = False

    def commit(self):
        self.committed = list(self.pending)

    def rollback(self):
        self.pending = list(self.committed)

    def close(self):
        pass


class AtomicImportTests(unittest.TestCase):
    def run_batch(self, *, invalid_zip=False, fail_second=False, fail_download=False, paused=False):
        connection = RecordingConnection()
        statuses = []
        logs = []

        def import_zip(conn, path, source_key, mapping):
            if source_key == 'second' and fail_second:
                raise ValueError('mapping conflict')
            conn.pending.append(source_key)

        def validate(path, name):
            if invalid_zip and name == 'second.zip':
                return False, 'invalid rows'
            return True, ''

        def download_zip(name, path):
            if fail_download and name == 'second.zip':
                raise OSError('download failed')

        with (
            patch.object(importer, 'is_import_enabled', return_value=True),
            patch.object(importer, 'load_mapping_config', return_value={}),
            patch.object(importer, 'get_paused_source_keys', return_value={'first'} if paused else set()),
            patch.object(importer, 'fetch_sftp_zip_names', return_value=['first.zip', 'second.zip']),
            patch.object(importer, 'download_sftp_zip_with_retry', side_effect=download_zip),
            patch.object(importer, 'validate_zip', side_effect=validate),
            patch.object(importer, 'read_zip_source_key', side_effect=lambda path: os.path.basename(path).split('.')[0]),
            patch.object(importer, 'zip_source_key_or_name', side_effect=lambda path, name: name.split('.')[0]),
            patch.object(importer, 'import_zip_file', side_effect=import_zip),
            patch.object(importer.BaseImportVersion, 'truncate_reporting_tables', side_effect=lambda conn: conn.pending.clear()),
            patch.object(importer.psycopg2, 'connect', return_value=connection),
            patch.object(importer, 'log_import_run', side_effect=lambda **kw: logs.append(kw)),
            patch.object(importer, 'log_leaf_node_failure'),
            patch.object(importer, 'log_infrastructure_failure'),
            patch.object(importer, 'update_import_source_control', side_effect=lambda *a: statuses.append(a)),
        ):
            importer.run_import()
        return connection.committed, statuses, logs

    def test_mapping_failure_rolls_back_entire_batch_and_never_marks_success(self):
        committed, statuses, logs = self.run_batch(fail_second=True)
        self.assertEqual(['existing patient'], committed)
        self.assertFalse(any(row['status'] == 'success' for row in logs))
        self.assertFalse(any(row[1] == 'data_loaded' for row in statuses))

    def test_invalid_zip_preserves_previous_reporting_data(self):
        committed, statuses, logs = self.run_batch(invalid_zip=True)
        self.assertEqual(['existing patient'], committed)
        self.assertFalse(any(row['status'] == 'success' for row in logs))
        self.assertFalse(any(row[1] == 'data_loaded' for row in statuses))

    def test_successful_batch_commits_all_sources(self):
        committed, statuses, logs = self.run_batch()
        self.assertEqual(['first', 'second'], committed)
        self.assertEqual(['first', 'second'], [row['source_key'] for row in logs if row['status'] == 'success'])
        self.assertEqual(2, sum(row[1] == 'data_loaded' for row in statuses))

    def test_download_failure_preserves_previous_reporting_data(self):
        committed, _, logs = self.run_batch(fail_download=True)
        self.assertEqual(['existing patient'], committed)
        self.assertFalse(any(row['status'] == 'success' for row in logs))

    def test_paused_source_is_not_deleted_by_full_refresh(self):
        committed, _, logs = self.run_batch(paused=True)
        self.assertEqual(['existing patient'], committed)
        self.assertFalse(any(row['status'] == 'success' for row in logs))


if __name__ == '__main__':
    unittest.main()
