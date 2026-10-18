# File Explorer (`/explorer`)

Windows-11-style browser for **every attachment** in your configured Telegram channels (videos, audio, images,
documents, archives, subtitles, other) — with or without IMDb/TMDB metadata.

## Deploy (nothing to install on the server)
1. Copy the changed files over your project (same paths) and restart / rebuild as usual.
2. Open `https://<your-domain>/explorer` (same admin login) and click **Sync** once. New uploads are indexed live afterwards.

No new Python packages, no ffmpeg, no second Telegram client — only new collections
(`explorer_files`, `explorer_folders`, `explorer_state`) in your existing *tracking* DB.
**Your streaming code is untouched** (`stream_routes.py`, `custom_dl.py`, `virtual_dl.py` are identical to the original).

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

Images/PDFs preview in the page (plain Telegram download, ≤ 40 MB); text files show the first 256 KB; other types use the same
`/dl` link to download. Also: search, filters, favorites, drag-and-drop folders.
