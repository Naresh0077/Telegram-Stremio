# File Explorer (`/explorer`)

Windows-11-style browser for **every attachment** in your configured Telegram channels (videos, audio, images,
documents, archives, subtitles, other) — with or without IMDb/TMDB metadata.

## Deploy (nothing to install on the server)
1. Copy the changed files over your project (same paths) and restart / rebuild as usual.
2. Open `https://<your-domain>/explorer` (same admin login) and click **Sync** once. New uploads are indexed live afterwards.

No new Python packages, no ffmpeg, no second Telegram client — only new collections
(`explorer_files`, `explorer_folders`, `explorer_state`) in your existing *tracking* DB.

## URLs
Every view and every virtual folder has its own link, e.g. `/explorer/Movies`, `/explorer/Movies/2024`,
`/explorer/Videos`, `/explorer/Favorites`, `/explorer/Channels/<name>`. Open them directly any time (right-click a folder →
*Copy folder link*). Root folders can't be named like built-in views (Videos, Audio, Images, Documents, Archives, Subtitles,
Favorites, Folders, Channels, Home, All Files, Other).

## Videos & audio: double-click = VLC (no download)
Double-click a video/audio file → VLC opens and plays it straight from Telegram (seeking works). Nothing is downloaded, no
dialog or player is shown, and video/audio have no Download option anywhere.

**One-time setup per PC/browser** (a web page can't start VLC until Windows knows how — unavoidable):
click *VLC setup* (it appears automatically the first time you double-click), download `vlc-setup.reg`, open it, click Yes,
press *Done*, and tick *Always allow* if the browser asks "Open VLC?". Needs VLC installed (path configurable in the dialog).
The link inside is signed, expires (6 h default, `EXPLORER_LINK_TTL`) and contains no Telegram credentials.

Images / PDF / text preview in the page; other file types download. Also: search, filters, favorites, drag-and-drop folders.

## Streaming fix included
`media_streamer`, `global_media_streamer` and `virtual_dl` computed one chunk too few when a Range ended exactly on a 1 MiB
boundary (e.g. `bytes=0-0`), producing short responses. Fixed (`ceil((end+1)/chunk)`); covered by `tests/test_range_math.py`.
