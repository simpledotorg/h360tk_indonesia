import sys
import json
import pandas as pd
from datetime import datetime
import re
import os
import psycopg2
from typing import Dict, Any

# --- CONFIGURATION (WHITELIST) ---
FINAL_COLUMN_WHITELIST = [
    'Puskesmas',
    'dump_start_date',
    'dump_end_date',
    'nik',
    'no_rm_lama',
    'sistole',
    'diastole',
    'nama_pasien',
    'tgl_lahir',
    'tanggal',
    'no_telp',
    'jenis_kelamin'

]
# ---------------------------------

# Define the date formats for parsing and output
DATE_FORMAT_IN = "%d-%m-%Y"
DATE_FORMAT_OUT = "%Y-%m-%d"

# --- DATABASE CONNECTION DETAILS ---
DB_CONNECTION_PARAMS = {
    'host': os.getenv('POSTGRES_HOST', 'postgres'),
    'database': os.getenv('POSTGRES_DB', 'metrics_db'),
    'user': os.getenv('POSTGRES_USER', 'grafana_user'),
    'password': os.getenv('POSTGRES_PASSWORD', 'your_db_password'),
}
SP_REGION_VALUE = 'Demo'
# ----------------------------------------------------------------

# --- HIERARCHY CONFIGURATION ---
# Level 1 (Region) and Level 2 (District) use default values since the
# current Excel format only provides Puskesmas (PHC) from metadata.
# Level 3 (Facility) is populated from the Puskesmas metadata field.
#
# Fields:
#   level        - integer depth (1 = top)
#   column       - Excel column name(s) to read (first match wins)
#   display_name - label for readability only
#   var_name     - Levels 1-5 use fixed names (region, district, phc, shc, village)
#   default      - fallback value when column is empty (None = skip level)

HIERARCHY_LEVELS = [
    {'level': 1, 'column': [],            'display_name': 'Region',   'var_name': 'region',   'default': SP_REGION_VALUE},
    {'level': 2, 'column': [],            'display_name': 'District', 'var_name': 'district', 'default': SP_REGION_VALUE},
    {'level': 3, 'column': ['puskesmas'], 'display_name': 'Facility', 'var_name': 'facility', 'default': 'UNKNOWN'},
]
# ----------------------------------------------------------------

# --- HELPER FUNCTIONS ---
def clean_blood_pressure(value):
    if pd.isna(value) or value is None:
        return None
    s = str(value).strip()
    cleaned_s = re.sub(r'[^\d.]', '', s)
    try:
        if not cleaned_s or cleaned_s == '.':
            return None
        return float(cleaned_s)
    except ValueError:
        return None

def parse_date_field(value):
    if pd.isna(value) or value is None:
        return None
    if isinstance(value, (datetime, pd.Timestamp)):
        return value
    if isinstance(value, str):
        value = value.strip()
        if not value or value.lower() == 'nan':
            return None
        try:
            return datetime.strptime(value, DATE_FORMAT_IN)
        except ValueError:
            pass
        try:
            parsed = pd.to_datetime(value)
            return parsed.to_pydatetime() if hasattr(parsed, 'to_pydatetime') else parsed
        except (ValueError, TypeError):
            return None
    try:
        parsed = pd.to_datetime(value)
        return parsed.to_pydatetime() if hasattr(parsed, 'to_pydatetime') else parsed
    except (ValueError, TypeError):
        return None

