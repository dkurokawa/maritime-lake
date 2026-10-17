-- Silver: chokepoint dimension (latest ingest run only).
CREATE OR REPLACE TABLE silver_dim_chokepoint AS
WITH raw AS (
    SELECT
        unnest(features).attributes AS a,
        regexp_extract(filename, 'run=([0-9]{8}T[0-9]{6}Z)', 1) AS run_id
    FROM read_json(
        '${bronze_glob}',
        columns = {features: 'STRUCT(attributes STRUCT(
            portid VARCHAR, portname VARCHAR, fullname VARCHAR, lat DOUBLE, lon DOUBLE))[]'},
        filename = true
    )
),
latest AS (SELECT max(run_id) AS run_id FROM raw)
SELECT a.portid AS portid, a.portname AS portname, a.fullname AS fullname, a.lat AS lat, a.lon AS lon
FROM raw JOIN latest USING (run_id)
ORDER BY portid;
