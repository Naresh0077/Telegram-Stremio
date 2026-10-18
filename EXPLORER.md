# File Explorer (`/explorer`)

Windows-11-style browser for **every attachment** in your configured Telegram channels (videos, audio, images,
documents, archives, subtitles, other) — with or without IMDb/TMDB metadata.

## Deploy (nothing to install on the host)
1. Copy the changed files over your project (same paths) and restart / rebuild as usual.
2. Open `https://<your-domain>/explorer` (same admin login) and click **Sync** once.
   New uploads are indexed live afterwards.

No new Python packages, no ffmpeg, no second Telegram client — only new collections
(`explorer_files`, `explorer_folders`, `explorer_state`) in your existing *tracking* DB.

## Playback = VLC (all videos and audio)
Double-click (or Play) a video/audio file → a small `.m3u` playlist downloads with an expiring signed link
(6 h by default) to the **original, untouched** Telegram file → open it in VLC. Seeking works (HTTP range, same
`media_streamer` Stremio uses). No Telegram tokens are in the link. No conversion happens on the server.

One-time on Windows: open the downloaded `.m3u` once, choose "VLC media player" + "Always".
Optional: download `vlc-setup.reg` from the dialog and run it once so `vlc://` links launch VLC directly.

Images/PDF/text preview inside the page; everything else downloads. Other actions: search, filters, favorites,
virtual folders (drag & drop), copy stream link, Open in Stremio (when the file is in your catalog).

## Notes
Indexing walks message-ID ranges and posts/deletes a tiny probe message per channel (Bot API can't read history) — same
method as the admin Scan tool. Optional env: `EXPLORER_LINK_TTL`, `EXPLORER_AUTO_SYNC` (see `sample_config.env`).
