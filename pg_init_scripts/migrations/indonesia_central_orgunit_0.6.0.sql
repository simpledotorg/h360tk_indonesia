BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM heart360tk_schema.org_units
        WHERE name IN ('Jawa Tengah', 'Kota Semarang')
    ) OR NOT EXISTS (
        SELECT 1 FROM heart360tk_schema.org_units
        WHERE id = 110360 AND name = 'Daerah Istimewa Yogyakarta'
          AND level = 1 AND parent_id IS NULL
    ) OR (
        SELECT count(*) FROM heart360tk_schema.org_units
        WHERE (id = 1 AND name = 'KABUPATEN GUNUNG KIDUL'
                   AND level = 1 AND parent_id IS NULL)
           OR (id = 687448 AND name = 'KAB. KULON PROGO'
                   AND level = 1 AND parent_id IS NULL)
           OR (id = 960513 AND name = 'Demo Region'
                   AND level = 1 AND parent_id IS NULL)
    ) <> 3 THEN
        RAISE EXCEPTION 'Unexpected central location hierarchy; no changes applied';
    END IF;
END $$;

INSERT INTO heart360tk_schema.org_units (name, level, parent_id)
VALUES ('Jawa Tengah', 1, NULL);

WITH RECURSIVE moved AS (
    SELECT id, id AS root_id FROM heart360tk_schema.org_units
    WHERE id IN (1, 687448, 960513)
    UNION ALL
    SELECT child.id, moved.root_id
    FROM heart360tk_schema.org_units child
    JOIN moved ON child.parent_id = moved.id
)
UPDATE heart360tk_schema.org_units AS ou
SET level = ou.level + 1,
    parent_id = CASE
        WHEN ou.id IN (1, 687448) THEN 110360
        WHEN ou.id = 960513 THEN (
            SELECT id FROM heart360tk_schema.org_units
            WHERE name = 'Jawa Tengah' AND level = 1 AND parent_id IS NULL
        )
        ELSE ou.parent_id
    END,
    name = CASE WHEN ou.id = 960513 THEN 'Kota Semarang' ELSE ou.name END
FROM moved WHERE ou.id = moved.id;

DO $$
BEGIN
    IF (SELECT count(*) FROM heart360tk_schema.org_units
        WHERE id IN (1, 687448) AND parent_id = 110360 AND level = 2) <> 2
       OR NOT EXISTS (
           SELECT 1 FROM heart360tk_schema.org_units sem
           JOIN heart360tk_schema.org_units jateng ON jateng.id = sem.parent_id
           WHERE sem.id = 960513 AND sem.name = 'Kota Semarang'
             AND sem.level = 2 AND jateng.name = 'Jawa Tengah'
             AND jateng.level = 1
       ) THEN
        RAISE EXCEPTION 'Location hierarchy migration failed verification';
    END IF;
END $$;

COMMIT;
