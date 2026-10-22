import logging
import os

from import_versions.base import BaseImportVersion
from orgunit_mapping import MappingRule

log = logging.getLogger(__name__)


class ImportVersion1(BaseImportVersion):
    REPORTING_TABLES = [
        'heart360_patients_category',
        'heart360_patients_under_care',
        'heart360_patients_registered',
        'heart360_blood_sugar_controlled',
        'heart360_blood_sugar_severity',
        'heart360_blood_sugar_missed_visits',
        'heart360_dm_bp_control',
        'heart360_dm_patients_under_care',
        'heart360_overdue_start_of_month',
        'heart360_overdue_patients_called',
        'heart360_overdue_returned_to_care',
        'heart360_dm_patients_catagory',
    ]

    def import_zip(
        self,
        conn,
        extract_dir: str,
        metadata: dict,
        mapping_rules: list[MappingRule] | None = None,
    ) -> None:
        source_key = metadata.get('source_key', '')
        log.info(
            'Importing zip for source_key=%s (import_export_version=1)',
            source_key,
        )

        self.import_org_units(
            conn, extract_dir, source_key, metadata, mapping_rules=mapping_rules
        )

        for filename in sorted(os.listdir(extract_dir)):
            if filename in self.SKIP_FILES:
                continue
            if not filename.lower().endswith('.csv'):
                log.debug('  Skipping non-CSV file: %s', filename)
                continue

            table_name = self.csv_to_table_name(filename)
            if table_name is None:
                log.warning('  Skipping unknown CSV: %s', filename)
                continue

            self.copy_csv_to_table(
                conn,
                table_name,
                os.path.join(extract_dir, filename),
                source_key,
            )
