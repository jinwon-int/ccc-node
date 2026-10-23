- **deps(security): aiohttp 3.14.3 → 3.14.4, certifi 2026.7.22 → 2026.10.19, OpenTelemetry 1.44.0 → 1.45.1 in the Termux mempalace
  requirements (#1864 tracker; Dependabot #14–#23).** aiohttp 3.14.4 needs `yarl>=1.25.1`, so yarl moves 1.24.5 → 1.25.1;
  the OpenTelemetry family moves together (`opentelemetry-semantic-conventions` 0.65b0 → 0.66b1) and exporter-grpc 1.45.1
  adds one new transitive pin, `opentelemetry-exporter-otlp-common==0.66b1`. The full set resolves to exactly the 81
  pinned packages with cp311 aarch64 / py3 wheels (`--only-binary=:all:`). chromadb 1.5.9 stays (alerts #1–#4 have no
  patched release yet; tracked by `chromadb-release-watch`). The node lane is off, so this changes the manifest only;
  an installed node with the old lock keeps refusing an in-place non-mempalace delta as before.
