#!/usr/bin/env python3
"""artifact-server — single-file stdlib server for published artifacts.

Serves a workbench-style home page (stat chips, filter pills, search,
light/dark themes) plus individual artifacts from a flat directory. No
dependencies beyond the Python standard library.

Endpoints:
  GET /                        -> home page (artifacts newest first)
  GET /<date>/<slug>/          -> artifact entry (e.g. index.html)
  GET /<date>/<slug>/<file..>  -> static file inside an artifact
  GET /api/artifacts.json      -> machine-readable artifact list
  POST /api/artifacts/archive  -> set archived flag {"date","slug","archived"}
  POST /api/artifacts          -> publish an artifact (raw body: tarball, zip, or file)
  GET /healthz                 -> "ok"
"""

import argparse
import base64
import html
import io
import json
import mimetypes
import os
import re
import shutil
import tarfile
import tempfile
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

MAX_UPLOAD = 200 * 1024 * 1024
TYPES = {"prototype", "plan", "site", "other"}

CONFIG_PATH = Path.home() / ".artifacts" / "config.json"

DEFAULTS = {
    "host": "0.0.0.0",
    "port": 8787,
    "artifacts_root": str(Path.home() / "artifacts"),
    "auth": None,
    "public_base": f"http://{Path.home().name}-mac.local:8787",
}

STYLE = """
:root, [data-theme="dark"] {
  --bg: #161616; --surface: #1C1C1C; --surface-2: #232323; --border: #282828;
  --text: #EDEDED; --muted: #A0A0A0; --faint: #707070; --hover: #232323;
  --accent: #6ea8fe; --accent-soft: rgba(110,168,254,.14);
  --proto-bg: rgba(110,168,254,.16);  --proto-fg: #9cc4ff; --proto-bd: rgba(110,168,254,.4);
  --plan-bg:  rgba(86,200,143,.16);   --plan-fg:  #7fd6a8; --plan-bd:  rgba(86,200,143,.4);
  --site-bg:  rgba(177,140,255,.16);  --site-fg:  #c3a6ff; --site-bd:  rgba(177,140,255,.4);
  --other-bg: rgba(154,163,178,.14);  --other-fg: #aeb6c4; --other-bd: rgba(154,163,178,.35);
  color-scheme: dark;
}
[data-theme="light"] {
  --bg: #F8F8F8; --surface: #FFFFFF; --surface-2: #F3F3F3; --border: #DBDBDB;
  --text: #171717; --muted: #6F6F6F; --faint: #8F8F8F; --hover: #EDEDED;
  --accent: #2563eb; --accent-soft: rgba(37,99,235,.08);
  --proto-bg: #e3efff; --proto-fg: #1d5fbf; --proto-bd: #b6d2fa;
  --plan-bg:  #e2f7ea; --plan-fg:  #1c7a41; --plan-bd:  #b6e4c8;
  --site-bg:  #f0e9ff; --site-fg:  #6b35c0; --site-bd:  #d6c2f7;
  --other-bg: #eceef2; --other-fg: #555d6a; --other-bd: #d7dbe2;
  color-scheme: light;
}
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
body {
  margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: var(--bg); color: var(--text); line-height: 1.5;
}
.shell { min-height: 100vh; padding-bottom: calc(2rem + env(safe-area-inset-bottom)); }

/* sticky header */
.top {
  position: sticky; top: 0; z-index: 20;
  background: rgba(22,22,22,.88); backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
  border-bottom: 1px solid var(--border);
  padding: calc(.6rem + env(safe-area-inset-top)) .9rem .7rem;
}
[data-theme="light"] .top { background: rgba(248,248,248,.88); }
.topbar { display: flex; align-items: center; justify-content: space-between; gap: .6rem; }
.topbar h1 { margin: 0; font-size: 1.15rem; font-weight: 700; display: flex; align-items: center; gap: .5rem; }
.topbar h1 .mark { color: var(--accent); }
.theme-btn {
  min-width: 44px; min-height: 44px; display: grid; place-items: center; border-radius: 10px;
  background: var(--surface); border: 1px solid var(--border); color: var(--text);
  font-size: 1.1rem; cursor: pointer; padding: 0;
}
.theme-btn:active { transform: scale(.96); }
.search { margin-top: .6rem; position: relative; }
.search input {
  width: 100%; min-height: 46px; border-radius: 12px; border: 1px solid var(--border);
  background: var(--surface); color: var(--text); padding: 0 .9rem 0 2.4rem;
  font-size: 16px; outline: none;
}
.search input:focus { border-color: var(--accent); }
.search input::placeholder { color: var(--faint); }
.search .glyph { position: absolute; left: .85rem; top: 50%; transform: translateY(-50%); color: var(--faint); pointer-events: none; }

/* scrollable filter pills */
.pills { display: flex; gap: .45rem; margin-top: .6rem; overflow-x: auto; padding-bottom: .2rem; scrollbar-width: none; }
.pills::-webkit-scrollbar { display: none; }
.pill {
  flex: none; min-height: 40px; padding: 0 1rem; border-radius: 999px; cursor: pointer;
  border: 1px solid var(--border); background: var(--surface); color: var(--muted); font-size: .9rem;
  display: inline-flex; align-items: center; gap: .4rem;
}
.pill:active { transform: scale(.97); }
.pill.on { background: var(--accent); border-color: var(--accent); color: #fff; font-weight: 600; }
.pill .cnt { font-size: .72rem; opacity: .75; }

.type-badge {
  font-size: .64rem; font-weight: 700; text-transform: uppercase; letter-spacing: .08em;
  padding: .2rem .55rem; border-radius: 999px; white-space: nowrap; border: 1px solid transparent;
}
.type-prototype { background: var(--proto-bg); color: var(--proto-fg); border-color: var(--proto-bd); }
.type-plan      { background: var(--plan-bg);  color: var(--plan-fg);  border-color: var(--plan-bd); }
.type-site      { background: var(--site-bg);  color: var(--site-fg);  border-color: var(--site-bd); }
.type-other     { background: var(--other-bg); color: var(--other-fg); border-color: var(--other-bd); }

/* day feed */
.feed { max-width: 1220px; margin: 0 auto; padding: .3rem 2.2rem 1.5rem; }
.dayhead {
  display: flex; align-items: center; gap: .8rem; margin: 1.2rem 0 .2rem;
  font-size: .72rem; font-weight: 700; text-transform: uppercase; letter-spacing: .1em; color: var(--accent);
}
.dayhead .rule { flex: 1; height: 1px; background: var(--border); }
.entry {
  display: block; text-decoration: none; color: inherit;
  display: flex; flex-direction: column; gap: .35rem; min-height: 86px; padding: .8rem 3rem .8rem .2rem;
  border-bottom: 1px solid var(--border);
}
.entry:active { background: var(--hover); }
.entry .e-top { display: flex; align-items: center; gap: .55rem; }
.entry .ttl { font-size: 1.05rem; font-weight: 650; line-height: 1.3; }
.entry .dsc { color: var(--muted); font-size: .85rem; }
.entry .e-meta { color: var(--faint); font-size: .72rem; display: flex; gap: .6rem; align-items: center; }
.empty { color: var(--muted); text-align: center; padding: 4rem 0; }

/* archived state — visible only in the Archived view */
.arch-chip {
  font-size: .6rem; font-weight: 700; text-transform: uppercase; letter-spacing: .08em;
  color: var(--faint); border: 1px solid var(--border); border-radius: 999px; padding: .15rem .5rem; flex: none;
}
.entry.archived .ttl { color: var(--muted); }
.entry.archived .dsc { opacity: .55; }

/* ghost ⋮ menu button */
.dots {
  position: absolute; top: .65rem; right: .1rem; min-width: 40px; min-height: 40px;
  display: inline-flex; align-items: center; justify-content: center;
  border: none; background: transparent; color: var(--faint);
  cursor: pointer; font-size: 1.3rem; letter-spacing: -2px; padding: 0; flex: none; border-radius: 10px;
}
.dots:hover { color: var(--text); background: transparent; }
.dots:active { transform: scale(.94); }

/* context menu — one compact action */
.rowwrap { position: relative; }
.menu {
  position: absolute; right: .1rem; top: 2.5rem; z-index: 40; min-width: 156px;
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  box-shadow: 0 12px 32px rgba(0,0,0,.45); padding: .2rem; display: none;
}
.menu.open { display: block; }
.menu .mi {
  display: flex; align-items: center; gap: .5rem; width: 100%; border: none; background: none;
  color: var(--text); font-size: .85rem; min-height: 34px; padding: 0 .7rem; border-radius: 7px; cursor: pointer; text-align: left;
}
.menu .mi:hover { background: var(--hover); }
.menu .mi.restore { color: var(--plan-fg); }

/* snackbar with Revert */
#snackbar {
  position: fixed; left: 50%; transform: translateX(-50%) translateY(16px); z-index: 70;
  bottom: calc(16px + env(safe-area-inset-bottom));
  display: flex; align-items: center; gap: 1rem; max-width: min(560px, calc(100vw - 32px));
  background: #1a1a1a; color: #EDEDED; border: 1px solid #3a3a3a; border-radius: 14px;
  padding: .6rem .7rem .6rem 1rem; font-size: .88rem; box-shadow: 0 14px 40px rgba(0,0,0,.5);
  opacity: 0; pointer-events: none; transition: opacity .2s ease, transform .2s ease;
}
#snackbar.show { opacity: 1; pointer-events: auto; transform: translateX(-50%) translateY(0); }
#snackbar .msg { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#snackbar .rev {
  flex: none; min-height: 36px; padding: 0 .85rem; border-radius: 9px; cursor: pointer;
  border: 1px solid var(--accent); background: var(--accent-soft); color: var(--accent);
  font-size: .82rem; font-weight: 700; letter-spacing: .02em;
}
#snackbar .rev:hover { background: var(--accent); color: #fff; }
#snackbar .rev:active { transform: scale(.96); }
[data-theme="light"] #snackbar { background: #ffffff; color: #171717; border-color: #d7dbe2; box-shadow: 0 14px 40px rgba(0,0,0,.16); }

@media (max-width: 639px) {
  .feed { padding-left: .9rem; padding-right: .9rem; }
}
"""


