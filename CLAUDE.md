# IT Metrics Repository

## Repo Overview
- Git remote: github.com/BristowInsight/IT-Metrics
- Contains multiple Power BI projects (PBIP format) + 1 Lakehouse
- `Archive/` — retired projects (LN-OIJ Flight Following)
- No .gitignore — all PBIR/TMDL files are tracked

## Active Projects
- **Bristow Insight Portfolio** — Product catalog dashboard (SharePoint Lists source)
- **ISMS KPI Dashboard** — Information security metrics
- **ITSC Metrics** — IT service center metrics
- **IT_Metrics** — Lakehouse + semantic model (Fabric)

## File Formats
- `.pbip` — Project entry point (open in Desktop)
- `.pbir` — Report pointer (inside .Report/ folders)
- `.pbism` — Semantic model pointer (inside .SemanticModel/ folders)
- `.tmdl` — Tabular model definitions (tables, measures, relationships, columns)
- `.platform` — Fabric workspace metadata (JSON)
- `.sample` — Data preview samples (do not edit)

## PBIR Gotchas
- Desktop validates visual.json strictly — no extra properties (e.g. `columnWidths` is runtime-only)
- `customTheme.type` must be `"RegisteredResources"`, not `"CustomTheme"`
- Desktop auto-renames visual folder names to hex IDs on save — don't rely on custom folder names
- Schema versions must match what Desktop generates (check existing files, don't guess)
- Close Desktop before editing PBIR files, then reopen

## PBI Modeling MCP
- Connect to Desktop: `Connect` with `Data Source=localhost:<port>` (find port via `ListLocalInstances`)
- Connect to Fabric: `ConnectFabric` with workspaceName + semanticModelName (workspace: "IT Metrics")
- **MCP or Git, never both in the same direction** — pick one write path per change:
  - MCP path: modify live model via MCP, then "Commit to git" from Fabric to sync back
  - Git path: edit TMDL/PBIR files, commit, push, then "Update from git" in Fabric
  - Never push git changes to a model that was just modified via MCP (etag conflict)
- Table refresh for SharePoint List sources fails via MCP — refresh in Desktop manually
- DAX table names need single quotes: `'Catalog'[Column]` not `Catalog[Column]`

## Color Palette (Refined Bristow Fluent 2)
- Navy: #1B305A (headers, emphasis)
- Active Development: #D4712A (orange)
- Pipeline: #6B6B6B / #E5E5E5 (grey)
- Production: #3680C4 (blue)
- Page background: #FAFAFA
- Stable operations text: #8A8A8A italic
