# artifact-publisher

Publish anything built on a remote Mac mini server (HTML prototypes, plan docs, static sites) so it can be viewed from anywhere over LAN or Tailscale — with a date-grouped home page.

## Architecture Plan

Two pieces: a tiny always-on server on the Mac mini, and a global opencode skill agents call to publish.

### Goals
- Publish any artifact (HTML prototypes, plan docs, static sites) so it's viewable over LAN/Tailscale.
- Open one URL (`/`) to see all artifacts grouped by date, newest first — no exact URL needed.
- Lightweight, near-zero maintenance, reliable.

### Piece 1 — The server (runs on the Mac mini)
- **Single-file Python stdlib app** (`artifact-server.py`, ~200 lines). No pip deps — macOS ships `python3`, so zero setup.
- Serves:
  - `GET /` → home page: mobile-first day feed of artifacts (newest first), day headers with count, type filter pills, search, light/dark theme toggle.
  - `GET /<date>/<slug>/` → the artifact (redirects to its `entry` file, e.g. `index.html`).
  - `GET /api/artifacts.json` → machine-readable list (headroom for future tooling/search).
  - `GET /healthz` → for uptime checks.
- **Dynamic index**: the home page is generated on each request by scanning the artifacts directory. No regeneration step → it can never go stale, even if files land there manually.
- **On-disk layout**:
  ```
  ~/artifacts/
    2026-08-05/
      2026-08-05-1030-proto-name/
        artifact.json      # title, type, description, created, entry
        index.html
        ...
  ```
- **Config** in `~/.artifacts/config.json`: port (default `8787`), bind address (default `0.0.0.0` for LAN + Tailscale; can pin to a Tailscale IP), artifacts root.
- **Runs under launchd** (LaunchAgent plist with `RunAtLoad` + `KeepAlive`): starts at boot, auto-restarts if it crashes, logs to `~/Library/Logs/artifacts.log`.

### Piece 2 — The publish skill (used by agents)
- Global skill at `~/.agents/skills/publish-artifact/SKILL.md`, backed by a tiny CLI `publish-artifact <src> --title "..." --type prototype|plan|site --desc "..."`.
- Publish flow:
  1. Copy source into a temp dir.
  2. Write `artifact.json` (title/type/description/created/entry).
  3. Atomic rename into `~/artifacts/<date>/<slug>/` → no half-uploaded artifacts.
  4. Print the URL, e.g. `http://mac-mini:8787/2026-08-05/proto-name/` (works via Tailscale MagicDNS too).
- The skill instructs agents: after building a prototype/plan/site, publish and hand back the URL. Type-agnostic — HTML renders in browser, markdown plans served raw (or optionally rendered).

### Security & access
- Assumes a trusted network (Tailscale/LAN) → plain HTTP, no auth. Default bind `0.0.0.0`; tighten to a specific Tailscale IP if you want LAN-off.
- Optional later: Basic Auth via env var, or front with Caddy.

### Reliability properties
- One process, no DB, no build step, no external deps. Artifacts are plain files → back up with rsync.
- launchd keeps it alive; publish is atomic; index is dynamic.

### Decisions (resolved during implementation)
1. **Dynamic index** — home page is generated per request by scanning the artifacts directory; no static index build step.
2. **Auth** — off by default; optional Basic Auth via `"auth": {"username", "password"}` in `~/.artifacts/config.json`.
3. **Markdown** — rendered to HTML at publish time by the CLI (small vendored renderer); raw `.md` kept alongside; `--no-render` serves raw.
4. **Bind** — `0.0.0.0` by default (LAN + Tailscale); pin to a Tailscale IP via `"host"` in config to go LAN-off.
5. **Home page** — mobile-first day feed: day headers with counts, type filter pills, search, big touch targets. Neutrals use opencode's OC-2 theme palette. Light/dark toggle persisted in localStorage, defaults to system preference.
6. **Archive** — each row has a ghost `⋮` button (no outline) opening a compact context menu with a single action: Archive (active rows) / Restore (archived rows). Archived rows leave the default feed and appear only under the "Archived" pill; active all/type counts exclude them. Actions POST to `/api/artifacts/archive` (sets `archived`/`archived_at` in `artifact.json`, same optional Basic Auth) and pop a bottom snackbar with a Revert button that undoes the last action.
7. **Artifact chrome bar** — HTML artifact routes are served in a lightweight outer shell with the chrome bar outside an iframe; the original artifact HTML is loaded unchanged via an internal `__ap_embed=1` route. The shell provides a back link to `/`, type badge, title, created date/time + entry, and an Archive/Restore button that POSTs to the same `/api/artifacts/archive` endpoint. This keeps arbitrary artifact CSS and layout behavior isolated; the home page is unaffected.
8. **Sketch vs exact-spec pages** — the publish skill forces a shape choice before publish. *Exact-spec* (faithful UI / interactive prototype) ships as-is. *Sketch* (screenshot galleries, annotated flows, plan HTML) must use a centered readable column / gallery grid so content isn’t a left strip on wide desktops. Guidelines live in `skill/SKILL.md`.
9. **Live home feed** — the home page polls `GET /api/artifacts.json` every 4s (and on tab focus) and rebuilds the feed when the list changes, preserving filter/search. No websockets; stdlib-only.
10. **Remote publish** — `POST /api/artifacts` accepts a raw body (tar.gz/zip archive or single file) with `title`/`type`/`desc`/`entry` in the query string; stages it, writes `artifact.json`, and atomic-renames into place (same flow as the CLI, `source: "remote"`). Same optional Basic Auth. The skill instructs agents on other machines to publish via `curl --data-binary`, so a networked agent with only the skill and curl can publish — no CLI install required.

### Layout & files
- `server.py` — the server (stdlib only)
- `publish.py` — the CLI (symlinked to `publish-artifact` on PATH)
- `install.sh` — idempotent installer (CLI, config, skill, LaunchAgent)
- `skill/SKILL.md` — global skill, installed to `~/.agents/skills/publish-artifact/SKILL.md`
- Service: `com.<user>.artifact-server` under launchd, logs to `~/Library/Logs/artifact-server.log`
- Config: `~/.artifacts/config.json`; artifacts in `~/artifacts/<date>/<slug>/`
- URL: `http://<hostname>.local:8787/` (home), `publish-artifact` prints per-artifact URLs

### Usage
```
publish-artifact <src> --title "T" --type prototype|plan|site --desc "D"
```
