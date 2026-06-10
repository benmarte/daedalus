// Build the Daedalus dashboard tab.
//
// Compiles src/App.jsx into a single self-contained IIFE at
// dist/index-<content-hash>.js, per the Hermes dashboard-plugin contract:
//   - React is NEVER bundled — it comes from window.__HERMES_PLUGIN_SDK__.React
//     at runtime (the source reads SDK.React directly), so the JSX factory
//     resolves to the in-scope `React` var and the output stays a few KB.
//   - Output is one file loadable via <script>; no sourcemap sidecar.
//   - The content hash is derived from the bundle itself so URLs are
//     cache-busting: a new build always produces a new filename. The previous
//     bundle is cleaned up so dist/ never contains stale artifacts.
//   - After the build, manifest.json is rewritten so `entry` points to the
//     hashed path. This keeps the Hermes plugin router in sync automatically.
//
// Usage: npm run build   (or: node build.js)
const esbuild = require("esbuild");
const fs = require("fs");
const path = require("path");

const distDir = path.join(__dirname, "dist");
const manifestPath = path.join(__dirname, "manifest.json");

// 1. Ensure dist/ exists.
fs.mkdirSync(distDir, { recursive: true });

// 2. Clean old bundle files so stale hashed bundles don't pile up.
for (const f of fs.readdirSync(distDir)) {
  if (f.endsWith(".js")) {
    fs.unlinkSync(path.join(distDir, f));
  }
}

// 3. Build with content hash via entryNames.
esbuild
  .build({
    entryPoints: ["src/App.jsx"],
    bundle: true,
    outdir: "dist",
    entryNames: "index-[hash]",
    metafile: true,
    format: "iife",
    globalName: "__HERMES_DAEDALUS_DASHBOARD__",
    external: ["react", "react-dom"],
    minify: false,
    sourcemap: false,
    target: "es2020",
    banner: {
      js: "// GENERATED FROM src/App.jsx — DO NOT EDIT. Rebuild with: npm run build",
    },
  })
  .then(function (result) {
    // 4. Read the emitted filename from the metafile.
    const outputs = Object.keys(result.metafile.outputs);
    const jsOutput = outputs.find(function (o) {
      return o.endsWith(".js");
    });
    if (!jsOutput) {
      throw new Error("No JS output found in esbuild metafile");
    }

    const hashedFilename = path.basename(jsOutput);

    // 5. Rewrite manifest.json so `entry` points to the hashed path.
    const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf-8"));
    manifest.entry = "dist/" + hashedFilename;
    fs.writeFileSync(
      manifestPath,
      JSON.stringify(manifest, null, 2) + "\n",
    );

    console.log(
      "✓ Built dist/" + hashedFilename + " from src/App.jsx",
    );
  })
  .catch(function (err) {
    console.error(err);
    process.exit(1);
  });
