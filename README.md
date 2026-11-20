# tag-cloud — monbooru plugin

A tag cloud page for monbooru: the most-used tags, sized by how many images
carry them, with the categories, count and colours you choose. Meta tags are
never shown.

No monbooru changes: it's a plugin. It pairs like any peer and serves a page
under `/plugins/tag-cloud/`.

## Where it shows up

A plugin cannot add a link to monbooru's top nav (that bar is fixed in
monbooru's layout, and the plugin contract only offers buttons on the image
page and the batch bar). So the page has two entry points:

- **`tag cloud` button** in an image's **Plugins** card — opens the page in a
  pop-in over monbooru;
- the page directly: **`http://127.0.0.1:8455/plugins/tag-cloud/cloud`**
  (adjust host/port) — bookmark it, or use the pop-in's *open in a tab* link.
  It is reachable while monbooru runs and the plugin is enabled and paired.

Clicking a tag opens monbooru's search for it (`/?q=tag`, or
`/?q=artist:name` for non-general categories).

## What it shows

- the top **50 / 100 / 200 / 500** tags by usage, font size scaled by
  `usage_count`;
- a packed cloud, not a list: the biggest words are placed first and the rest
  spiral outward from the centre until they collide; the whole cloud is
  re-fitted on resize, and words that do not fit are counted in the status
  line;
- the page takes a bigger pop-in (about 96vw × 84vh) and, for larger counts
  like 500 tags, grows the canvas vertically and scrolls instead of dropping
  words; the layout is capped at **2560 × 1440 (2K)**, so a 4K window just
  leaves empty margin around the cloud;
- **categories**: any mix of general, artist, copyright, character (and any
  custom category your gallery has) — meta is always excluded;
- **colours**: category colours (monbooru's own), one accent, or grayscale.

Defaults: all four built-in categories, top 100, category colours. Changes are
saved in `settings.json` next to the plugin and apply next time too.

## Requirements

- monbooru with plugin contract `1`.
- **Python 3.9+** on the machine running monbooru. Standard library only —
  there is nothing to `pip install`.

## Install

Put the folder next to `monbooru.toml`, inside `plugins/`, named
`tag-cloud` (the plugin's name):

```
<monbooru profile>/
├── monbooru.toml
└── plugins/
    └── tag-cloud/
        ├── plugin.toml
        ├── tag_cloud.py
        └── README.md
```

Windows desktop/portable profile is `%APPDATA%\monbooru\` (or the folder with
the portable `monbooru.exe`). Then restart monbooru, open **Settings →
Plugins**, **Enable** `tag-cloud`, and approve its pairing (asks `read` +
`write`; `write` is only needed because open-mode buttons require it).

The plugin listens on `127.0.0.1:7313` by default. If that port is taken, add
`--port` to `args_windows` in `plugin.toml`; it will then offer a re-pair,
since monbooru calls it at the address from the pairing.

`plugin.toml` runs `python3` on Linux/macOS and `python` on Windows; the
interpreter must be on `PATH`. If it isn't, put the full path to `python.exe`
in `command_windows` (`py -0p` prints the installed ones).

## How it works

- Pairs with `POST /api/v1/pair/request` + `GET /api/v1/pair/status`; keeps
  the token and peer secret in `credentials.json`.
- Declares one open button (`tag cloud`) in `detail-actions`.
- Reads `GET /api/v1/categories` (names + colours) and
  `GET /api/v1/tags?category=…&sort=usage&order=desc&limit=N&show_zero=0&type=tag`
  once per selected category, merges by `usage_count`, and renders the cloud.
- The page talks only to the plugin (`api/meta`, `api/cloud`,
  `api/settings`); the browser holds no monbooru token — monbooru's
  `/plugins/` mount adds the peer secret to each request.

No API token, port or path from a specific machine is baked in; state files
(`credentials.json`, `settings.json`) are created at run time and are
git-ignored.

## Limits

- The cloud is one page of the top tags; it does not paginate.
- Usage counts are whatever monbooru reports at that moment (no caching).
- The nav link needs a monbooru change; until then the button + bookmark are
  the entry points.

## Licence

MIT, see [LICENSE](LICENSE). Made by GideonLovecraft.

