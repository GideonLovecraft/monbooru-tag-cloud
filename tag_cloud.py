#!/usr/bin/env python3
"""tag-cloud: a tag cloud page for monbooru, served by a plugin.

Pairs with monbooru like any peer, adds a "tag cloud" button to an image's
page (the only in-app entry point the plugin contract offers), and serves the
page at /plugins/tag-cloud/cloud. The page is also reachable directly by that
URL, so it can be bookmarked.

The cloud is built from GET /api/v1/tags?sort=usage, reading usage_count,
category and colour per tag. Meta tags are never shown.

Needs Python 3.9+. No third-party packages.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from string import Template

APP = "tag-cloud"
VERSION = "0.1.0"
CONTRACT = 1
DEFAULT_PORT = 7313
MAX_BODY = 1 << 20

CREDENTIALS_FILE = "credentials.json"
SETTINGS_FILE = "settings.json"

DEFAULT_SETTINGS = {
    "categories": ["general", "character", "artist", "copyright"],
    "limit": 100,
    "palette": "category",
}
VALID_LIMITS = (50, 100, 200, 500)
VALID_PALETTES = ("category", "accent", "gray")

CATEGORY_NAME = re.compile(r"^[a-z0-9_-]{1,40}$")

log = logging.getLogger(APP)


# ---------------------------------------------------------------------------
# runtime state
# ---------------------------------------------------------------------------

MONBOORU = "http://127.0.0.1:8455"
SELF_URL = "http://127.0.0.1:7313"
STATE_DIR = "."

STOP = threading.Event()
CREDS = {"token": "", "peer": "", "url": "", "buttons": ""}
CREDS_LOCK = threading.Lock()


class Unreachable(Exception):
    """monbooru did not answer at all."""


class ApiError(Exception):
    """monbooru answered with an error status."""


def creds():
    with CREDS_LOCK:
        return dict(CREDS)


def load_credentials():
    path = os.path.join(STATE_DIR, CREDENTIALS_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            with CREDS_LOCK:
                CREDS["token"] = str(data.get("token") or "")
                CREDS["peer"] = str(data.get("peer") or "")
                CREDS["url"] = str(data.get("url") or "")
                CREDS["buttons"] = str(data.get("buttons") or "")
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.warning("could not read %s: %s", path, exc)


def save_credentials(token, peer, url):
    with CREDS_LOCK:
        CREDS.update(token=token, peer=peer, url=url, buttons=buttons_fingerprint())
    path = os.path.join(STATE_DIR, CREDENTIALS_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"token": token, "peer": peer, "url": url,
                   "buttons": buttons_fingerprint()}, f)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def forget_credentials():
    with CREDS_LOCK:
        CREDS.update(token="", peer="", url="", buttons="")
    try:
        os.remove(os.path.join(STATE_DIR, CREDENTIALS_FILE))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

SETTINGS_LOCK = threading.Lock()


def _sanitize_settings(raw, known=None):
    if not isinstance(raw, dict):
        raw = {}
    cats = raw.get("categories")
    if not isinstance(cats, list):
        cats = DEFAULT_SETTINGS["categories"]
    cats = [str(c) for c in cats if isinstance(c, str)]
    cats = [c for c in cats if CATEGORY_NAME.match(c) and c != "meta"]
    if known is not None:
        cats = [c for c in cats if c in known]
    if not cats:
        cats = [c for c in DEFAULT_SETTINGS["categories"]
                if known is None or c in known]
    # Keep the checkbox order stable: general, character, artist, copyright, rest.
    order = {"general": 0, "character": 1, "artist": 2, "copyright": 3}
    cats = sorted(dict.fromkeys(cats), key=lambda c: (order.get(c, 99), c))
    limit = raw.get("limit")
    if limit not in VALID_LIMITS:
        limit = DEFAULT_SETTINGS["limit"]
    palette = raw.get("palette")
    if palette not in VALID_PALETTES:
        palette = DEFAULT_SETTINGS["palette"]
    return {"categories": cats, "limit": limit, "palette": palette}


def load_settings():
    path = os.path.join(STATE_DIR, SETTINGS_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    except Exception as exc:
        log.warning("could not read %s: %s", path, exc)
        data = {}
    return _sanitize_settings(data)


def save_settings(values):
    with SETTINGS_LOCK:
        merged = _sanitize_settings(values)
        path = os.path.join(STATE_DIR, SETTINGS_FILE)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
        return merged


# ---------------------------------------------------------------------------
# monbooru API
# ---------------------------------------------------------------------------


def api_call(method, path, *, params=None, timeout=30):
    url = MONBOORU.rstrip("/") + "/api/v1" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method)
    token = creds()["token"]
    if token:
        req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise Unreachable(str(getattr(exc, "reason", exc))) from exc


def error_text(raw, limit=300):
    try:
        doc = json.loads(raw)
        if isinstance(doc, dict):
            value = doc.get("error") or doc.get("message") or doc.get("code")
            if value:
                return str(value)[:limit]
    except Exception:
        pass
    text = raw.decode("utf-8", "replace").strip()
    return (text or "no details given")[:limit]


def expect_ok(code, raw, what):
    if 200 <= code < 300:
        return
    raise ApiError("{}: HTTP {}: {}".format(what, code, error_text(raw)))


def fetch_categories():
    code, raw = api_call("GET", "/categories", timeout=30)
    expect_ok(code, raw, "reading categories")
    try:
        rows = json.loads(raw) or []
    except ValueError as exc:
        raise ApiError("reading categories: unreadable answer") from exc
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        if not name or name == "meta":
            continue
        out.append({"name": name, "color": str(row.get("color") or "#888888")})
    order = {"general": 0, "character": 1, "artist": 2, "copyright": 3}
    out.sort(key=lambda c: (order.get(c["name"], 99), c["name"]))
    return out


def fetch_tags(categories, limit):
    rows = []
    for category in categories:
        code, raw = api_call("GET", "/tags", params={
            "category": category,
            "sort": "usage",
            "order": "desc",
            "limit": str(limit),
            "show_zero": "0",
            "type": "tag",
        })
        expect_ok(code, raw, "reading tags")
        try:
            doc = json.loads(raw) or {}
        except ValueError as exc:
            raise ApiError("reading tags: unreadable answer") from exc
        rows.extend(doc.get("results") or [])
    return rows


def shape_cloud(rows, limit):
    cleaned = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        category = str(row.get("category") or "")
        if not name or category == "meta":
            continue
        try:
            usage = int(row.get("usage_count") or 0)
        except (TypeError, ValueError):
            usage = 0
        if usage <= 0:
            continue
        cleaned.append({"name": name, "category": category,
                        "color": str(row.get("color") or "#888888"), "usage": usage})
    cleaned.sort(key=lambda t: t["usage"], reverse=True)
    return cleaned[:max(1, int(limit))]


# ---------------------------------------------------------------------------
# pairing
# ---------------------------------------------------------------------------


def buttons():
    return [{
        "slot": "detail-actions",
        "label": "tag cloud",
        "mode": "open",
        "path": "/cloud?back={back_url}",
        "media": "",
    }]


def buttons_fingerprint():
    raw = json.dumps(buttons(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def wait_for_monbooru():
    while not STOP.is_set():
        try:
            api_call("GET", "/galleries", timeout=10)
            return True
        except Unreachable:
            log.info("waiting for monbooru at %s", MONBOORU)
            STOP.wait(2)
    return False


def offer_pairing():
    while not STOP.is_set():
        peer = secrets.token_urlsafe(32)
        body = {
            "app": APP,
            "url": SELF_URL,
            "requested_scopes": ["read", "write"],
            "peer_token": peer,
            "version": VERSION,
            "buttons": buttons(),
            "contract": CONTRACT,
        }
        try:
            raw = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(
                MONBOORU.rstrip("/") + "/api/v1/pair/request",
                data=raw, method="POST")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=20) as resp:
                code, answer = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            code, answer = exc.code, exc.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log.info("cannot reach monbooru at %s: %s", MONBOORU, exc)
            STOP.wait(5)
            continue
        if code == 409:
            log.error("monbooru refused the pairing: %s", error_text(answer))
            return
        if code == 429:
            log.info("monbooru has too many pending pairing requests; retrying")
            STOP.wait(10)
            continue
        if code != 200:
            log.warning("pairing request failed (HTTP %s): %s", code, error_text(answer))
            STOP.wait(10)
            continue
        try:
            request_id = json.loads(answer).get("request_id") or ""
        except ValueError:
            request_id = ""
        if not request_id:
            log.warning("pairing request answered without an id; retrying")
            STOP.wait(10)
            continue
        log.info("waiting for approval in monbooru: Settings -> Plugins")
        while not STOP.is_set():
            try:
                code, raw = api_call(
                    "GET", "/pair/status?id=" + urllib.parse.quote(request_id), timeout=20)
            except Unreachable as exc:
                log.info("monbooru went away while pairing: %s", exc)
                STOP.wait(5)
                continue
            if code == 404:
                log.info("the pairing offer expired; offering again")
                break
            if code != 200:
                log.warning("pairing status failed (HTTP %s): %s", code, error_text(raw))
                STOP.wait(5)
                continue
            try:
                reply = json.loads(raw)
            except ValueError:
                reply = {}
            if reply.get("token"):
                save_credentials(reply["token"], peer, SELF_URL)
                log.info("paired as %s", APP)
                return
            if reply.get("status") == "denied":
                log.info("the pairing was denied; restart the plugin to offer again")
                return
            STOP.wait(2)


def bootstrap():
    load_credentials()
    if not wait_for_monbooru():
        return
    while not STOP.is_set():
        current = creds()
        if current["token"] and current["peer"]:
            if current["url"] and current["url"] != SELF_URL:
                log.info("the plugin now answers at %s but paired at %s; pairing again",
                         SELF_URL, current["url"])
                forget_credentials()
                continue
            if current["buttons"] != buttons_fingerprint():
                log.info("the button set changed; offering it for approval in monbooru")
                offer_pairing()
                return
            try:
                code, _ = api_call("GET", "/galleries", timeout=15)
            except Unreachable:
                STOP.wait(2)
                continue
            if code == 200:
                log.info("paired as %s", APP)
                return
            if code in (401, 403):
                log.info("monbooru no longer accepts the stored token; pairing again")
                forget_credentials()
                continue
            log.info("monbooru answered HTTP %s; retrying", code)
            STOP.wait(10)
            continue
        offer_pairing()
        return


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------

CSS = """
:root {
  color-scheme: dark;
  --bg: #0e0e0e;
  --surface: #1a1818;
  --border: #2a2828;
  --fg: #e8e4e2;
  --dim: #a09898;
  --accent: #9d2235;
  --link: #ef8f99;
  --error: #ef8f99;
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 13px/1.4 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  display: flex; flex-direction: column;
}
header {
  display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap;
  padding: 12px 16px 6px;
}
h1 { font-size: 16px; margin: 0; font-weight: 600; }
.status { color: var(--dim); font-size: 12px; }
.back { margin-left: auto; color: var(--link); text-decoration: none; font-size: 12px; }
.back:hover { text-decoration: underline; }
.controls {
  display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
  padding: 6px 16px 10px; border-bottom: 1px solid var(--border);
  color: var(--dim); font-size: 12px;
}
.cats { display: flex; gap: 10px; flex-wrap: wrap; }
.cat { display: inline-flex; align-items: center; gap: 5px; cursor: pointer; color: var(--fg); }
.cat input { accent-color: var(--accent); margin: 0; }
.dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
select, button {
  font: inherit; font-size: 12px; color: var(--fg); background: var(--surface);
  border: 1px solid var(--border); border-radius: 4px; padding: 3px 6px;
}
button { cursor: pointer; }
button:hover, select:hover { border-color: var(--link); }
main {
  flex: 1; min-height: 0; overflow: auto; position: relative;
}
.cloud-inner { position: relative; width: 100%; margin: 0 auto; }
.tag {
  position: absolute; color: var(--link); text-decoration: none;
  line-height: 1; white-space: nowrap; padding: 0 2px;
}
.tag:hover { text-decoration: underline; }
.empty {
  padding: 48px 16px; text-align: center; color: var(--dim);
}
.error { color: var(--error); border: 1px solid var(--error); border-radius: 6px; margin: 10px 16px; }
[hidden] { display: none !important; }
"""

SHELL = Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>tag cloud</title>
<style>${css}</style>
</head>
<body>
<header>
  <h1>Tag cloud</h1>
  <span class="status" id="status">loading…</span>
  <a class="back" id="back" href="${back}">← back</a>
</header>
<div id="error" class="error" hidden></div>
<div class="controls">
  <span class="cats" id="cats"></span>
  <label>show <select id="limit">
    <option value="50">50 tags</option>
    <option value="100">100 tags</option>
    <option value="200">200 tags</option>
    <option value="500">500 tags</option>
  </select></label>
  <label>colors <select id="palette">
    <option value="category">category colors</option>
    <option value="accent">one accent</option>
    <option value="gray">grayscale</option>
  </select></label>
  <button type="button" id="reload">reload</button>
</div>
<main id="cloud"></main>
<script>
(function () {
  var state = { settings: null };
  var catsEl = document.getElementById('cats');
  var cloudEl = document.getElementById('cloud');
  var statusEl = document.getElementById('status');
  var errorEl = document.getElementById('error');
  var limitEl = document.getElementById('limit');
  var paletteEl = document.getElementById('palette');
  var allCategories = [];

  function showError(msg) {
    errorEl.textContent = msg || '';
    errorEl.hidden = !msg;
  }

  function getJSON(url) {
    return fetch(url, { headers: { 'Accept': 'application/json' } }).then(function (r) {
      if (!r.ok) {
        return r.text().then(function (t) { throw new Error(t || ('HTTP ' + r.status)); });
      }
      return r.json();
    });
  }

  function saveSettings() {
    fetch('api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(state.settings)
    }).catch(function () { /* the next load still works */ });
  }

  function tagQuery(tag) {
    return (tag.category && tag.category !== 'general')
      ? tag.category + ':' + tag.name : tag.name;
  }

  var lastTags = [];
  var FONT_STACK = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
  var measured = document.createElement('canvas').getContext('2d');
  var inner = document.createElement('div');
  inner.className = 'cloud-inner';
  cloudEl.appendChild(inner);

  // monbooru opens plugin pages in a 90vw x 74vh dialog; take more room so
  // a few hundred words have somewhere to go.
  try {
    var frame = window.frameElement;
    if (frame) {
      var dlg = frame.closest ? frame.closest('dialog') : null;
      if (dlg) {
        dlg.style.width = 'min(1500px, 96vw)';
        dlg.style.maxWidth = '96vw';
      }
      frame.style.height = 'min(84vh, 1000px)';
    }
  } catch (e) { /* opened on its own: the window is already full */ }

  function cellKey(x, y) { return x + ',' + y; }

  // Overlap check on a coarse grid, so a few hundred words stay quick.
  function hits(grid, box) {
    var pad = 3;
    var x0 = Math.floor((box.x - pad) / 64), x1 = Math.floor((box.x + box.w + pad) / 64);
    var y0 = Math.floor((box.y - pad) / 64), y1 = Math.floor((box.y + box.h + pad) / 64);
    for (var gx = x0; gx <= x1; gx++) {
      for (var gy = y0; gy <= y1; gy++) {
        var arr = grid[cellKey(gx, gy)];
        if (!arr) continue;
        for (var k = 0; k < arr.length; k++) {
          var b = arr[k];
          if (box.x < b.x + b.w + pad && box.x + box.w + pad > b.x &&
              box.y < b.y + b.h + pad && box.y + box.h + pad > b.y) return true;
        }
      }
    }
    return false;
  }

  function addToGrid(grid, box) {
    var x0 = Math.floor(box.x / 64), x1 = Math.floor((box.x + box.w) / 64);
    var y0 = Math.floor(box.y / 64), y1 = Math.floor((box.y + box.h) / 64);
    for (var gx = x0; gx <= x1; gx++) {
      for (var gy = y0; gy <= y1; gy++) {
        var key = cellKey(gx, gy);
        (grid[key] = grid[key] || []).push(box);
      }
    }
  }

  function sizesFor(tags, width, height) {
    var maxUsage = 0, minUsage = Infinity;
    tags.forEach(function (t) {
      if (t.usage > maxUsage) maxUsage = t.usage;
      if (t.usage < minUsage) minUsage = t.usage;
    });
    var span = Math.max(1, maxUsage - minUsage);
    var base = Math.sqrt((width * height) / Math.max(30, tags.length));
    var maxPx = Math.max(18, Math.min(64, base * 0.8));
    var minPx = Math.max(9, Math.min(13, base * 0.25));
    var items = tags.map(function (t) {
      var ratio = Math.sqrt((t.usage - minUsage) / span);
      var size = minPx + (maxPx - minPx) * ratio;
      measured.font = size.toFixed(1) + 'px ' + FONT_STACK;
      return { tag: t, size: size,
               w: measured.measureText(t.name).width + 6,
               h: size * 1.12 + 4 };
    });
    items.sort(function (a, b) { return b.size - a.size; });
    return items;
  }

  // Biggest word first, each along a spiral from the centre.
  function pack(items, width, height) {
    var grid = {}, placed = [], skipped = 0;
    var cx = width / 2, cy = height / 2, margin = 4;
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      var ok = false;
      for (var s = 0; s < 1400; s++) {
        var angle = s * 0.28 + (i % 9) * 0.7;
        var radius = 2.2 * angle;
        var x = cx + Math.cos(angle) * radius;
        var y = cy + Math.sin(angle) * radius;
        var box = { x: x - it.w / 2, y: y - it.h / 2, w: it.w, h: it.h };
        if (box.x < margin || box.y < margin ||
            box.x + it.w > width - margin || box.y + it.h > height - margin) continue;
        if (hits(grid, box)) continue;
        addToGrid(grid, box);
        placed.push({ tag: it.tag, size: it.size, x: box.x, y: box.y });
        ok = true;
        break;
      }
      if (!ok) skipped++;
    }
    return { placed: placed, skipped: skipped };
  }

  // The layout never grows past 2K (2560 x 1440) even on a huge screen or
  // with a big tag count; the rest of the window stays empty margin.
  var MAX_W = 2560, MAX_H = 1440;

  function drawCloud() {
    var width = Math.min(cloudEl.clientWidth, MAX_W);
    var visible = Math.min(cloudEl.clientHeight, MAX_H);
    if (!width || !visible || !lastTags.length) return;
    var items = sizesFor(lastTags, width, visible);
    var height = visible;
    var result = pack(items, width, height);
    // If words do not fit, make the canvas taller and let the page scroll
    // instead of silently dropping them (up to the 2K ceiling).
    for (var attempt = 1;
         result.skipped > Math.max(1, lastTags.length * 0.02) && attempt <= 6;
         attempt++) {
      height = Math.min(Math.ceil(visible * Math.pow(1.6, attempt)), MAX_H);
      result = pack(items, width, height);
      if (height >= MAX_H) break;
    }
    inner.style.width = width + 'px';
    inner.style.height = height + 'px';
    inner.style.marginTop =
      Math.max(0, Math.round((cloudEl.clientHeight - height) / 2)) + 'px';
    inner.innerHTML = '';
    var palette = state.settings.palette;
    result.placed.forEach(function (p) {
      var a = document.createElement('a');
      a.className = 'tag';
      a.href = '/?q=' + encodeURIComponent(tagQuery(p.tag));
      a.target = '_top';
      a.textContent = p.tag.name;
      a.style.left = p.x.toFixed(1) + 'px';
      a.style.top = p.y.toFixed(1) + 'px';
      a.style.fontSize = p.size.toFixed(1) + 'px';
      if (palette === 'category') a.style.color = p.tag.color || '#e8e4e2';
      else if (palette === 'gray') a.style.color = '#b9b3ae';
      a.title = p.tag.category + ': ' + p.tag.usage + ' images';
      inner.appendChild(a);
    });
    cloudEl.scrollTop = Math.max(0, Math.round((height - visible) / 2));
    var top = 0;
    lastTags.forEach(function (t) { if (t.usage > top) top = t.usage; });
    var status = lastTags.length + ' tags · top tag: ' + top.toLocaleString() + ' images';
    if (height > visible) status += ' · scrolls';
    if (result.skipped) status += ' · ' + result.skipped + " didn't fit";
    statusEl.textContent = status;
  }

  function renderCloud(doc) {
    lastTags = doc.tags || [];
    if (!lastTags.length) {
      inner.style.height = 'auto';
      inner.innerHTML = '<p class="empty">No tags for this selection.</p>';
      statusEl.textContent = 'no tags';
      return;
    }
    requestAnimationFrame(drawCloud);
  }

  var resizeTimer = null;
  window.addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(drawCloud, 120);
  });

  function loadCloud() {
    showError('');
    var cats = state.settings.categories.join(',');
    var url = 'api/cloud?cats=' + encodeURIComponent(cats)
      + '&limit=' + state.settings.limit
      + '&palette=' + encodeURIComponent(state.settings.palette);
    getJSON(url).then(renderCloud).catch(function (e) {
      showError('could not load tags: ' + e.message);
      statusEl.textContent = 'error';
    });
  }

  function renderCategories() {
    catsEl.innerHTML = '';
    allCategories.forEach(function (cat) {
      var label = document.createElement('label');
      label.className = 'cat';
      var box = document.createElement('input');
      box.type = 'checkbox';
      box.value = cat.name;
      box.checked = state.settings.categories.indexOf(cat.name) >= 0;
      box.addEventListener('change', function () {
        var picked = [];
        allCategories.forEach(function (c) {
          var cb = catsEl.querySelector('input[value="' + c.name + '"]');
          if (cb && cb.checked) picked.push(c.name);
        });
        state.settings.categories = picked;
        saveSettings();
        loadCloud();
      });
      var dot = document.createElement('span');
      dot.className = 'dot';
      dot.style.background = cat.color || '#888';
      label.appendChild(box);
      label.appendChild(dot);
      label.appendChild(document.createTextNode(cat.name));
      catsEl.appendChild(label);
    });
  }

  function init() {
    getJSON('api/meta').then(function (meta) {
      state.settings = meta.settings;
      allCategories = meta.categories || [];
      renderCategories();
      limitEl.value = String(state.settings.limit);
      paletteEl.value = state.settings.palette;
      limitEl.addEventListener('change', function () {
        state.settings.limit = parseInt(limitEl.value, 10);
        saveSettings();
        loadCloud();
      });
      paletteEl.addEventListener('change', function () {
        state.settings.palette = paletteEl.value;
        saveSettings();
        loadCloud();
      });
      document.getElementById('reload').addEventListener('click', loadCloud);
      loadCloud();
    }).catch(function (e) {
      showError('could not start: ' + e.message);
      statusEl.textContent = 'error';
    });
  }
  init();
})();
</script>
</body>
</html>
""")


