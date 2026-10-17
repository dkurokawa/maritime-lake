-- Silver: container port-call ranking over the trailing 365 days (latest ingest run only).
-- Bronze holds the result of one server-side statistics query on Daily_Ports_Data:
-- sum(portcalls_container) grouped by port, ordered by calls DESC. rank 1 = busiest.
-- Ties are broken by portid so the ranking is deterministic.
CREATE OR REPLACE TABLE silver_container_port_rank AS
WITH raw AS (
    SELECT
        unnest(features).attributes AS a,
        regexp_extract(filename, 'run=([0-9]{8}T[0-9]{6}Z)', 1) AS run_id
    FROM read_json(
        '${bronze_glob}',
        columns = {features: 'STRUCT(attributes STRUCT(
            portid VARCHAR, portname VARCHAR, country VARCHAR, calls DOUBLE))[]'},
        filename = true
    )
),
latest AS (SELECT max(run_id) AS run_id FROM raw)
SELECT
    a.portid AS portid, a.portname AS portname, a.country AS country,
    CAST(a.calls AS BIGINT) AS calls_365d,
    CAST(row_number() OVER (ORDER BY a.calls DESC, a.portid) AS INTEGER) AS rank
FROM raw JOIN latest USING (run_id)
ORDER BY rank;
