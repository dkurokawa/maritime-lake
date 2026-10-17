-- Gold: silver + 7-day trailing means, year-over-year change and a 2019-2022 seasonal baseline.
--
--   ma7_total / ma7_capacity : trailing 7-day mean (NULL until 7 observations exist).
--   yoy_pct                  : 100 * (ma7_total / ma7_total one calendar year earlier - 1).
--   baseline_ma7             : mean of ma7_total for the same month-day over 2019-2022.
--                              Feb 29 rows are excluded from building the baseline and
--                              look up the Feb 28 baseline.
--   baseline_lo / baseline_hi: min / max of those yearly ma7_total values (the "band").
--   vs_baseline_pct          : 100 * (ma7_total / baseline_ma7 - 1).
CREATE OR REPLACE TABLE gold_chokepoint_daily AS
WITH base AS (
    SELECT
        s.*,
        CASE WHEN month(date) = 2 AND day(date) = 29 THEN '02-28' ELSE strftime(date, '%m-%d') END AS md,
        CASE WHEN count(*) OVER w = 7 THEN avg(n_total) OVER w END  AS ma7_total,
        CASE WHEN count(*) OVER w = 7 THEN avg(capacity) OVER w END AS ma7_capacity
    FROM silver_chokepoint_daily s
    WINDOW w AS (PARTITION BY portid ORDER BY date RANGE BETWEEN INTERVAL 6 DAYS PRECEDING AND CURRENT ROW)
),
baseline AS (
    SELECT portid, md,
           avg(ma7_total) AS baseline_ma7,
           min(ma7_total) AS baseline_lo,
           max(ma7_total) AS baseline_hi
    FROM base
    WHERE year BETWEEN 2019 AND 2022
      AND NOT (month(date) = 2 AND day(date) = 29)
      AND ma7_total IS NOT NULL
    GROUP BY portid, md
)
SELECT
    b.date, b.year, b.portid, b.portname,
    b.n_container, b.n_dry_bulk, b.n_general_cargo, b.n_roro, b.n_tanker, b.n_cargo, b.n_total,
    b.capacity_container, b.capacity_dry_bulk, b.capacity_general_cargo, b.capacity_roro,
    b.capacity_tanker, b.capacity_cargo, b.capacity,
    b.ma7_total, b.ma7_capacity,
    CASE WHEN p.ma7_total > 0 THEN 100.0 * (b.ma7_total / p.ma7_total - 1) END AS yoy_pct,
    bl.baseline_ma7, bl.baseline_lo, bl.baseline_hi,
    CASE WHEN bl.baseline_ma7 > 0 THEN 100.0 * (b.ma7_total / bl.baseline_ma7 - 1) END AS vs_baseline_pct
FROM base b
LEFT JOIN base p
       ON p.portid = b.portid AND p.date = CAST(b.date - INTERVAL 1 YEAR AS DATE)
LEFT JOIN baseline bl ON bl.portid = b.portid AND bl.md = b.md
ORDER BY b.portid, b.date;