def sanitize_back(raw):
    raw = (raw or "").strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return "/"
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return raw
    return "/"


def render_page(back):
    return SHELL.substitute(css=CSS, back=html.escape(sanitize_back(back), quote=True))


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def send_body(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, doc):
        self.send_body(status, json.dumps(doc).encode("utf-8"),
                       "application/json; charset=utf-8")

    def send_text(self, status, text):
        self.send_body(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def send_html(self, status, markup):
        self.send_body(status, markup.encode("utf-8"), "text/html; charset=utf-8")

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            raise ValueError("request body too large")
        return self.rfile.read(length) if length else b""

    def authorized(self):
        header = self.headers.get("Authorization", "")
        peer = creds()["peer"]
        if not peer or not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[len("Bearer "):], peer)

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def route(self, method):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            body = self.read_body() if method == "POST" else b""
            if path == "/health" and method == "GET":
                self.send_json(200, {"version": VERSION})
                return
            if not self.authorized():
                self.send_text(401, "unauthorized")
                return
            if path == "/api/v1/pair/remove" and method == "POST":
                forget_credentials()
                log.info("the pairing was removed in monbooru; offering again")
                self.send_json(200, {"status": "removed"})
                threading.Thread(target=bootstrap, name="pair", daemon=True).start()
                return
            if path == "/cloud" and method == "GET":
                back = (query.get("back") or [""])[0]
                self.send_html(200, render_page(back))
                return
            if path == "/api/meta" and method == "GET":
                self.handle_meta()
                return
            if path == "/api/cloud" and method == "GET":
                self.handle_cloud(query)
                return
            if path == "/api/settings" and method == "POST":
                self.handle_settings(body)
                return
            self.send_text(404, "not found")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except Unreachable as exc:
            log.warning("%s %s: monbooru unreachable: %s", method, path, exc)
            self.send_text(502, "monbooru did not answer: {}".format(exc))
        except ValueError as exc:
            self.send_text(400, str(exc))
        except Exception:
            log.exception("%s %s", method, path)
            try:
                self.send_text(500, "the plugin failed; see monbooru's log")
            except OSError:
                pass

    def handle_meta(self):
        try:
            categories = fetch_categories()
        except (ApiError, Unreachable) as exc:
            self.send_json(502, {"error": str(exc)})
            return
        known = set(c["name"] for c in categories)
        settings = _sanitize_settings(load_settings(), known)
        self.send_json(200, {"categories": categories, "settings": settings,
                             "version": VERSION})

    def handle_cloud(self, query):
        def one(name, default=""):
            return (query.get(name) or [default])[0]

        raw_cats = one("cats")
        cats = [c.strip() for c in raw_cats.split(",") if c.strip()]
        cats = [c for c in cats if CATEGORY_NAME.match(c) and c != "meta"]
        if not cats:
            self.send_json(200, {"tags": [], "limit": 0})
            return
        try:
            limit = int(one("limit", "100"))
        except ValueError:
            limit = 100
        limit = max(1, min(500, limit))
        palette = one("palette", "category")
        if palette not in VALID_PALETTES:
            palette = "category"
        try:
            rows = fetch_tags(cats, limit)
        except (ApiError, Unreachable) as exc:
            self.send_json(502, {"error": str(exc)})
            return
        tags = shape_cloud(rows, limit)
        self.send_json(200, {"tags": tags, "limit": limit, "palette": palette})

    def handle_settings(self, body):
        try:
            values = json.loads(body.decode("utf-8", "replace") or "{}")
        except ValueError:
            self.send_text(400, "bad json")
            return
        try:
            saved = save_settings(values)
        except OSError as exc:
            self.send_text(500, "could not save settings: {}".format(exc))
            return
        self.send_json(200, saved)


