-- Silver: typed daily chokepoint facts, deduplicated by (portid, date).
-- Bronze holds every ingest run; the most recent run wins, which is how the
-- trailing-window re-fetch upserts revised (modelled) values.
CREATE OR REPLACE TABLE silver_chokepoint_daily AS
WITH raw AS (
    SELECT
        unnest(features).attributes AS a,
        regexp_extract(filename, 'run=([0-9]{8}T[0-9]{6}Z)', 1) AS run_id
    FROM read_json(
        '${bronze_glob}',
        columns = {features: 'STRUCT(attributes STRUCT(
            date VARCHAR, portid VARCHAR, portname VARCHAR,
            n_container BIGINT, n_dry_bulk BIGINT, n_general_cargo BIGINT, n_roro BIGINT,
            n_tanker BIGINT, n_cargo BIGINT, n_total BIGINT,
            capacity_container BIGINT, capacity_dry_bulk BIGINT, capacity_general_cargo BIGINT,
            capacity_roro BIGINT, capacity_tanker BIGINT, capacity_cargo BIGINT, capacity BIGINT))[]'},
        filename = true
    )
),
ranked AS (
    SELECT *, row_number() OVER (PARTITION BY a.portid, a.date ORDER BY run_id DESC) AS rn
    FROM raw
)
SELECT
    CAST(a.date AS DATE)            AS date,
    year(CAST(a.date AS DATE))      AS year,
    a.portid                        AS portid,
    a.portname                      AS portname,
    a.n_container, a.n_dry_bulk, a.n_general_cargo, a.n_roro, a.n_tanker, a.n_cargo, a.n_total,
    a.capacity_container, a.capacity_dry_bulk, a.capacity_general_cargo, a.capacity_roro,
    a.capacity_tanker, a.capacity_cargo, a.capacity,
    run_id                          AS ingested_run
FROM ranked
WHERE rn = 1
ORDER BY portid, date;
