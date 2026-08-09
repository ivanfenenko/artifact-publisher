---
name: publish-artifact
description: Publish a built artifact (HTML prototype, plan doc, static site, etc.) from this machine — or over HTTP from another machine on the LAN/Tailscale — to the artifact server so it can be opened from a browser. Use after building anything the user might want to view remotely.
---

# publish-artifact

Make whatever you just built viewable remotely through the artifact server.

## When to use
After building a prototype, an HTML plan/mockup, a static site, or any other self-contained artifact the user may want to open in a browser — especially on a headless/remote machine where they can't open a preview locally. Ask the user, or publish directly if they asked for it.

## Before you publish — pick a page shape

Every HTML artifact is one of two shapes. Pick explicitly before writing or shipping the page.

### Exact-spec
The page *is* the thing under evaluation: a faithful UI mock, app screen, interactive prototype, or multi-page site whose layout is the content.

- Publish **as-is**. Do not wrap it in a doc shell, center column, or “readable page” chrome.
- Full viewport is fine. Mobile frames stay at their designed width.
- `--type prototype` or `site` is usual.

### Sketch
Everything else: screenshot galleries, annotated flows, plan write-ups, comparison boards, throwaway HTML that *presents* content rather than *being* a UI.

- Must be **readable on a wide desktop and on a phone**.
- Follow [Sketch page guidelines](#sketch-page-guidelines) below before publishing.
- `--type plan` is usual; `other` if it doesn’t fit.

If unsure: if someone would judge pixel layout / interaction, it’s exact-spec; if someone would read or scan it, it’s a sketch.

## Sketch page guidelines

Sketches are viewed on large monitors over Tailscale. Left-flush narrow content on a 1400px+ viewport is a failed sketch.

### Required layout
1. **Centered column** — one main column, `margin: 0 auto`, never stuck to the left edge on wide screens.
2. **Max-width by content**:
   - Prose / plans: `max-width: 42rem` (≈672px)
   - Screenshot or media gallery: `max-width: 72rem` (≈1152px), or a responsive grid of cards
   - Side-by-side comparisons: grid that wraps; each pane has its own max width
3. **Page padding** — at least `24px` on the sides (`padding: 32px 24px` or similar). Safe on mobile.
4. **Viewport meta** — `<meta name="viewport" content="width=device-width, initial-scale=1">`.

### Type and media
5. **Readable type** — system UI stack; body ≥15px; line-length comfortable inside the column (don’t stretch prose to 1200px).
6. **Images** — `max-width: 100%; height: auto; display: block`. Phone screenshots may cap around `390–430px` **within the centered column or grid cell**, not as the only width constraint on an otherwise full-bleed left-aligned body.
7. **Spacing** — clear section gaps (≈24–32px); headings tied to the block they introduce.

### Minimal sketch shell (use or match)

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>…</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #121212; color: #ededed;
    padding: 32px 24px 64px;
    line-height: 1.5;
  }
  .page { max-width: 42rem; margin: 0 auto; }          /* prose */
  /* .page { max-width: 72rem; margin: 0 auto; } */   /* gallery */
  .page img { max-width: 100%; height: auto; display: block; }
</style>
</head>
<body>
  <main class="page">
    <!-- content -->
  </main>
</body>
</html>
```

For a screenshot gallery, prefer a grid inside `.page` (e.g. `display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 24px`) so shots share the row on wide screens instead of a single left stack.

### Anti-patterns (sketch)
- Body content with no `max-width` + no centering (wide-screen left strip).
- `max-width` only on images while headings/text stay full-bleed left.
- Forgetting the viewport meta.
- Treating a screenshot dump as exact-spec and skipping the shell.

### Anti-patterns (exact-spec)
- Wrapping a real UI prototype in `.page { max-width: 42rem }` “for readability”.
- Adding sketch chrome that changes the layout being judged.

## How to publish
1. Ensure the CLI is on PATH (`install.sh` symlinks it to `~/bin/publish-artifact`). If missing, run the repo’s `install.sh`.
2. Confirm page shape (sketch vs exact-spec). For sketches, apply the guidelines above — fix the HTML before publishing if it fails them.
3. Source must be **self-contained** (single file or folder with assets). Inline or bundle external assets first.
4. Run:
   ```
   publish-artifact <src> \
     --title "Short descriptive title" \
     --type prototype|plan|site|other \
     --desc "One-line description"
   ```
   - `<src>` can be a single `.html` / `.md` file or a directory.
   - `--type` guess: `prototype` for interactive demos/UI (often exact-spec), `plan` for docs/sketches, `site` for multi-page sites.
   - `.md` files are auto-rendered to HTML; pass `--no-render` to serve raw markdown.
5. The command prints a URL. Always paste it in your final message so the user can click it.

**Completion check before handing over the URL:** open the shape decision again — if sketch, would this still be readable on a 1440px desktop? If not, fix and republish.

## Publishing from another machine (no CLI installed)

If you're not on the server machine and `publish-artifact` isn't available, push over HTTP with `curl` — the server accepts the artifact body directly.

### Finding the server address (do this first)

Resolve the server base URL in this order:

1. **`ARTIFACT_SERVER` env var** — if set, use it directly (e.g. `http://mac-mini:8787`). It may also come from a project config or the user.
2. **mDNS discovery** — on macOS, browse Bonjour for the service:
   ```
   dns-sd -B _artifactserver._tcp local.
   ```
   Look for an instance named `artifact-server`; that machine is the server. Resolve it to a hostname with:
   ```
   dns-sd -L artifact-server _artifactserver._tcp local.
   ```
   The host line gives the hostname to use (`<host>.local`), then use `http://<host>.local:8787`.
3. **Ask the user** — if discovery turns up nothing, ask the user for the server address or hostname. Do not guess a random hostname.

### Publishing

**Single file** — pass it as the request body, `entry` names it inside the artifact:
```
curl -X POST --data-binary @plan.md \
  "http://mac-mini:8787/api/artifacts?title=My%20Plan&type=plan&desc=...&entry=plan.md"
```

**Directory** — tar it with the folder *contents* at the archive root, then upload:
```
tar -czf /tmp/art.tgz -C <dir> .
curl -X POST --data-binary @/tmp/art.tgz \
  "http://mac-mini:8787/api/artifacts?title=My%20Site&type=site&desc=..."
```

The response is JSON: `{"ok": true, "url": "http://mac-mini:8787/2026-08-09/my-site/", ...}`. Paste the `url` value in your final message.

- All the same rules apply: sketch-vs-exact-spec first, self-contained source, `.md` auto-renders unless you add `&no_render=1`.
- If the server has Basic Auth enabled, add `-u user:pass`.
- The same URL also works over Tailscale using the server's MagicDNS name.

## Conventions
- If the artifact needs an obvious name and the user didn't give one, derive the title from the content (e.g. the `<title>` of an HTML file).
- Never publish secrets, credentials, or large binaries unless asked.
- Artifacts are served over the LAN / Tailscale with no auth by default — keep anything sensitive out.

## Home page
The server's root (`http://<hostname>.local:8787/`) lists all published artifacts grouped by date, newest first. If you only have a URL for an artifact, you can also tell the user to open the home page and find it there.