def safe_str(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return str(value)

def to_sql_literal(value, target_type=None):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        if target_type == 'bigint':
            return 'NULL::BIGINT'
        elif target_type == 'DATE':
            return 'NULL::DATE'
        elif target_type == 'TIMESTAMP':
            return 'NULL::TIMESTAMP'
        elif target_type == 'NUMERIC':
            return 'NULL::NUMERIC'
        else:
            return 'NULL::VARCHAR'

    if target_type == 'bigint':
        val_str = str(value).strip()
        if val_str.endswith('.0'):
            val_str = val_str[:-2]
        if not val_str.isdigit():
            return 'NULL::BIGINT'
        return f"CAST('{val_str}' AS bigint)"

    if target_type == 'DATE' and isinstance(value, (datetime, pd.Timestamp)):
        return f"'{value.strftime('%Y-%m-%d')}'::DATE"

    if target_type == 'TIMESTAMP' and isinstance(value, (datetime, pd.Timestamp)):
        return f"'{value.strftime('%Y-%m-%d %H:%M:%S')}'::timestamp"

    if isinstance(value, str):
        return f"'{value.replace(chr(39), chr(39)+chr(39))}'::VARCHAR"

    if isinstance(value, (int, float)):
        return str(value)

    return f"'{str(value).replace(chr(39), chr(39)+chr(39))}'::VARCHAR"

def get_metadata_from_excel(file_path):
    ROWS_TO_READ = 22
    START_ROW_INDEX = 2

    try:
        df_metadata = pd.read_excel(file_path, sheet_name=0, header=None, skiprows=START_ROW_INDEX, nrows=ROWS_TO_READ, usecols=[0],engine='calamine')
    except Exception as e:
        raise Exception(f"Failed to read metadata block: {e}")

    metadata = {}
    for index, row in df_metadata.iterrows():
        line = str(row[0]).strip()
        parts = line.split(':', 1)
        if len(parts) == 2:
            key = parts[0].strip()
            value = parts[1].strip()
            if key and key.lower() != 'laporan harian - pelayanan pasien':
                metadata[key] = value if value else None

    if 'Tanggal' in metadata:
        tanggal_value = metadata.pop('Tanggal')
        if tanggal_value and ' - ' in tanggal_value:
            try:
                start_date_str, end_date_str = [d.strip() for d in tanggal_value.split(' - ')]
                start_date_dt = datetime.strptime(start_date_str, DATE_FORMAT_IN)
                metadata['dump_start_date'] = start_date_dt.strftime(DATE_FORMAT_OUT)
                end_date_dt = datetime.strptime(end_date_str, DATE_FORMAT_IN)
                metadata['dump_end_date'] = end_date_dt.strftime(DATE_FORMAT_OUT)
            except ValueError:
                metadata['dump_start_date'] = None
                metadata['dump_end_date'] = None
        else:
            metadata['dump_start_date'] = None
            metadata['dump_end_date'] = None

    return metadata

# --- DATABASE HELPER FUNCTIONS ---

def execute_upsert_org_unit_chain(cur, hierarchy):
    """Upsert org_unit hierarchy chain and return leaf org_unit_id.
    hierarchy: list of (name, level) tuples from top to bottom.
    Skips entries with None names.
    """
    names = [h[0] for h in hierarchy if h[0] is not None]
    levels = [h[1] for h in hierarchy if h[0] is not None]
    if not names:
        return None
    names_literal = "ARRAY[" + ",".join(to_sql_literal(n) for n in names) + "]"
    levels_literal = "ARRAY[" + ",".join(str(l) for l in levels) + "]"
    sql = f"SELECT upsert_org_unit_chain({names_literal}::VARCHAR[], {levels_literal}::INTEGER[]);"
    cur.execute(sql)
    return cur.fetchone()[0]

def execute_upsert_patient(cur, patient_id_sql, record, registration_date_parsed, birth_date_parsed, org_unit_id):
    """Insert new patient or update registration_date if earlier."""
    sql = f"""
INSERT INTO patients (patient_id, patient_name, gender, phone_number, patient_status, registration_date, birth_date, org_unit_id)
VALUES (
    {patient_id_sql},
    {to_sql_literal(safe_str(record.get('nama_pasien')))},
    {to_sql_literal(safe_str(record.get('jenis_kelamin')))},
    {to_sql_literal(safe_str(record.get('no_telp')))},
    'ALIVE'::VARCHAR,
    {to_sql_literal(registration_date_parsed, target_type='TIMESTAMP')},
    {to_sql_literal(birth_date_parsed, target_type='DATE')},
    {org_unit_id}
)
ON CONFLICT (patient_id) DO UPDATE SET
    registration_date = LEAST(patients.registration_date, EXCLUDED.registration_date);
"""
    cur.execute(sql)

def execute_insert_encounter(cur, patient_id_sql, encounter_datetime_parsed, org_unit_id):
    """Create encounter (or get existing). Returns encounter_id."""
    sql = f"""
INSERT INTO encounters (patient_id, encounter_date, org_unit_id)
VALUES ({patient_id_sql}, {to_sql_literal(encounter_datetime_parsed, target_type='TIMESTAMP')}, {org_unit_id})
ON CONFLICT (patient_id, encounter_date)
DO UPDATE SET org_unit_id = EXCLUDED.org_unit_id
RETURNING id;
"""
    cur.execute(sql)
    return cur.fetchone()[0]

def execute_insert_bp(cur, encounter_id, systolic, diastolic):
    """Insert blood pressure for an encounter."""
    if systolic is None and diastolic is None:
        return
    sql = f"""
INSERT INTO blood_pressures (encounter_id, systolic_bp, diastolic_bp)
VALUES ({encounter_id}, {to_sql_literal(systolic, target_type='NUMERIC')}, {to_sql_literal(diastolic, target_type='NUMERIC')})
ON CONFLICT (encounter_id) DO UPDATE SET
    systolic_bp = EXCLUDED.systolic_bp, diastolic_bp = EXCLUDED.diastolic_bp;
"""
    cur.execute(sql)

# --- MAIN INGESTION AND EXECUTION FUNCTION ---

def ingest_and_execute(file_path):
    """
    Main function to read data from Excel (with metadata header), generate SQL,
    and execute against the DB using row-by-row commit (autocommit).

    The Excel has metadata in rows 2-24 (including Puskesmas name and date range)
    and data starting at row 25+.

    Date handling:
      - 'tanggal' is used as encounter_date, registration_date, and BP date.
      - No blood sugar or blood sugar date logic.

    Hierarchy:
      - Level 1 (Region): defaults to SP_REGION_VALUE
      - Level 2 (District): defaults to SP_REGION_VALUE
      - Level 3 (PHC): from Puskesmas metadata
    """

    # 1. Extract and process static metadata
    try:
        static_metadata = get_metadata_from_excel(file_path)
        facility = static_metadata.get('Puskesmas', 'UNKNOWN')
    except Exception as e:
        print(f"Error during metadata extraction: {e}", file=sys.stderr)
        return

    print(json.dumps(static_metadata, ensure_ascii=False))

    # 2. Load the main tabular data
    SKIP_ROWS_TO_DATA_HEADER = 25

    DTYPE_MAPPING = {'nik': str}

    try:
        df_data = pd.read_excel(
            file_path,
            sheet_name=0,
            header=0,
            skiprows=SKIP_ROWS_TO_DATA_HEADER,
            dtype=DTYPE_MAPPING,
            engine='calamine'
        )
    except Exception as e:
        print(f"Error loading main data table: {e}", file=sys.stderr)
        return

    df_data.columns = df_data.columns.astype(str).str.lower().str.replace(r'[^a-z0-9_]+', '_', regex=True).str.strip('_')

    print(f"Columns found: {list(df_data.columns)}", file=sys.stderr)
    print(f"Total rows: {len(df_data)}", file=sys.stderr)

    # Build hierarchy using metadata facility for level 3 (PHC)
    # Levels 1 and 2 use defaults since current Excel only has Puskesmas
    hierarchy = []
    for hlvl in HIERARCHY_LEVELS:
        if hlvl['level'] == 3:
            # PHC level: use facility from metadata
            hierarchy.append((facility, hlvl['level']))
        else:
            # Region/District: use default values
            value = hlvl.get('default')
            if value:
                hierarchy.append((value, hlvl['level']))

    # --- DATABASE EXECUTION ---
    conn = None
    cur = None
    total_processed = 0
    success_inserts = 0

    try:
        conn = psycopg2.connect(**DB_CONNECTION_PARAMS)
        conn.autocommit = True
        cur = conn.cursor()

        # Auto-sync hierarchy_config from HIERARCHY_LEVELS
        for hlvl in HIERARCHY_LEVELS:
            cur.execute("""
                INSERT INTO hierarchy_config (level, display_name, var_name)
                VALUES (%s, %s, %s)
                ON CONFLICT (level) DO UPDATE
                    SET display_name = EXCLUDED.display_name,
                        var_name     = EXCLUDED.var_name
            """, (hlvl['level'], hlvl['display_name'], hlvl['var_name']))

        # Upsert org_unit hierarchy chain once (same for all rows in this file)
        org_unit_id = execute_upsert_org_unit_chain(cur, hierarchy)

        # 3. Process each row
        for record in df_data.to_dict('records'):
            total_processed += 1

            first_key = next(iter(record), None)
            if first_key and pd.isna(record.get(first_key)):
                 continue

            flat_record = record

            # Parse date fields
            birth_date_parsed = parse_date_field(flat_record.get('tgl_lahir'))
            tanggal_parsed = parse_date_field(flat_record.get('tanggal'))

            # Use 'tanggal' for both registration and encounter date
            registration_date_parsed = tanggal_parsed
            encounter_datetime_parsed = tanggal_parsed

            # Validate: Skip if tanggal (date) is missing
            if tanggal_parsed is None:
                print(f"\n--- SKIPPING RECORD (NO DATE) ---", file=sys.stderr)
                print(f"Skipping record #{total_processed} - tanggal is required", file=sys.stderr)
                continue

            # Clean Sistole and Diastole
            flat_record['sistole'] = clean_blood_pressure(flat_record.get('sistole'))
            flat_record['diastole'] = clean_blood_pressure(flat_record.get('diastole'))

            has_bp = flat_record.get('sistole') is not None or flat_record.get('diastole') is not None

            # Prepare JSON for logging
            final_filtered_record = {
                key: None if pd.isna(flat_record.get(key)) else flat_record.get(key)
                for key in FINAL_COLUMN_WHITELIST
                if key in flat_record
            }
            print(json.dumps(final_filtered_record, ensure_ascii=False, default=str))

            # --- Per-Row Insertion Attempt ---
            try:
                patient_id_sql = to_sql_literal(flat_record.get('nik'), target_type='bigint')

                if patient_id_sql == 'NULL::BIGINT':
                    print(f"\n--- SKIPPING RECORD (NULL patient_id) ---", file=sys.stderr)
                    print(f"Skipping record #{total_processed} due to NULL patient_id (nik)", file=sys.stderr)
                    continue

                # 1. Upsert patient (always — even without BP data)
                execute_upsert_patient(cur, patient_id_sql, flat_record, registration_date_parsed, birth_date_parsed, org_unit_id)

                # 2. Create encounter + BP only if BP values are present
                if has_bp:
                    enc_id = execute_insert_encounter(cur, patient_id_sql, encounter_datetime_parsed, org_unit_id)
                    execute_insert_bp(cur, enc_id, flat_record.get('sistole'), flat_record.get('diastole'))

                success_inserts += 1

            except psycopg2.Error as e:
                # Log the error but CONTINUE to the next record
                print(f"\n--- RECORD FAILURE ---", file=sys.stderr)
                print(f"Error processing record #{total_processed}. Skipping. Details: {e}", file=sys.stderr)

        print(f"\n--- EXECUTION SUMMARY ---", file=sys.stderr)
        print(f"Total records processed: {total_processed}", file=sys.stderr)
        print(f"Successfully inserted records: {success_inserts}", file=sys.stderr)
        print(f"Failed records (skipped): {total_processed - success_inserts}", file=sys.stderr)

    except psycopg2.Error as e:
        print(f"\n--- CONNECTION ERROR ---", file=sys.stderr)
        print(f"PostgreSQL Connection Error: {e}", file=sys.stderr)

    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python ingest_file_puskesmas.py <xlsx_file_path>", file=sys.stderr)
    else:
        ingest_and_execute(sys.argv[1])
