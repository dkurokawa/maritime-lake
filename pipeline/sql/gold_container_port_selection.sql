-- Gold: which ports the time map covers. Computed, not hand-picked:
-- the top ${top_n} ports by container calls over the last 365 days, plus the preset origins
-- (the only hard-coded ids) even when they fall outside the top ${top_n}.
-- Used by ingest (to decide which ports to fetch daily data for) and by gold_container_ports.
CREATE OR REPLACE TABLE gold_container_port_selection AS
WITH wanted AS (
    SELECT portid FROM silver_container_port_rank WHERE rank <= ${top_n}
    UNION
    SELECT unnest([${preset_ids}]) AS portid
)
SELECT w.portid, r.rank, r.calls_365d
FROM wanted w
LEFT JOIN silver_container_port_rank r USING (portid)
ORDER BY r.rank NULLS LAST, w.portid;
