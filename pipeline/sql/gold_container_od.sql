-- Gold: origin-destination sailing distances between the selected ports (nautical miles).
-- Rows come from the searoute cache (silver_container_od, written by `mlake od`) restricted to
-- the currently selected ports. nm is NULL when searoute finds no path under the scenario.
CREATE OR REPLACE TABLE gold_container_od AS
SELECT o.origin, o.dest, o.scenario, o.nm, o.searoute_version
FROM silver_container_od o
WHERE o.origin IN (SELECT portid FROM gold_container_ports)
  AND o.dest   IN (SELECT portid FROM gold_container_ports)
ORDER BY o.scenario, o.origin, o.dest;
