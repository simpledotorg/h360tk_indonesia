BEGIN;

SET ROLE heart360tk;
SET search_path TO heart360tk_schema;

-- 1. Update upsert_org_unit to use explicit schema
CREATE OR REPLACE FUNCTION heart360tk_schema.upsert_org_unit(p_name VARCHAR, p_level INTEGER, p_parent_id INTEGER)
RETURNS INTEGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_id INTEGER;
BEGIN
    IF p_parent_id IS NULL THEN
        INSERT INTO heart360tk_schema.org_units (name, level, parent_id)
        VALUES (p_name, p_level, NULL)
        ON CONFLICT (name, level) WHERE parent_id IS NULL
        DO NOTHING;

        SELECT ou.id INTO v_id FROM heart360tk_schema.org_units ou
        WHERE ou.name = p_name AND ou.level = p_level AND ou.parent_id IS NULL;
    ELSE
        INSERT INTO heart360tk_schema.org_units (name, level, parent_id)
        VALUES (p_name, p_level, p_parent_id)
        ON CONFLICT (name, level, parent_id) WHERE parent_id IS NOT NULL
        DO NOTHING;

        SELECT ou.id INTO v_id FROM heart360tk_schema.org_units ou
        WHERE ou.name = p_name AND ou.level = p_level AND ou.parent_id = p_parent_id;
    END IF;

    RETURN v_id;
END;
$$;

-- 2. Update upsert_org_unit_chain to use explicit schema function calls
CREATE OR REPLACE FUNCTION heart360tk_schema.upsert_org_unit_chain(p_names VARCHAR[], p_levels INTEGER[])
RETURNS INTEGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_parent_id INTEGER := NULL;
    v_id INTEGER;
    i INTEGER;
BEGIN
    FOR i IN 1..array_length(p_names, 1) LOOP
        v_id := heart360tk_schema.upsert_org_unit(p_names[i], p_levels[i], v_parent_id);
        v_parent_id := v_id;
    END LOOP;
    RETURN v_id;
END;
$$;

-- 3. Add log_type to export_run_log
ALTER TABLE heart360tk_reporting.export_run_log
ADD COLUMN IF NOT EXISTS log_type TEXT NOT NULL DEFAULT 'leaf_node' CHECK (log_type IN ('leaf_node', 'infrastructure'));

-- 4. Add log_type to import_run_log
ALTER TABLE heart360tk_reporting.import_run_log
ADD COLUMN IF NOT EXISTS log_type TEXT NOT NULL DEFAULT 'leaf_node' CHECK (log_type IN ('leaf_node', 'infrastructure'));

-- 5. Create import_source_control table
CREATE TABLE IF NOT EXISTS heart360tk_reporting.import_source_control (
    source_key        TEXT        NOT NULL,
    data_status       TEXT        NOT NULL DEFAULT 'no_data'  CHECK (data_status IN ('no_data', 'data_loaded')),
    is_paused         BOOLEAN     NOT NULL DEFAULT FALSE,
    paused_changed_at TIMESTAMPTZ,
    last_purged_at    TIMESTAMPTZ,
    review_note       TEXT,

    CONSTRAINT uq_import_source_control_source_key UNIQUE (source_key)
);

-- 6. Create import_schedule_status table
CREATE TABLE IF NOT EXISTS heart360tk_reporting.import_schedule_status (
    id                     INTEGER     PRIMARY KEY DEFAULT 1,
    cron_expression        TEXT,
    next_run_at            TIMESTAMPTZ,
    last_run_started_at    TIMESTAMPTZ,
    last_run_finished_at   TIMESTAMPTZ,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    force_import_requested BOOLEAN     DEFAULT false,
    CONSTRAINT import_schedule_status_singleton CHECK (id = 1)
);

GRANT INSERT, UPDATE, SELECT ON heart360tk_reporting.import_schedule_status TO heart360tk;

-- 6b. Flag leaf org units that carry numbers; the importer refuses to put two of
--     them on one central org unit
ALTER TABLE heart360tk_reporting.import_facility_mapping
ADD COLUMN IF NOT EXISTS carries_data boolean NOT NULL DEFAULT false;

