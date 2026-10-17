-- Silver: typed daily port calls for the selected container ports, deduplicated by (portid, date).
-- Same upsert rule as silver_chokepoint_daily: the most recent bronze run wins, which is how the
-- trailing-window re-fetch applies revised (modelled) values.
CREATE OR REPLACE TABLE silver_port_daily AS
WITH raw AS (
    SELECT
        unnest(features).attributes AS a,
        regexp_extract(filename, 'run=([0-9]{8}T[0-9]{6}Z)', 1) AS run_id
    FROM read_json(
        '${bronze_glob}',
        columns = {features: 'STRUCT(attributes STRUCT(
            date VARCHAR, portid VARCHAR, portname VARCHAR, country VARCHAR,
            portcalls_container BIGINT, portcalls_dry_bulk BIGINT, portcalls_general_cargo BIGINT,
            portcalls_roro BIGINT, portcalls_tanker BIGINT, portcalls_cargo BIGINT, portcalls BIGINT))[]'},
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
    a.country                       AS country,
    a.portcalls_container, a.portcalls_dry_bulk, a.portcalls_general_cargo,
    a.portcalls_roro, a.portcalls_tanker, a.portcalls_cargo, a.portcalls,
    run_id                          AS ingested_run
FROM ranked
WHERE rn = 1
ORDER BY portid, date;
