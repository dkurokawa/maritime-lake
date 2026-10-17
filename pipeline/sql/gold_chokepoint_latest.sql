-- Gold: one row per chokepoint at its latest available date, plus coordinates.
CREATE OR REPLACE TABLE gold_chokepoint_latest AS
WITH last AS (SELECT portid, max(date) AS date FROM gold_chokepoint_daily GROUP BY portid)
SELECT g.*, d.fullname, d.lat, d.lon
FROM gold_chokepoint_daily g
JOIN last USING (portid, date)
LEFT JOIN silver_dim_chokepoint d USING (portid)
ORDER BY g.portid;
