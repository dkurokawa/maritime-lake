-- Silver: PortWatch natural-disaster disruptions (GDACS-like). Latest run wins per eventid.
CREATE OR REPLACE TABLE silver_disruptions AS
WITH raw AS (
    SELECT
        unnest(features).attributes AS a,
        regexp_extract(filename, 'run=([0-9]{8}T[0-9]{6}Z)', 1) AS run_id
    FROM read_json(
        '${bronze_glob}',
        columns = {features: 'STRUCT(attributes STRUCT(
            eventid BIGINT, eventtype VARCHAR, eventname VARCHAR, htmlname VARCHAR,
            htmldescription VARCHAR, alertlevel VARCHAR, country VARCHAR,
            fromdate BIGINT, todate BIGINT, affectedports VARCHAR, n_affectedports BIGINT,
            lat DOUBLE, long DOUBLE))[]'},
        filename = true
    )
),
ranked AS (
    SELECT *, row_number() OVER (PARTITION BY a.eventid ORDER BY run_id DESC) AS rn FROM raw
)
SELECT
    a.eventid AS eventid, a.eventtype AS eventtype, a.eventname AS eventname,
    a.htmlname AS title, a.htmldescription AS description, a.alertlevel AS alertlevel,
    a.country AS country,
    CAST(epoch_ms(a.fromdate) AS DATE) AS start_date,
    CAST(epoch_ms(a.todate) AS DATE)   AS end_date,
    a.affectedports AS affectedports, a.n_affectedports AS n_affectedports,
    a.lat AS lat, a.long AS lon
FROM ranked WHERE rn = 1
ORDER BY start_date, eventid;