def load_config(path=CONFIG_PATH):
    cfg = dict(DEFAULTS)
    if Path(path).exists():
        try:
            cfg.update(json.loads(Path(path).read_text()))
        except Exception:
            pass
    cfg["artifacts_root"] = os.path.expanduser(cfg["artifacts_root"])
    return cfg


def load_artifacts(root: Path):
    artifacts = []
    if root.exists():
        for date_dir in root.iterdir():
            if not date_dir.is_dir() or not re.match(r"^\d{4}-\d{2}-\d{2}$", date_dir.name):
                continue
            for slug_dir in date_dir.iterdir():
                if not slug_dir.is_dir():
                    continue
                info = {"date": date_dir.name, "slug": slug_dir.name, "archived": False}
                meta = slug_dir / "artifact.json"
                if meta.exists():
                    try:
                        info.update(json.loads(meta.read_text()))
                    except Exception:
                        pass
                artifacts.append(info)
    artifacts.sort(key=lambda a: a.get("created") or a["slug"], reverse=True)
    return artifacts


def slugify(name):
    name = re.sub(r"\s+", "-", name.strip().lower())
    name = re.sub(r"[^a-z0-9_-]+", "", name)
    return name or "artifact"


def unique_slug_dir(date_dir, time_str, base):
    for i in range(1, 1000):
        name = f"{time_str}-{base}" if i == 1 else f"{time_str}-{i}-{base}"
        if not (date_dir / name).exists():
            return name
    raise RuntimeError("could not find unique slug")