-- 7. Create delete_leaf_node_data function
CREATE OR REPLACE FUNCTION heart360tk_reporting.delete_leaf_node_data(
    p_source_keys text[],
    p_delete_hierarchy boolean DEFAULT false,
    p_purge_everything boolean DEFAULT false
)
RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    v_table_name text;
    v_central_org_unit_ids integer[];
    v_hierarchy_org_unit_ids integer[];
    v_valid_leaf_nodes text[] := ARRAY[]::text[];
    v_failed_leaf_nodes text[] := ARRAY[]::text[];
    v_error_message text;
    v_org_unit_id integer;

    v_report_tables text[] := ARRAY[
        'heart360_patients_category',
        'heart360_patients_under_care',
        'heart360_patients_registered',
        'heart360_blood_sugar_controlled',
        'heart360_blood_sugar_severity',
        'heart360_blood_sugar_missed_visits',
        'heart360_dm_bp_control',
        'heart360_dm_patients_under_care',
        'heart360_overdue_patients',
        'heart360_overdue_start_of_month',
        'heart360_overdue_patients_called',
        'heart360_overdue_returned_to_care',
        'heart360_cohort_patient_details',
        'heart360_dm_patients_catagory'
    ];

BEGIN
    -- delete everything if requested
    IF p_purge_everything THEN
        BEGIN
            -- 1. Delete all reporting data
            FOREACH v_table_name IN ARRAY v_report_tables
            LOOP
                EXECUTE format( 'TRUNCATE TABLE heart360tk_reporting.%I', v_table_name );
            END LOOP;

            -- 2. Delete all leaf-node mappings
            TRUNCATE TABLE heart360tk_reporting.import_facility_mapping;

            -- 3. Delete complete hierarchy
            TRUNCATE TABLE heart360tk_schema.org_units CASCADE;

            -- 4. Reset hierarchy sequence
            ALTER SEQUENCE heart360tk_schema.org_units_id_seq RESTART WITH 1;

            -- 5. Reset source status
            UPDATE heart360tk_reporting.import_source_control SET data_status = 'no_data', last_purged_at = NOW();

            RETURN jsonb_build_object(
                'success', true,
                'purge_everything', true,
                'message', 'All reporting data, facility mappings and hierarchy data purged successfully',
                'failed_count', 0
            );

        EXCEPTION WHEN OTHERS THEN
            RETURN jsonb_build_object(
                'success', false,
                'purge_everything', true,
                'message', 'Failed to purge all data: ' || SQLERRM,
                'failed_count', 0
            );
        END;
    END IF;

    -- At least one leaf node is required.
    IF p_source_keys IS NULL OR cardinality(p_source_keys) = 0 THEN
        RETURN jsonb_build_object(
            'success', false,
            'message', 'At least one leaf node is required',
            'failed_count', 0
        );
    END IF;

    -- 1. Identify valid (mapped) and invalid (unmapped) leaf nodes in bulk
    WITH mapped AS (
        SELECT leaf_node_key, ARRAY_AGG(DISTINCT central_org_unit_id) as c_ids
        FROM heart360tk_reporting.import_facility_mapping
        WHERE leaf_node_key = ANY(p_source_keys)
        GROUP BY leaf_node_key
    )
    SELECT
        COALESCE(ARRAY_AGG(leaf_node_key), ARRAY[]::text[]),
        COALESCE(
            (SELECT ARRAY_AGG(DISTINCT unnest_c_ids)
             FROM (SELECT unnest(c_ids) AS unnest_c_ids FROM mapped) sub),
            ARRAY[]::integer[]
        )
    INTO v_valid_leaf_nodes, v_central_org_unit_ids
    FROM mapped;

    -- Find nodes without mapping and format error directly
    SELECT COALESCE(ARRAY_AGG(k || ': No central org unit mapping found for leaf node: ' || k), ARRAY[]::text[])
    INTO v_failed_leaf_nodes
    FROM unnest(p_source_keys) k
    WHERE k <> ALL(v_valid_leaf_nodes);

    -- If no valid nodes to process, return early
    IF cardinality(v_valid_leaf_nodes) = 0 THEN
        RETURN jsonb_build_object(
            'success', false,
            'message', 'Data deletion failed for leaf nodes: ' || array_to_string(v_failed_leaf_nodes, ', '),
            'failed_count', cardinality(v_failed_leaf_nodes)
        );
    END IF;

    BEGIN
        -- 2. Bulk delete reporting data (N tables * 1 query instead of N * M queries)
        FOREACH v_table_name IN ARRAY v_report_tables
        LOOP
            EXECUTE format(
                'DELETE FROM heart360tk_reporting.%I WHERE org_unit_id = ANY ($1)',
                v_table_name
            ) USING v_central_org_unit_ids;
        END LOOP;

        -- 3. Bulk Hierarchy Deletion
        IF p_delete_hierarchy THEN
            WITH RECURSIVE selected_hierarchy AS (
                SELECT id, parent_id
                FROM heart360tk_schema.org_units
                WHERE id = ANY(v_central_org_unit_ids)

                UNION

                SELECT parent_ou.id, parent_ou.parent_id
                FROM heart360tk_schema.org_units parent_ou
                JOIN selected_hierarchy h ON parent_ou.id = h.parent_id
            ),
            other_hierarchy AS (
                SELECT ou.id, ou.parent_id
                FROM heart360tk_reporting.import_facility_mapping other_ifm
                JOIN heart360tk_schema.org_units ou ON ou.id = other_ifm.central_org_unit_id
                WHERE other_ifm.leaf_node_key <> ALL(v_valid_leaf_nodes)

                UNION

                SELECT parent_ou.id, parent_ou.parent_id
                FROM heart360tk_schema.org_units parent_ou
                JOIN other_hierarchy h ON parent_ou.id = h.parent_id
            )
            SELECT ARRAY_AGG(h.id)
            INTO v_hierarchy_org_unit_ids
            FROM selected_hierarchy h
            WHERE NOT EXISTS (SELECT 1 FROM other_hierarchy oh WHERE oh.id = h.id);

            -- Remove leaf mappings first
            DELETE FROM heart360tk_reporting.import_facility_mapping WHERE leaf_node_key = ANY(v_valid_leaf_nodes);

            -- Delete unshared hierarchy nodes bottom-up to safely resolve FK constraints
            IF v_hierarchy_org_unit_ids IS NOT NULL THEN
                FOR v_org_unit_id IN
                    SELECT ou.id
                    FROM heart360tk_schema.org_units ou
                    WHERE ou.id = ANY(v_hierarchy_org_unit_ids)
                    ORDER BY ou.level DESC, ou.id DESC
                LOOP
                    DELETE FROM heart360tk_schema.org_units WHERE id = v_org_unit_id;
                END LOOP;
            END IF;
        ELSE
            -- Data-only purge
            DELETE FROM heart360tk_reporting.import_facility_mapping WHERE leaf_node_key = ANY(v_valid_leaf_nodes);
        END IF;

        -- 4. Bulk update import_source_control
        UPDATE heart360tk_reporting.import_source_control
        SET data_status = 'no_data', last_purged_at = NOW()
        WHERE source_key = ANY(v_valid_leaf_nodes);

    EXCEPTION WHEN OTHERS THEN
        -- If the bulk transaction fails (e.g. FK error), rollback everything and mark all valid nodes as failed
        v_error_message := SQLERRM;
        SELECT ARRAY_AGG(k || ': ' || v_error_message) INTO v_valid_leaf_nodes FROM unnest(v_valid_leaf_nodes) k;
        v_failed_leaf_nodes := array_cat(v_failed_leaf_nodes, v_valid_leaf_nodes);
        v_valid_leaf_nodes := ARRAY[]::text[];
    END;

    -- 5. Return JSON summary matching the previous output style
    RETURN jsonb_build_object(
        'success', cardinality(v_failed_leaf_nodes) = 0,
        'message',
            CASE
                WHEN cardinality(v_failed_leaf_nodes) = 0 AND p_delete_hierarchy THEN
                    'Data and hierarchy deleted successfully for leaf node: ' || array_to_string(v_valid_leaf_nodes, ', ')
                WHEN cardinality(v_failed_leaf_nodes) = 0 THEN
                    'Data deleted successfully for leaf nodes: ' || array_to_string(v_valid_leaf_nodes, ', ')
                WHEN cardinality(v_valid_leaf_nodes) = 0 THEN
                    'Data deletion failed for leaf nodes: ' || array_to_string(v_failed_leaf_nodes, ', ')
                ELSE
                    'Data deletion completed. Successful: ' || array_to_string(v_valid_leaf_nodes, ', ') || '. Failed: ' || array_to_string(v_failed_leaf_nodes, ', ')
            END,
        'failed_count', cardinality(v_failed_leaf_nodes)
    );
END;
$$;

GRANT EXECUTE ON FUNCTION heart360tk_reporting.delete_leaf_node_data(
    text[],
    boolean,
    boolean
) TO heart360tk;

-- 8. Update build_drill_url to point to heart360_showcase
CREATE OR REPLACE FUNCTION heart360tk_schema.build_drill_url(p_child_id INTEGER)
RETURNS TEXT
LANGUAGE sql STABLE
AS $$
    SELECT '/d/heart360_showcase/hypertension-and-diabetes-program?var-org_unit=' || p_child_id::text;
$$;

RESET ROLE;

COMMIT;
