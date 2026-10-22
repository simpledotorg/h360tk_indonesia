from import_versions.base import BaseImportVersion
from import_versions.v1 import ImportVersion1

VERSION_REGISTRY: dict[int, type[BaseImportVersion]] = {
    1: ImportVersion1,
}


def get_importer(version: int) -> BaseImportVersion:
    importer_cls = VERSION_REGISTRY.get(version)
    if importer_cls is None:
        raise ValueError(f'Unsupported import_export_version: {version}')
    return importer_cls()


def all_reporting_tables() -> list[str]:
    tables: set[str] = set()
    for importer_cls in VERSION_REGISTRY.values():
        tables.update(importer_cls.REPORTING_TABLES)
    return sorted(tables)
