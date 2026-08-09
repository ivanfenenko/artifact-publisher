#!/usr/bin/env python3
"""publish-artifact — publish a file or directory to the artifact server.

Copies a prototype/plan/site into ~/artifacts/<date>/<slug>/, writes the
artifact.json manifest, optionally renders markdown to HTML, and prints the
public URL.

Usage:
  publish-artifact <src> [--title TITLE] [--type prototype|plan|site] [--desc DESC]
"""

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

CONFIG_PATH = Path.home() / ".artifacts" / "config.json"
DEFAULTS = {
    "host": "0.0.0.0",
    "port": 8787,
    "artifacts_root": str(Path.home() / "artifacts"),
    "auth": None,
    "public_base": f"http://{Path.home().name}-mac.local:8787",
    # Optional: override the auto-detected Tailscale base, e.g.
    # "tailscale_base": "http://mac-mini.tailabcdef.ts.net:8787"
    "tailscale_base": None,
}

TYPES = {"prototype", "plan", "site", "other"}


def load_config(path=CONFIG_PATH):
    cfg = dict(DEFAULTS)
    if Path(path).exists():
        try:
            cfg.update(json.loads(Path(path).read_text()))
        except Exception:
            pass
    cfg["artifacts_root"] = os.path.expanduser(cfg["artifacts_root"])
    return cfg


def slugify(name):
    name = re.sub(r"\s+", "-", name.strip().lower())
    name = re.sub(r"[^a-z0-9_-]+", "", name)
    return name or "artifact"


# --- URL printing -----------------------------------------------------------

TAILSCALE_BIN = (
    shutil.which("tailscale")
    or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
)


def find_tailscale_hostname():
    """Best stable Tailscale base host for this machine, or None."""
    if not os.path.exists(TAILSCALE_BIN):
        return None
    try:
        out = subprocess.run(
            [TAILSCALE_BIN, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            data = json.loads(out.stdout)
            dns = data.get("Self", {}).get("DNSName", "")
            if dns:
                return dns.rstrip(".")
    except Exception:
        pass
    try:
        out = subprocess.run(
            [TAILSCALE_BIN, "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            ips = out.stdout.split()
            if ips:
                return ips[0]
    except Exception:
        pass
    return None


# --- minimal markdown renderer ---------------------------------------------

def inline(text):
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
    open_lists = []  # (tag, is_bullet) markers in case we need to close

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
            out.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
            i += 1
            continue

        m = re.match(r"^([>*])\s+(.*)$", stripped)
        if m and m.group(1) == ">":
            quote_lines = []
            while i < n and lines[i].strip().startswith(">"):
                quote_lines.append(inline(re.sub(r"^>\s?", "", lines[i].strip())))
                i += 1
            close_lists()
            out.append("<blockquote>" + "<br>".join(quote_lines) + "</blockquote>")
            continue

        m = re.match(r"^([-*])\s+(.*)$", stripped)
        if m:
            close_lists()
            items = [inline(m.group(2))]
            i += 1
            while i < n and re.match(r"^\s*[-*]\s+", lines[i]):
                items.append(inline(re.sub(r"^\s*[-*]\s+", "", lines[i])))
                i += 1
            out.append("<ul>" + "".join(f"<li>{it}</li>" for it in items) + "</ul>")
            continue

        m = re.match(r"^\d+\.\s+(.*)$", stripped)
        if m:
            close_lists()
            items = [inline(m.group(1))]
            i += 1
            while i < n and re.match(r"^\s*\d+\.\s+", lines[i]):
                items.append(inline(re.sub(r"^\s*\d+\.\s+", "", lines[i])))
                i += 1
            out.append("<ol>" + "".join(f"<li>{it}</li>" for it in items) + "</ol>")
            continue

        para = [inline(stripped)]
        i += 1
        while i < n and lines[i].strip() and not re.match(r"^(#{1,6}\s|[-*>]\s|\d+\.\s|```)", lines[i].strip()):
            para.append(inline(lines[i].strip()))
            i += 1
        out.append("<p>" + " ".join(para) + "</p>")

    if in_code:
        out.append("<pre><code>" + "\n".join(code_lines) + "</code></pre>")
    close_lists()
    return "\n".join(out) + "\n"


# --- publish logic ----------------------------------------------------------

def unique_slug_dir(date_dir, time_str, base):
    for i in range(1, 1000):
        name = f"{time_str}-{base}" if i == 1 else f"{time_str}-{i}-{base}"
        if not (date_dir / name).exists():
            return name
    raise RuntimeError("could not find unique slug")


def copy_source(src: Path, dest: Path):
    if src.is_dir():
        shutil.copytree(src, dest, dirs_exist_ok=True)
    else:
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest / src.name)


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("src", help="file or directory to publish")
    parser.add_argument("--title")
    parser.add_argument("--type", choices=sorted(TYPES), default="prototype")
    parser.add_argument("--desc", default="")
    parser.add_argument("--no-render", action="store_true", help="don't render markdown to HTML")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="path to config.json")
    args = parser.parse_args()

    config = load_config(args.config)
    root = Path(config["artifacts_root"])
    src = Path(args.src).expanduser().resolve()
    if not src.exists():
        print(f"error: {src} does not exist", file=sys.stderr)
        sys.exit(1)

    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H%M")
    date_dir = root / date_str
    os.makedirs(date_dir, exist_ok=True)
    base = slugify(args.title or src.name)
    slug = unique_slug_dir(date_dir, time_str, base)

    staging = Path(tempfile.mkdtemp(dir=str(root), prefix=".staging-"))
    artifact_dir = staging / slug
    try:
        copy_source(src, artifact_dir)
        entry = pick_entry(artifact_dir)
        if entry and entry.lower().endswith(".md") and not args.no_render:
            md_path = artifact_dir / entry
            html_path = artifact_dir / (Path(entry).stem + ".html")
            try:
                html_path.write_text(render_markdown(md_path.read_text(encoding="utf-8")))
                entry = html_path.name
            except Exception as e:
                print(f"warning: markdown render failed ({e}); serving raw", file=sys.stderr)
        manifest = {
            "title": args.title or src.name,
            "type": args.type,
            "description": args.desc,
            "created": now.strftime("%Y-%m-%dT%H:%M:%S"),
            "entry": entry or "index.html",
            "source": str(src),
        }
        (artifact_dir / "artifact.json").write_text(json.dumps(manifest, indent=2))
        final_dir = date_dir / slug
        os.rename(artifact_dir, final_dir)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    base = config["public_base"].rstrip("/")
    path = f"/{date_str}/{slug}/"
    url = f"{base}{path}"
    print(f"published: {args.type} '{manifest['title']}'")
    print(f"  LAN:       {url}")
    ts = config.get("tailscale_base")
    if ts:
        print(f"  Tailscale: {ts.rstrip('/')}{path}")
    else:
        ts_host = find_tailscale_hostname()
        if ts_host:
            port = config["port"]
            print(f"  Tailscale: http://{ts_host}:{port}{path}")
        else:
            print("  Tailscale: (tailscale not detected; set 'tailscale_base' in config to enable)")


if __name__ == "__main__":
    main()
