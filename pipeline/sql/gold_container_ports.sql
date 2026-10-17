-- Gold: the selected container ports with coordinates and recent activity.
--   rank, calls_365d : container port calls over the trailing 365 days (from the statistics query).
--   ma7_container    : mean of portcalls_container over the 7 calendar days ending at the port's
--                      latest date; NULL unless all 7 days are observed.
--   latest_date      : the port's latest date in silver_port_daily.
CREATE OR REPLACE TABLE gold_container_ports AS
WITH w AS (
    SELECT
        portid, date,
        CASE WHEN count(*) OVER win = 7 THEN avg(portcalls_container) OVER win END AS ma7_container
    FROM silver_port_daily
    WINDOW win AS (PARTITION BY portid ORDER BY date RANGE BETWEEN INTERVAL 6 DAYS PRECEDING AND CURRENT ROW)
),
latest AS (
    SELECT portid, arg_max(ma7_container, date) AS ma7_container, max(date) AS latest_date
    FROM w GROUP BY portid
)
SELECT
    s.portid,
    coalesce(d.portname, r.portname) AS portname,
    coalesce(d.country, r.country)   AS country,
    d.lat, d.lon,
    s.rank, s.calls_365d,
    l.ma7_container, l.latest_date
FROM gold_container_port_selection s
LEFT JOIN silver_dim_port d USING (portid)
LEFT JOIN silver_container_port_rank r USING (portid)
LEFT JOIN latest l USING (portid)
ORDER BY s.rank NULLS LAST, s.portid;
