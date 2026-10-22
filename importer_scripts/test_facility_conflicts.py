import unittest

from import_versions.base import BaseImportVersion


class MappingCursor:
    def __init__(self, other_carries_data):
        self.other_carries_data = other_carries_data
        self.query = ''
        self.params = ()

    def execute(self, query, params):
        self.query = query
        self.params = params

    def fetchone(self):
        if 'SELECT name FROM' in self.query:
            return ('Shared facility',)
        incoming_carries_data = self.params[2]
        if ('(carries_data OR %s)' in self.query and
                (self.other_carries_data or incoming_carries_data)) or (
                'carries_data AND %s' in self.query and
                self.other_carries_data and incoming_carries_data):
            return ('other-source', 42)
        return None


class FacilityConflictTests(unittest.TestCase):
    def setUp(self):
        self.importer = BaseImportVersion()
        self.row = {'name': 'Shared facility', 'leaf_id': 10}

    def test_inactive_mapping_may_share_a_facility_with_active_source(self):
        cursor = MappingCursor(other_carries_data=False)
        self.importer._check_central_org_unit_free(
            cursor, 'new-source', self.row, 100, True, {}
        )

    def test_active_sources_cannot_share_a_facility(self):
        cursor = MappingCursor(other_carries_data=True)
        with self.assertRaisesRegex(ValueError, 'Mapping conflict'):
            self.importer._check_central_org_unit_free(
                cursor, 'new-source', self.row, 100, True, {}
            )

    def test_leaf_without_reporting_rows_is_not_data_bearing(self):
        self.assertFalse(self.importer._carries_reporting_data(10, set()))
        self.assertTrue(self.importer._carries_reporting_data(10, {10}))

    def test_two_data_bearing_units_in_same_source_cannot_share_facility(self):
        cursor = MappingCursor(other_carries_data=False)
        with self.assertRaisesRegex(ValueError, 'Mapping conflict'):
            self.importer._check_central_org_unit_free(
                cursor, 'new-source', self.row, 100, True,
                {100: (9, 'Existing facility', True)},
            )

    def test_inactive_unit_cannot_mask_later_active_mapping_in_same_source(self):
        cursor = MappingCursor(other_carries_data=False)
        used = {100: (9, 'Unused facility', False)}
        self.importer._check_central_org_unit_free(
            cursor, 'new-source', self.row, 100, True, used
        )
        self.importer._remember_org_unit(used, 100, self.row, True)
        with self.assertRaisesRegex(ValueError, 'Mapping conflict'):
            self.importer._check_central_org_unit_free(
                cursor, 'new-source', {'name': 'Third facility', 'leaf_id': 11},
                100, True, used,
            )


if __name__ == '__main__':
    unittest.main()
