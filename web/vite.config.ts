import { defineConfig, type Plugin } from "vite";
import { createReadStream, existsSync, statSync, cpSync } from "node:fs";
import { resolve, join, normalize } from "node:path";

const GOLD = process.env.MLAKE_DATA_DIR ? resolve(process.env.MLAKE_DATA_DIR, "gold") : resolve(import.meta.dirname, "../data/gold");

/** Dev: serve ../data/gold at /data. Build: copy it into dist/data. */
function goldData(): Plugin {
  return {
    name: "maritime-lake-gold-data",
    configureServer(server) {
      server.middlewares.use("/data", (req, res, next) => {
        const rel = normalize(decodeURIComponent((req.url ?? "/").split("?")[0])).replace(/^(\.\.[/\\])+/, "");
        const file = join(GOLD, rel);
        if (!file.startsWith(GOLD) || !existsSync(file) || !statSync(file).isFile()) return next();
        res.setHeader("Content-Type", file.endsWith(".json") || file.endsWith(".geojson") ? "application/json" : "application/octet-stream");
        createReadStream(file).pipe(res);
      });
    },
    closeBundle() {
      if (existsSync(GOLD)) cpSync(GOLD, resolve(import.meta.dirname, "dist/data"), { recursive: true });
    },
  };
}

export default defineConfig({
  plugins: [goldData()],
  // maplibre-gl v6 loads a sibling worker module; pre-bundling breaks that lookup in dev.
  optimizeDeps: { exclude: ["maplibre-gl"] },
  build: { chunkSizeWarningLimit: 1500 },
});
