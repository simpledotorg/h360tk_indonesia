BEGIN;

DO $$
BEGIN
    IF (
        SELECT count(*) FROM heart360tk_schema.data_source
        WHERE uid IN (
            'postgres-grafana',
            'postgres-hypertension',
            'postgres-hypertension-cached'
        ) AND type = 'postgres' AND version = 5
    ) <> 3 THEN
        RAISE EXCEPTION 'Unexpected Grafana PostgreSQL datasource state; no changes applied';
    END IF;
END $$;

UPDATE heart360tk_schema.data_source
SET type = 'grafana-postgresql-datasource', version = 6
WHERE uid IN (
    'postgres-grafana',
    'postgres-hypertension',
    'postgres-hypertension-cached'
) AND type = 'postgres' AND version = 5;

DO $$
BEGIN
    IF (
        SELECT count(*) FROM heart360tk_schema.data_source
        WHERE uid IN (
            'postgres-grafana',
            'postgres-hypertension',
            'postgres-hypertension-cached'
        ) AND type = 'grafana-postgresql-datasource' AND version = 6
    ) <> 3 THEN
        RAISE EXCEPTION 'Grafana PostgreSQL datasource migration failed verification';
    END IF;
END $$;

COMMIT;
