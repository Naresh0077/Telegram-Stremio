# File Explorer (`/explorer`)

Windows-style UI (ribbon with File / Home / Share / View tabs, address bar, navigation tree, Details and Large-icons views,
status bar, dark by default with a light option under View → Dark mode).

Windows-11-style browser for **every attachment** in your configured Telegram channels (videos, audio, images,
documents, archives, subtitles, other) — with or without IMDb/TMDB metadata.

## Deploy (nothing to install on the server)
1. Copy the changed files over your project (same paths) and restart / rebuild as usual.
2. Open `https://<your-domain>/explorer` (same admin login) and click **Sync** once. New uploads are indexed live afterwards.

No new Python packages by default, no ffmpeg, no second Telegram client — only new collections
(`explorer_files`, `explorer_folders`, `explorer_state`) in your existing *tracking* DB.
**Your streaming code is untouched** (`stream_routes.py`, `custom_dl.py`, `virtual_dl.py` are identical to the original).

## Phone UI
Under 700 px wide the page switches to a phone layout: app bar (☰ drawer, title, search, ⋮), breadcrumb chips, large list rows
with thumbnails (or a 2/3-column grid via ⋮ → View as grid). **Tap** opens; **long-press** selects (then ★ favorite, move,
⋮ for more). Sort / filter / new folder / sync / dark mode are in the ⋮ sheet. Videos open in VLC with one tap:
Android → VLC app (intent), iPhone/iPad → VLC app (x-callback). Needs the VLC app installed and your site reachable from the
phone (your public https domain). No one-time setup on phones; that is Windows-only. macOS/Linux download a small `.m3u`.

## Videos & audio: double-click = VLC, using the admin panel's own link
The Explorer builds exactly what *Media → Edit → Content → Copy Link* builds:
`https://<your-site>/dl/<API token>/<stream id>/<file name>` (first API token from the Dashboard), and hands it to VLC.
So playback is identical to pasting that copied link into VLC — it is served by your original `/dl` streaming route.
Nothing is downloaded; video/audio have no Download option. (No API token yet? create one on the Dashboard first.)

**One-time setup per PC/browser** (a web page can't start VLC until Windows knows how): the setup box appears on the first
double-click (or use the *VLC setup* button): download `vlc-setup.reg`, open it, click Yes, press *Done*, tick *Always allow*
if the browser asks. Needs VLC installed (path configurable in the dialog).

## URLs
Every view and virtual folder has its own link: `/explorer/Movies`, `/explorer/Movies/2024`, `/explorer/Videos`,
`/explorer/Favorites`, `/explorer/Channels/<name>`. Right-click a folder → *Copy folder link*. Root folders can't be named like
built-in views (Videos, Audio, Images, Documents, Archives, Subtitles, Favorites, Folders, Channels, Home, All Files, Other).

## Thumbnails
Light by default: (1) the picture Telegram stored on the file (MP4s have one); (2) for videos without one, the poster of the
matching Stremio catalog entry; (3) a file-type icon. Telegram is only asked when the index says a picture exists, and poster
look-ups are remembered for an hour, so browsing costs your server almost nothing.

*Optional, OFF by default:* a picture made from the **first frame of the video** for MKVs that have none. It needs the extra
package `av` (add `"av>=13.0.0"` to `dependencies` in `pyproject.toml`); without it nothing here is installed or started.
When on, the decoding runs in a separate low-priority helper process with a memory cap (`EXPLORER_THUMB_MAX_MB`, default 450), one
video at a time, only while at least `EXPLORER_THUMB_MIN_FREE_MB` (200) MB of RAM are free, so a crash or a huge file can never
take the website down. Results are cached on disk (`EXPLORER_THUMB_DIR`, `EXPLORER_THUMB_CACHE_MB`=300).
A video with no picture: select it → Properties → *Diagnose* prints the exact reason.

## Small servers (Oracle free tier etc.)
Nothing in the Explorer runs in the background: indexing only happens when you press Sync (or `EXPLORER_AUTO_SYNC=true`), new
uploads are indexed with one small database write each, and VLC playback is served by your original `/dl` code. Memory caches are
small and capped (thumbnails ~2 MB, full-size pictures 32 MB in total, largest picture 25 MB).

## Image viewer
Double-click / tap an image (it is fitted completely inside the window): ◀ ▶ buttons or ← → keys (Home / End), swipe on a phone,
a "3 / 24" counter, a filmstrip, the next pictures are preloaded, and more pages load automatically at the end of the list.
**Zoom:** mouse wheel (towards the cursor), the − / percent / + buttons, `+` `-` `0` keys, double-click / double-tap (fit ↔ actual
size), drag to move around a zoomed picture, pinch on a touch screen. Swiping only changes the picture while it is not zoomed in.
Each new picture starts fitted. Esc, ✕ or the browser/phone Back button closes it and leaves you in the same folder.

## Videos & audio: double-click = VLC, using the admin panel's own link
The Explorer builds exactly what *Media → Edit → Content → Copy Link* builds:
`https://<your-site>/dl/<API token>/<stream id>/<file name>` (first API token from the Dashboard), and hands it to VLC.
So playback is identical to pasting that copied link into VLC — it is served by your original `/dl` streaming route.
Nothing is downloaded; video/audio have no Download option. (No API token yet? create one on the Dashboard first.)

**One-time setup per PC/browser** (a web page can't start VLC until Windows knows how): the setup box appears on the first
double-click (or use the *VLC setup* button): download `vlc-setup.reg`, open it, click Yes, press *Done*, tick *Always allow*
if the browser asks. Needs VLC installed (path configurable in the dialog).

## URLs
Every view and virtual folder has its own link: `/explorer/Movies`, `/explorer/Movies/2024`, `/explorer/Videos`,
`/explorer/Favorites`, `/explorer/Channels/<name>`. Right-click a folder → *Copy folder link*. Root folders can't be named like
built-in views (Videos, Audio, Images, Documents, Archives, Subtitles, Favorites, Folders, Channels, Home, All Files, Other).

## Thumbnails (also for MKV)
Order: (1) the picture Telegram stored on the file (MP4s usually have one); (2) for videos without one, which is typical for MKV
sent as a document, the **first frame of the video itself** (no seeking, only the first few MB of the file are read), made in
the background with PyAV (package `av`, its wheel bundles FFmpeg's libraries, so nothing is installed on the server: the app
installs the package by itself on the next Restart); (3) the poster of the matching Stremio catalog entry; (4) a file-type icon.
If the very first frame is black (a fade-in) the next ~3 seconds of the opening are checked for a real picture.
Works for H.264 / HEVC / 10-bit / AV1 / AVI. Scaled to 480 px, cached on disk (`EXPLORER_THUMB_DIR`, capped at
`EXPLORER_THUMB_CACHE_MB`=300), 3 videos at a time (`EXPLORER_THUMB_JOBS`). Tiles show an icon first and the picture appears
when it is ready. `EXPLORER_THUMBS=false` switches generation off; a failed video is retried after 10 minutes.
**A video still shows no picture?** Select it → Properties → *Diagnose*: it tries right now and prints the exact reason.

## Image viewer
Double-click / tap an image (it is fitted completely inside the window): ◀ ▶ buttons or ← → keys (Home / End), swipe on a phone, a "3 / 24" counter, a filmstrip, the
next pictures are preloaded, and more pages load automatically when you reach the end of the list. Esc, ✕ or the browser/phone
Back button closes it and leaves you in the same folder.

PDFs preview in the page (plain Telegram download, ≤ 25 MB); text files show the first 256 KB; other types use the same
`/dl` link to download. Also: search, filters, favorites, drag-and-drop folders.