def pick_entry(artifact_dir: Path):
    candidates = ["index.html", "index.htm"]
    for c in candidates:
        if (artifact_dir / c).is_file():
            return c
    htmls = sorted(artifact_dir.glob("*.html"))
    if htmls:
        return htmls[0].name
    mds = sorted(artifact_dir.glob("*.md"))
    if mds:
        return mds[0].name
    return None


def check_archive_member(name: str):
    parts = Path(name).parts
    if name.startswith("/") or ".." in parts:
        raise ValueError(f"unsafe path in archive: {name!r}")


def extract_upload(body: bytes, dest: Path, filename: str):
    """Extract a raw upload into dest: tarball/zip if it is one, else a single file."""
    dest.mkdir(parents=True, exist_ok=True)
    lower = filename.lower()
    if lower.endswith((".tar.gz", ".tgz")) or (body[:2] == b"\x1f\x8b"):
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tf:
            for m in tf.getmembers():
                check_archive_member(m.name)
            tf.extractall(dest)
    elif lower.endswith(".zip") or body[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            for name in zf.namelist():
                check_archive_member(name)
            zf.extractall(dest)
    else:
        name = Path(filename).name or "index.html"
        (dest / name).write_bytes(body)


def inline_md(text):
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', text)
    return text


def render_markdown(text):
    text = html.escape(text)
    lines = text.splitlines()
    out = []
    i = 0
    n = len(lines)
    in_code = False
    code_lines = []
    open_lists = []

    def close_lists():
        for tag, _ in reversed(open_lists):
            out.append(f"</{tag}>")
        open_lists.clear()

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not in_code and stripped.startswith("```"):
            in_code = True
            code_lines = []
            i += 1
            continue
        if in_code:
            if stripped.startswith("```"):
                in_code = False
                out.append("<pre><code>" + "\n".join(code_lines) + "</code></pre>")
            else:
                code_lines.append(line)
            i += 1
            continue

        if not stripped:
            close_lists()
            out.append("")
            i += 1
            continue

        if stripped == "---":
            close_lists()
            out.append("<hr>")
            i += 1
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            close_lists()
            level = len(m.group(1))
            out.append(f"<h{level}>{inline_md(m.group(2))}</h{level}>")
            i += 1
            continue

        m = re.match(r"^([>*])\s+(.*)$", stripped)
        if m and m.group(1) == ">":
            quote_lines = []
            while i < n and lines[i].strip().startswith(">"):
                quote_lines.append(inline_md(re.sub(r"^>\s?", "", lines[i].strip())))
                i += 1
            close_lists()
            out.append("<blockquote>" + "<br>".join(quote_lines) + "</blockquote>")
            continue

        m = re.match(r"^([-*])\s+(.*)$", stripped)
        if m:
            close_lists()
            items = [inline_md(m.group(2))]
            i += 1
            while i < n and re.match(r"^\s*[-*]\s+", lines[i]):
                items.append(inline_md(re.sub(r"^\s*[-*]\s+", "", lines[i])))
                i += 1
            out.append("<ul>" + "".join(f"<li>{it}</li>" for it in items) + "</ul>")
            continue

        m = re.match(r"^\d+\.\s+(.*)$", stripped)
        if m:
            close_lists()
            items = [inline_md(m.group(1))]
            i += 1
            while i < n and re.match(r"^\s*\d+\.\s+", lines[i]):
                items.append(inline_md(re.sub(r"^\s*\d+\.\s+", "", lines[i])))
                i += 1
            out.append("<ol>" + "".join(f"<li>{it}</li>" for it in items) + "</ol>")
            continue

        para = [inline_md(stripped)]
        i += 1
        while i < n and lines[i].strip() and not re.match(r"^(#{1,6}\s|[-*>]\s|\d+\.\s|```)", lines[i].strip()):
            para.append(inline_md(lines[i].strip()))
            i += 1
        out.append("<p>" + " ".join(para) + "</p>")

    if in_code:
        out.append("<pre><code>" + "\n".join(code_lines) + "</code></pre>")
    close_lists()
    return "\n".join(out) + "\n"


def render_markdown_upload(artifact_dir: Path, entry: str):
    """Render a .md entry to HTML next to it, returning the new entry name."""
    md_path = artifact_dir / entry
    if not md_path.is_file() or not entry.lower().endswith(".md"):
        return entry
    text = md_path.read_text(encoding="utf-8")
    html_name = Path(entry).stem + ".html"
    (artifact_dir / html_name).write_text(render_markdown(text), encoding="utf-8")
    return html_name


def artifact_link(a):
    return f"/{a['date']}/{a['slug']}/"


def day_label(date_str):
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return date_str
    today = datetime.now().date()
    delta = (today - d).days
    if delta == 0:
        rel = "Today"
    elif delta == 1:
        rel = "Yesterday"
    else:
        rel = d.strftime("%A")
    return f"{rel} · {d.strftime('%b')} {d.day}"


def render_entry(a):
    title = html.escape(str(a.get("title") or a["slug"]))
    desc = html.escape(str(a.get("description") or ""))
    type_ = html.escape(str(a.get("type") or "other"))
    archived = bool(a.get("archived"))
    created = str(a.get("created") or "")
    time_ = ""
    if len(created) >= 16:
        try:
            time_ = datetime.strptime(created[:16], "%Y-%m-%dT%H:%M").strftime("%H:%M")
        except ValueError:
            pass
    date_attr = html.escape(str(a["date"]))
    slug_attr = html.escape(str(a["slug"]))
    arch_state = "1" if archived else "0"
    arch_cls = " archived" if archived else ""
    chip = '<span class="arch-chip">archived</span>' if archived else ""
    action = "Restore" if archived else "Archive"
    icon = "&#8634;" if archived else "&#128229;"
    mi_cls = " restore" if archived else ""
    return f"""<div class="rowwrap"><a class="entry{arch_cls}" href="{artifact_link(a)}" data-type="{type_}" data-archived="{arch_state}" data-date="{date_attr}" data-slug="{slug_attr}">
  <div class="e-top"><span class="type-badge type-{type_}">{type_}</span><span class="e-meta">{time_}</span>{chip}
  </div>
  <div class="ttl">{title}</div>
  <div class="dsc">{desc}</div>
</a>
<button class="dots" data-date="{date_attr}" data-slug="{slug_attr}" aria-label="More">&#8942;</button>
<div class="menu"><button class="mi{mi_cls}" data-date="{date_attr}" data-slug="{slug_attr}" data-act="{action.lower()}">{icon}&nbsp; {action}</button></div>
</div>"""


def render_home(artifacts):
    types = ["prototype", "plan", "site", "other"]
    counts = {t: 0 for t in types}
    archived_count = 0
    for a in artifacts:
        if a.get("archived"):
            archived_count += 1
        t = str(a.get("type") or "other")
        if t in counts and not a.get("archived"):
            counts[t] += 1
    pills = []
    for t in ["all"] + types:
        cnt = sum(1 for a in artifacts if not a.get("archived")) if t == "all" else counts.get(t, 0)
        pills.append(f'<button class="pill{" on" if t == "all" else ""}" data-f="{t}" data-cnt="{cnt}">{t.title()} <span class="cnt">{cnt}</span></button>')
    pills.append(f'<button class="pill" data-f="archived" data-cnt="{archived_count}">Archived <span class="cnt">{archived_count}</span></button>')
    pills = "".join(pills)
    feed = []
    last_day = None
    for a in artifacts:
        day = day_label(str(a.get("date") or ""))
        if day != last_day:
            feed.append(f'<div class="dayhead">{html.escape(day)}<span class="rule"></span></div>')
            last_day = day
        feed.append(render_entry(a))
    if not feed:
        feed = ['<div class="empty">No artifacts yet. Publish one with <code>publish-artifact</code>.</div>']
    return f"""<!doctype html>
<html lang="en" data-theme="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Artifacts</title><style>{STYLE}</style></head>
<body><div class="shell">
<div class="top">
  <div class="topbar">
    <h1><span class="mark">&#9670;</span> Artifacts</h1>
    <button class="theme-btn" id="themeBtn" aria-label="Toggle theme">&#9788;</button>
  </div>
  <div class="search"><span class="glyph">&#128269;</span><input type="search" id="search" placeholder="Search artifacts…"></div>
  <div class="pills" id="pills">{pills}</div>
</div>
 <div class="feed" id="items">{''.join(feed)}<div class="empty" id="empty" style="display:none"></div></div>
</div>
<div id="snackbar"><span class="msg" id="snackMsg"></span><button class="rev" id="snackRevert">Revert</button></div>
<script>
(function(){{
  var saved = null; try {{ saved = localStorage.getItem("artifacts-theme"); }} catch (e) {{}}
  var dark = saved ? saved !== "light" : !window.matchMedia("(prefers-color-scheme: light)").matches;
  var root = document.documentElement; root.dataset.theme = dark ? "dark" : "light";
  var btn = document.getElementById("themeBtn");
  btn.innerHTML = dark ? "&#9788;" : "&#9789;";
  btn.addEventListener("click", function(){{
    dark = !dark; root.dataset.theme = dark ? "dark" : "light";
    btn.innerHTML = dark ? "&#9788;" : "&#9789;";
    try {{ localStorage.setItem("artifacts-theme", dark ? "dark" : "light"); }} catch (e) {{}}
  }});

  var curF = "all", curQ = "";
  var items = document.getElementById("items");
  var lastAction = null;

  function entryVisible(r){{
    var arch = r.dataset.archived === "1";
    var byFilter = curF === "archived" ? arch : !arch && (curF === "all" || r.dataset.type === curF);
    var bySearch = !curQ || r.textContent.toLowerCase().indexOf(curQ) !== -1;
    return byFilter && bySearch;
  }}
  function hideEmptyDays() {{
    document.querySelectorAll(".dayhead").forEach(function(d){{
      var n = d.nextElementSibling, any = false;
      while (n && n.classList && !n.classList.contains("dayhead")) {{
        if (n.style.display !== "none") any = true;
        n = n.nextElementSibling;
      }}
      d.style.display = any ? "" : "none";
    }});
  }}
  function apply(){{
    var visible = 0;
    items.querySelectorAll(".rowwrap").forEach(function(row){{
      var r = row.querySelector(".entry");
      var shown = entryVisible(r);
      row.style.display = shown ? "" : "none";
      if (shown) visible++;
    }});
    hideEmptyDays();
    var empty = document.getElementById("empty");
    if (!empty) return;
    empty.style.display = visible ? "none" : "";
    if (visible) return;
    empty.textContent = curQ ? "No matching artifacts." : curF === "archived" ? "No archived artifacts." : "No active artifacts.";
  }}
  function setCnt(f, delta){{
    var p = document.querySelector('.pill[data-f="' + f + '"]');
    if (!p) return;
    var c = parseInt(p.dataset.cnt, 10) + delta;
    p.dataset.cnt = c;
    p.querySelector(".cnt").textContent = c;
  }}

  function updateCounts(type, archived){{
    setCnt("all", archived ? -1 : 1);
    setCnt(type, archived ? -1 : 1);
    setCnt("archived", archived ? 1 : -1);
  }}

  function setArchived(date, slug, archived, done){{
    fetch("/api/artifacts/archive", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{ date: date, slug: slug, archived: archived }})
    }}).then(function(r){{ return r.json(); }}).then(function(res){{
      if (!res.ok) {{ alert("archive failed: " + (res.error || "unknown error")); return; }}
      fingerprint = null; /* next poll re-baselines without rebuild */
      done();
    }}).catch(function(){{ alert("archive request failed"); }});
  }}

  function updateRow(entry, archived){{
    entry.dataset.archived = archived ? "1" : "0";
    entry.classList.toggle("archived", archived);
    var meta = entry.querySelector(".e-meta");
    var chip = entry.querySelector(".arch-chip");
    if (archived && !chip) meta.insertAdjacentHTML("afterend", '<span class="arch-chip">archived</span>');
    else if (!archived && chip) chip.remove();
    var mi = entry.parentElement.querySelector(".menu .mi");
    mi.dataset.act = archived ? "restore" : "archive";
    mi.classList.toggle("restore", archived);
    mi.innerHTML = (archived ? "&#8634;" : "&#128229;") + "&nbsp; " + (archived ? "Restore" : "Archive");
  }}

  function snack(msg){{
    var sb = document.getElementById("snackbar"), m = document.getElementById("snackMsg"), r = document.getElementById("snackRevert");
    m.textContent = msg;
    r.style.display = lastAction ? "" : "none";
    sb.classList.add("show");
    clearTimeout(snack._t);
    snack._t = setTimeout(function(){{ sb.classList.remove("show"); }}, 4500);
  }}

  function findEntry(date, slug){{
    var out = null;
    items.querySelectorAll(".entry").forEach(function(r){{
      if (r.dataset.date === date && r.dataset.slug === slug) out = r;
    }});
    return out;
  }}

  function doToggle(date, slug, entry, becoming, verb){{
    setArchived(date, slug, becoming, function(){{
      lastAction = {{ date: date, slug: slug, archived: becoming }};
      var ttl = entry.querySelector(".ttl").textContent;
      updateRow(entry, becoming);
      updateCounts(entry.dataset.type, becoming);
      apply();
      snack(verb + " “" + ttl + "”");
    }});
  }}

  var pills = document.getElementById("pills");
  pills.querySelectorAll("[data-f]").forEach(function(p){{
    p.addEventListener("click", function(){{
      pills.querySelectorAll("[data-f]").forEach(function(x){{ x.classList.toggle("on", x === p); }});
      curF = p.dataset.f;
      apply();
    }});
  }});
  var search = document.getElementById("search");
  search.addEventListener("input", function(){{
    curQ = search.value.toLowerCase();
    apply();
  }});

  /* context menu: open on ⋮, act on menu item */
  items.addEventListener("click", function(ev){{
    var dots = ev.target.closest ? ev.target.closest(".dots") : null;
    if (dots) {{
      ev.preventDefault(); ev.stopPropagation();
      var menu = dots.closest(".rowwrap").querySelector(".menu");
      items.querySelectorAll(".menu.open").forEach(function(m){{ if (m !== menu) m.classList.remove("open"); }});
      menu.classList.toggle("open");
      return;
    }}
    var mi = ev.target.closest ? ev.target.closest(".mi") : null;
    if (mi) {{
      ev.preventDefault(); ev.stopPropagation();
      var entry = mi.closest(".rowwrap").querySelector(".entry");
      var becoming = entry.dataset.archived !== "1";
      doToggle(mi.dataset.date, mi.dataset.slug, entry, becoming, becoming ? "Archived" : "Restored");
    }}
  }});
  document.addEventListener("click", function(ev){{
    if (!ev.target.closest || !ev.target.closest(".rowwrap")) {{
      items.querySelectorAll(".menu.open").forEach(function(m){{ m.classList.remove("open"); }});
    }}
  }});

  /* snackbar Revert */
  document.getElementById("snackRevert").addEventListener("click", function(){{
    if (!lastAction) return;
    var la = lastAction; lastAction = null;
    var entry = findEntry(la.date, la.slug);
    if (!entry) return;
    setArchived(la.date, la.slug, !la.archived, function(){{
      var ttl = entry.querySelector(".ttl").textContent;
      updateRow(entry, !la.archived);
      updateCounts(entry.dataset.type, !la.archived);
      apply();
      document.getElementById("snackbar").classList.remove("show");
      snack("Reverted “" + ttl + "”");
    }});
  }});
  apply();

  /* live refresh: poll artifacts.json and rebuild feed when list changes */
  var fingerprint = null;
  var pollBusy = false;
  function esc(s){{
    return String(s == null ? "" : s).replace(/[&<>"']/g, function(c){{
      return ({{"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}})[c];
    }});
  }}
  function dayLabel(dateStr){{
    var m = /^(\\d{{4}})-(\\d{{2}})-(\\d{{2}})$/.exec(dateStr || "");
    if (!m) return dateStr || "";
    var d = new Date(+m[1], +m[2] - 1, +m[3]);
    var today = new Date(); today.setHours(0,0,0,0);
    var delta = Math.round((today - d) / 86400000);
    var rel = delta === 0 ? "Today" : delta === 1 ? "Yesterday" : d.toLocaleDateString(undefined, {{ weekday: "long" }});
    var mon = d.toLocaleDateString(undefined, {{ month: "short" }});
    return rel + " · " + mon + " " + d.getDate();
  }}
  function entryTime(created){{
    var s = String(created || "");
    if (s.length < 16) return "";
    var m = /^(\\d{{4}})-(\\d{{2}})-(\\d{{2}})T(\\d{{2}}):(\\d{{2}})/.exec(s);
    return m ? m[4] + ":" + m[5] : "";
  }}
  function renderEntryHtml(a){{
    var title = esc(a.title || a.slug);
    var desc = esc(a.description || "");
    var type = esc(a.type || "other");
    var archived = !!a.archived;
    var date = esc(a.date);
    var slug = esc(a.slug);
    var time = entryTime(a.created);
    var archState = archived ? "1" : "0";
    var archCls = archived ? " archived" : "";
    var chip = archived ? '<span class="arch-chip">archived</span>' : "";
    var action = archived ? "Restore" : "Archive";
    var icon = archived ? "&#8634;" : "&#128229;";
    var miCls = archived ? " restore" : "";
    return '<div class="rowwrap"><a class="entry' + archCls + '" href="/' + date + '/' + slug + '/" data-type="' + type +
      '" data-archived="' + archState + '" data-date="' + date + '" data-slug="' + slug + '">' +
      '<div class="e-top"><span class="type-badge type-' + type + '">' + type + '</span><span class="e-meta">' + time +
      '</span>' + chip + '</div><div class="ttl">' + title + '</div><div class="dsc">' + desc + '</div></a>' +
      '<button class="dots" data-date="' + date + '" data-slug="' + slug + '" aria-label="More">&#8942;</button>' +
      '<div class="menu"><button class="mi' + miCls + '" data-date="' + date + '" data-slug="' + slug +
      '" data-act="' + action.toLowerCase() + '">' + icon + '&nbsp; ' + action + '</button></div></div>';
  }}
  function sig(list){{
    return list.map(function(a){{
      return [a.date, a.slug, a.archived ? 1 : 0, a.created || "", a.title || "", a.type || "", a.description || ""].join("\\0");
    }}).join("\\n");
  }}
  function setPillCnt(f, cnt){{
    var p = document.querySelector('.pill[data-f="' + f + '"]');
    if (!p) return;
    p.dataset.cnt = cnt;
    var c = p.querySelector(".cnt");
    if (c) c.textContent = cnt;
  }}
  function rebuildFeed(artifacts){{
    var types = ["prototype", "plan", "site", "other"];
    var counts = {{ prototype: 0, plan: 0, site: 0, other: 0 }};
    var archivedCount = 0;
    var html = "";
    var lastDay = null;
    artifacts.forEach(function(a){{
      if (a.archived) archivedCount++;
      else {{
        var t = a.type || "other";
        if (counts[t] != null) counts[t]++;
      }}
      var day = dayLabel(a.date || "");
      if (day !== lastDay) {{
        html += '<div class="dayhead">' + esc(day) + '<span class="rule"></span></div>';
        lastDay = day;
      }}
      html += renderEntryHtml(a);
    }});
    if (!artifacts.length) {{
      html = '<div class="empty">No artifacts yet. Publish one with <code>publish-artifact</code>.</div>';
    }}
    html += '<div class="empty" id="empty" style="display:none"></div>';
    items.innerHTML = html;
    setPillCnt("all", artifacts.length - archivedCount);
    types.forEach(function(t){{ setPillCnt(t, counts[t]); }});
    setPillCnt("archived", archivedCount);
    apply();
  }}
  function poll(){{
    if (pollBusy || document.hidden) return;
    pollBusy = true;
    fetch("/api/artifacts.json", {{ cache: "no-store" }}).then(function(r){{ return r.json(); }}).then(function(data){{
      var list = data.artifacts || [];
      var s = sig(list);
      if (fingerprint === null) {{ fingerprint = s; return; }}
      if (s === fingerprint) return;
      fingerprint = s;
      rebuildFeed(list);
    }}).catch(function(){{}}).then(function(){{ pollBusy = false; }});
  }}
  setInterval(poll, 4000);
  document.addEventListener("visibilitychange", function(){{ if (!document.hidden) poll(); }});
  poll();
}})();
</script></body></html>"""


BAR_STYLE = """
#ap-bar{position:fixed!important;top:0!important;left:0!important;right:0!important;z-index:2147483000!important;
  display:flex!important;align-items:center!important;gap:10px!important;width:100vw!important;max-width:100vw!important;
  margin:0!important;padding:8px 12px!important;background:rgba(22,22,22,.92)!important;-webkit-backdrop-filter:blur(10px)!important;
  backdrop-filter:blur(10px)!important;border-bottom:1px solid #2a2a2a!important;box-sizing:border-box!important;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif!important;line-height:1.4!important;
  color:#EDEDED!important;text-align:left!important;}
#ap-bar *{box-sizing:border-box!important;margin:0!important;}
#ap-bar a,#ap-bar button{color:inherit!important;}
#ap-bar .ap-back{flex:none!important;display:inline-flex!important;align-items:center!important;justify-content:center!important;
  width:36px!important;height:36px!important;border-radius:9px!important;border:1px solid #333!important;
  background:rgba(255,255,255,.06)!important;color:#EDEDED!important;text-decoration:none!important;font-size:17px!important;
  cursor:pointer!important;line-height:1!important;transition:background .15s ease!important;}
#ap-bar .ap-back:hover{background:rgba(255,255,255,.14)!important;}
#ap-bar .ap-info{flex:1 1 auto!important;min-width:0!important;display:flex!important;flex-direction:column!important;gap:1px!important;}
#ap-bar .ap-row{display:flex!important;align-items:center!important;gap:8px!important;min-width:0!important;}
#ap-bar .ap-badge{flex:none!important;font-size:10px!important;font-weight:700!important;text-transform:uppercase!important;
  letter-spacing:.08em!important;padding:2px 8px!important;border-radius:999px!important;border:1px solid!important;line-height:1.4!important;}
#ap-bar .ap-badge.prototype{background:rgba(110,168,254,.16)!important;color:#9cc4ff!important;border-color:rgba(110,168,254,.4)!important;}
#ap-bar .ap-badge.plan{background:rgba(86,200,143,.16)!important;color:#7fd6a8!important;border-color:rgba(86,200,143,.4)!important;}
#ap-bar .ap-badge.site{background:rgba(177,140,255,.16)!important;color:#c3a6ff!important;border-color:rgba(177,140,255,.4)!important;}
#ap-bar .ap-badge.other{background:rgba(154,163,178,.14)!important;color:#aeb6c4!important;border-color:rgba(154,163,178,.35)!important;}
#ap-bar .ap-title{overflow:hidden!important;text-overflow:ellipsis!important;white-space:nowrap!important;
  font-size:14px!important;font-weight:650!important;color:#EDEDED!important;line-height:1.3!important;}
#ap-bar .ap-meta{font-size:11px!important;color:#A0A0A0!important;white-space:nowrap!important;overflow:hidden!important;
  text-overflow:ellipsis!important;}
#ap-bar .ap-act{flex:none!important;min-width:86px!important;height:36px!important;padding:0 14px!important;border-radius:9px!important;
  border:1px solid #444!important;background:rgba(255,255,255,.06)!important;color:#EDEDED!important;font-size:12px!important;
  font-weight:650!important;cursor:pointer!important;line-height:1!important;transition:background .15s ease!important;}
#ap-bar .ap-act:hover{background:rgba(255,255,255,.14)!important;}
#ap-bar .ap-act.archived{color:#7fd6a8!important;border-color:rgba(86,200,143,.4)!important;background:rgba(86,200,143,.12)!important;}
"""

BAR_JS = """(function(){
  var bar=document.getElementById("ap-bar"); if(!bar) return;
  var btn=bar.querySelector(".ap-act"), busy=false;
  btn.addEventListener("click", function(){
    if(busy) return; busy=true;
    var arch=bar.dataset.archived==="1";
    fetch("/api/artifacts/archive",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({date:bar.dataset.date,slug:bar.dataset.slug,archived:!arch})})
      .then(function(r){return r.json();}).then(function(res){
        busy=false;
        if(!res.ok){alert("archive failed: "+(res.error||"unknown error"));return;}
        var now=!arch; bar.dataset.archived=now?"1":"0";
        btn.classList.toggle("archived",now); btn.textContent=now?"Restore":"Archive";
      }).catch(function(){busy=false; alert("archive request failed");});
  });
})();"""


def render_artifact_bar(info):
    type_ = str(info.get("type") or "other")
    if type_ not in ("prototype", "plan", "site", "other"):
        type_ = "other"
    title = html.escape(str(info.get("title") or info.get("slug") or "artifact"))
    badge = html.escape(type_)
    date_attr = html.escape(str(info.get("date") or ""))
    slug_attr = html.escape(str(info.get("slug") or ""))
    archived = "1" if info.get("archived") else "0"
    created = str(info.get("created") or "")
    meta = ""
    if len(created) >= 16:
        try:
            meta = datetime.strptime(created[:16], "%Y-%m-%dT%H:%M").strftime("%b %d, %Y · %H:%M")
        except ValueError:
            meta = created
    entry = str(info.get("entry") or "")
    if entry:
        meta = (meta + " · " + entry) if meta else entry
    meta = html.escape(meta)
    act = "Restore" if archived == "1" else "Archive"
    act_cls = " archived" if archived == "1" else ""
    return f"""<style id="ap-chrome">{BAR_STYLE}</style>
<div id="ap-bar" data-date="{date_attr}" data-slug="{slug_attr}" data-archived="{archived}">
  <a class="ap-back" href="/" aria-label="Back to home">&#8592;</a>
  <div class="ap-info">
    <div class="ap-row">
      <span class="ap-badge {badge}">{badge}</span>
      <span class="ap-title">{title}</span>
    </div>
    <div class="ap-meta">{meta}</div>
  </div>
  <button type="button" class="ap-act{act_cls}" aria-pressed="{archived}">{act}</button>
</div>
<script>{BAR_JS}</script>"""


def render_artifact_shell(info, iframe_src):
    title = html.escape(str(info.get("title") or info.get("slug") or "Artifact"))
    src = html.escape(iframe_src, quote=True)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
html, body {{ margin:0!important; width:100%; height:100%; overflow:hidden; background:#161616; }}
#ap-shell {{ width:100%; height:100%; display:flex; flex-direction:column; }}
#ap-shell #ap-bar {{ position:relative!important; top:auto!important; left:auto!important; right:auto!important;
  width:100%!important; max-width:none!important; flex:none!important; }}
#ap-frame {{ display:block; flex:1 1 auto; width:100%; min-height:0; border:0; background:#fff; }}
</style></head><body><div id="ap-shell">
{render_artifact_bar(info)}
<iframe id="ap-frame" src="{src}" title="{title}"></iframe>
</div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "artifact-server/1.0"
    config = None

    def log_message(self, fmt, *args):
        sys_stderr = self.server.stderr
        print(f'[{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] {self.address_string()} - {fmt % args}', file=sys_stderr)

    def send_body(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, code=200):
        self.send_body(code, json.dumps(obj, indent=2), "application/json")

    def send_error_page(self, code, message):
        self.send_body(code, f"<!doctype html><meta charset=utf-8><title>{code}</title><body><h1>{code}</h1><p>{html.escape(message)}</p>")

    def unauthorized(self):
        self.send_body(401, "authentication required", "text/plain; charset=utf-8",
                       extra={"WWW-Authenticate": 'Basic realm="artifacts"'})

    def check_auth(self):
        auth = self.config.get("auth")
        if not auth:
            return True
        header = self.headers.get("Authorization", "")
        expected = "Basic " + base64.b64encode(f"{auth.get('username')}:{auth.get('password')}".encode()).decode()
        return header == expected

    def do_GET(self):
        if not self.check_auth():
            self.unauthorized()
            return
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        embedded = parse_qs(parsed.query).get("__ap_embed") == ["1"]
        root = Path(self.config["artifacts_root"])
        segs = [s for s in path.split("/") if s]
        try:
            if not segs or segs[0] == "index.html":
                self.send_body(200, render_home(load_artifacts(root)))
            elif segs[0] == "healthz":
                self.send_body(200, "ok", "text/plain; charset=utf-8")
            elif segs[0] == "api" and len(segs) >= 2 and segs[1] == "artifacts.json":
                self.send_json({"count": len(load_artifacts(root)), "artifacts": load_artifacts(root)})
            elif len(segs) >= 2:
                self.serve_artifact(root, segs, embedded)
            else:
                self.send_error_page(404, "Not found")
        except BrokenPipeError:
            pass

    def do_POST(self):
        if not self.check_auth():
            self.unauthorized()
            return
        path = unquote(self.path.split("?", 1)[0])
        if path.rstrip("/") == "/api/artifacts/archive":
            self.archive_artifact()
        elif path.rstrip("/") == "/api/artifacts":
            self.publish_artifact()
        else:
            self.send_error_page(404, "Not found")

    def archive_artifact(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self.send_json({"ok": False, "error": "invalid JSON body"}, 400)
            return
        date_str = data.get("date") or ""
        slug = data.get("slug") or ""
        archived = data.get("archived")
        if not isinstance(archived, bool):
            self.send_json({"ok": False, "error": "archived must be a boolean"}, 400)
            return
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str) or not slug or "/" in slug or ".." in slug:
            self.send_json({"ok": False, "error": "invalid date or slug"}, 400)
            return
        root = Path(self.config["artifacts_root"])
        base = (root / date_str / slug).resolve()
        if not str(base).startswith(str(root.resolve())) or not base.is_dir():
            self.send_json({"ok": False, "error": "artifact not found"}, 404)
            return
        meta = base / "artifact.json"
        if not meta.is_file():
            self.send_json({"ok": False, "error": "artifact.json missing"}, 404)
            return
        try:
            manifest = json.loads(meta.read_text())
        except Exception:
            self.send_json({"ok": False, "error": "corrupt artifact.json"}, 500)
            return
        manifest["archived"] = archived
        if archived:
            manifest["archived_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        else:
            manifest.pop("archived_at", None)
        meta.write_text(json.dumps(manifest, indent=2))
        self.send_json({"ok": True, "date": date_str, "slug": slug, "archived": archived})

    def publish_artifact(self):
        """POST /api/artifacts?title=..&type=..&desc=..&entry=.. with raw body.

        Body is a tar.gz/zip archive (directory) or a single file. Title/type/desc
        come from the query string so a plain `curl --data-binary @file` works.
        """
        qs = parse_qs(urlsplit(self.path).query)
        title = (qs.get("title") or [""])[0].strip() or "artifact"
        type_ = (qs.get("type") or [""])[0].strip().lower() or "prototype"
        if type_ not in TYPES:
            type_ = "other"
        desc = (qs.get("desc") or [""])[0].strip()
        entry = (qs.get("entry") or [""])[0].strip() or "index.html"
        no_render = "no_render" in qs

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_UPLOAD:
            self.send_json({"ok": False, "error": f"body must be 1..{MAX_UPLOAD} bytes"}, 400)
            return
        body = self.rfile.read(length)

        root = Path(self.config["artifacts_root"])
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H%M")
        date_dir = root / date_str
        date_dir.mkdir(parents=True, exist_ok=True)
        base = slugify(title)
        slug = unique_slug_dir(date_dir, time_str, base)

        staging = Path(tempfile.mkdtemp(dir=str(root), prefix=".staging-"))
        artifact_dir = staging / slug
        try:
            extract_upload(body, artifact_dir, entry)
            entry = pick_entry(artifact_dir) or entry
            if not no_render:
                entry = render_markdown_upload(artifact_dir, entry)
            manifest = {
                "title": title,
                "type": type_,
                "description": desc,
                "created": now.strftime("%Y-%m-%dT%H:%M:%S"),
                "entry": entry,
                "source": "remote",
            }
            (artifact_dir / "artifact.json").write_text(json.dumps(manifest, indent=2))
            final_dir = date_dir / slug
            os.rename(artifact_dir, final_dir)
        except Exception as e:
            self.send_json({"ok": False, "error": f"publish failed: {e}"}, 400)
            return
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        path = f"/{date_str}/{slug}/"
        base_url = str(self.config.get("public_base") or "").rstrip("/")
        resp = {"ok": True, "date": date_str, "slug": slug, "title": title,
                "type": type_, "path": path}
        if base_url:
            resp["url"] = base_url + path
        ts = self.config.get("tailscale_base")
        if ts:
            resp["tailscale_url"] = str(ts).rstrip("/") + path
        self.send_json(resp)

    def serve_artifact(self, root, segs, embedded=False):
        date_dir, slug_dir = segs[0], segs[1]
        base = (root / date_dir / slug_dir).resolve()
        root_res = root.resolve()
        if not str(base).startswith(str(root_res)):
            self.send_error_page(403, "Forbidden")
            return
        if not base.is_dir():
            self.send_error_page(404, "Artifact not found")
            return
        info = {"date": date_dir, "slug": slug_dir, "archived": False}
        meta = base / "artifact.json"
        if meta.exists():
            try:
                info.update(json.loads(meta.read_text()))
            except Exception:
                pass
        if len(segs) > 2:
            rel = "/".join(segs[2:])
            target = (base / rel).resolve()
            if not str(target).startswith(str(base)):
                self.send_error_page(403, "Forbidden")
                return
            if not target.is_file():
                self.send_error_page(404, "File not found")
                return
            if not embedded and self.is_html(target):
                suffix = "/" if len(segs) == 2 else ""
                iframe_src = "/" + "/".join(quote(segment, safe="") for segment in segs) + suffix + "?__ap_embed=1"
                self.send_body(200, render_artifact_shell(info, iframe_src))
            else:
                self.send_file(target)
            return
        entry = info.get("entry") or "index.html"
        target = (base / entry).resolve()
        if not str(target).startswith(str(base)) or not target.is_file():
            self.send_error_page(404, "Entry not found")
            return
        if not embedded and self.is_html(target):
            suffix = "/" if len(segs) == 2 else ""
            iframe_src = "/" + "/".join(quote(segment, safe="") for segment in segs) + suffix + "?__ap_embed=1"
            self.send_body(200, render_artifact_shell(info, iframe_src))
        else:
            self.send_file(target)

    @staticmethod
    def is_html(path):
        return path.suffix.lower() in (".html", ".htm")

    def send_file(self, path):
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if path.suffix.lower() == ".md":
            ctype = "text/plain; charset=utf-8"
        try:
            data = path.read_bytes()
        except OSError:
            self.send_error_page(500, "Could not read file")
            return
        self.send_body(200, data, ctype)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(CONFIG_PATH))
    args = parser.parse_args()
    config = load_config(args.config)
    host, port = config["host"], int(config["port"])
    os.makedirs(config["artifacts_root"], exist_ok=True)
    server = ThreadingHTTPServer((host, port), Handler)
    Handler.config = config
    server.stderr = sys.stderr
    print(f"artifact-server listening on http://{host}:{port} (root: {config['artifacts_root']})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    import sys
    main()