# ---------------------------------------------------------------------------
# startup and shutdown
# ---------------------------------------------------------------------------


def watch_stdin():
    """monbooru stops a managed plugin by closing its stdin."""
    try:
        if sys.stdin is None:
            return
        while True:
            chunk = sys.stdin.buffer.read(1024)
            if not chunk:
                break
    except Exception:
        pass
    log.info("stdin closed; stopping")
    STOP.set()


def install_signal_handlers():
    def stop(signum, _frame):
        log.info("signal %d; stopping", signum)
        STOP.set()

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, stop)
        except (ValueError, OSError):
            pass


def main(argv=None):
    global MONBOORU, SELF_URL, STATE_DIR

    parser = argparse.ArgumentParser(description="tag cloud page for monbooru")
    parser.add_argument("--monbooru",
                        default=os.environ.get("MONBOORU_URL") or "http://127.0.0.1:8455",
                        help="monbooru base url (default: $MONBOORU_URL or http://127.0.0.1:8455)")
    parser.add_argument("--addr", default="127.0.0.1", help="address to serve on")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="port to serve on")
    parser.add_argument("--url", default="",
                        help="address monbooru should call the plugin at")
    parser.add_argument("--state", default=".",
                        help="directory holding credentials.json and settings.json")
    args = parser.parse_args(argv)

    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")

    MONBOORU = args.monbooru.rstrip("/")
    STATE_DIR = args.state or "."
    host = "127.0.0.1" if args.addr in ("0.0.0.0", "::") else args.addr
    SELF_URL = (args.url or "http://{}:{}".format(host, args.port)).rstrip("/")

    try:
        server = ThreadingHTTPServer((args.addr, args.port), Handler)
    except OSError as exc:
        log.error("cannot listen on %s:%s: %s", args.addr, args.port, exc)
        return 1
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    log.info("%s %s listening on %s:%s", APP, VERSION, args.addr, args.port)

    if sys.stdin is not None and not sys.stdin.isatty():
        threading.Thread(target=watch_stdin, name="stdin", daemon=True).start()
    install_signal_handlers()
    threading.Thread(target=bootstrap, name="pair", daemon=True).start()

    STOP.wait()
    log.info("shutting down")
    server.shutdown()
    server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
