"""File Explorer routes.

* Pages:      /explorer and /explorer/<anything>   (same single-page app; every folder has its own URL)
* JSON API:   /api/explorer/*
* Raw bytes:  /_ex/*   (kept off /explorer/ so folder names can never collide with technical endpoints)

Video/audio playback happens in VLC using the SAME link as the admin panel's "Copy Link" button
(/dl/<api token>/<stream id>/<name>, served by the untouched original streaming code). The Explorer contains no
streaming code of its own, never converts anything and offers no download for media.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from collections import OrderedDict
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from Backend.config import Telegram
from Backend.fastapi.routes.stream_routes import _content_disposition, select_best_client
from Backend.fastapi.routes.template_routes import _base_context, templates
from Backend.fastapi.security.credentials import get_current_user, require_auth
from Backend.helper.exceptions import FileNotFound
from Backend.helper import explorer_thumbs as TH
from Backend.helper.encrypt import encode_string
from Backend.helper.explorer import (
    KINDS, configured_channels, extract_media, get_indexer, get_store, parse_fid,
)
from Backend.helper.settings_manager import SettingsManager
from Backend import db
from Backend.logger import LOGGER
from Backend.pyrofork.bot import StreamBot, multi_clients

router = APIRouter(tags=["Explorer"])

MEDIA_KINDS = ("video", "audio")
_thumb_cache: "OrderedDict[str, bytes]" = OrderedDict()
_img_cache: "OrderedDict[str, tuple]" = OrderedDict()
_THUMB_MAX, _IMG_MAX = 600, 12
_MAX_IMAGE_BYTES = 40 * 1024 * 1024
_TEXT_PREVIEW_BYTES = 256 * 1024
_VLC_PATH_RE = re.compile(r"^[A-Za-z]:\\[A-Za-z0-9 _.()\-\\]{1,200}\.exe$")
DEFAULT_VLC_PATH = r"C:\Program Files\VideoLAN\VLC\vlc.exe"


#----- helpers -------------------------------------------------------------------------------------
async def _doc_or_404(fid: str) -> dict:
    try:
        parse_fid(fid)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid file id")
    doc = await get_store().get_file(fid)
    if not doc:
        raise HTTPException(status_code=404, detail="File is not in the Explorer index (run a sync)")
    return doc


def _client():
    if not multi_clients:
        raise HTTPException(status_code=503, detail="No Telegram client available")
    return multi_clients[select_best_client(0)]


def _public_base(request: Request) -> str:
    base = (SettingsManager.current().base_url or Telegram.BASE_URL or "").rstrip("/")
    if base:
        return base
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


#----- Same link the admin panel's "Copy Link" button produces: /dl/<api token>/<stream id>/<file name>
async def _dl_path(doc: dict) -> str:
    chat = str(doc["chat_id"])
    if not chat.startswith("-100"):
        raise HTTPException(status_code=422, detail="This chat type can't be streamed (only channels are supported)")
    tokens = await db.get_all_api_tokens()
    token = tokens[0].get("token") if tokens else None
    if not token:
        raise HTTPException(status_code=409, detail="No API token found. Please create an API token on the Dashboard page first.")
    stream_id = await encode_string({"chat_id": int(chat[4:]), "msg_id": int(doc["msg_id"])})
    return f"/dl/{token}/{stream_id}/{quote(doc['name'] or 'stream', safe='')}"


def _lru_put(cache: OrderedDict, key, value, limit: int) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


#----- In-page previews (images / PDFs up to 40 MB) via a plain Telegram download - no streaming code involved
async def _serve_preview(fid: str, doc: dict):
    if doc.get("kind") in MEDIA_KINDS:
        raise HTTPException(status_code=400, detail="Videos and audio open in VLC")
    cached = _img_cache.get(fid)
    if cached:
        data, mime = cached
    else:
        client = _client()
        try:
            msg = await client.get_messages(doc["chat_id"], doc["msg_id"])
            if getattr(msg, "empty", False):
                raise FileNotFound()
            if doc.get("size", 0) > _MAX_IMAGE_BYTES:
                raise HTTPException(status_code=413, detail="Too large to preview - use Download")
            buf = await client.download_media(msg, in_memory=True)
            data, mime = buf.getvalue(), doc.get("mime") or "application/octet-stream"
        except HTTPException:
            raise
        except FileNotFound:
            raise HTTPException(status_code=404, detail="The Telegram message or file no longer exists")
        except Exception as e:
            LOGGER.warning(f"[Explorer] preview fetch failed for {fid}: {e}")
            raise HTTPException(status_code=502, detail="Telegram could not provide this file right now")
        _lru_put(_img_cache, fid, (data, mime), _IMG_MAX)
    headers = {"Content-Disposition": _content_disposition(doc["name"], "inline"),
               "Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"}
    if doc.get("kind") == "image":
        headers["Content-Security-Policy"] = "sandbox"
    return Response(content=data, media_type=mime, headers=headers)


#----- Index / listing -------------------------------------------------------------------------------
@router.get("/api/explorer/summary")
async def api_summary(_: bool = Depends(require_auth)):
    store = get_store()
    await store.ensure_indexes()
    data = await store.summary()
    data["status"] = get_indexer().get_status()
    data["configured_channels"] = configured_channels()
    data["default_vlc_path"] = DEFAULT_VLC_PATH
    data["thumb_generation"] = TH.ENABLED
    data["thumb_installed"] = TH.AVAILABLE
    return data


@router.get("/api/explorer/files")
async def api_files(kind: str = "", channel: Optional[int] = None, folder: str = "", fav: bool = False,
                    q: str = "", ext: str = "", sort: str = "date", order: str = "desc",
                    page: int = 1, limit: int = 60, _: bool = Depends(require_auth)):
    if kind and kind != "all" and kind not in KINDS:
        raise HTTPException(status_code=400, detail="Unknown kind")
    return await get_store().list_files(kind=kind, channel=channel, folder_id=folder or None, fav=fav, q=q,
                                        ext=ext, sort=sort, order=order, page=page, limit=limit)


@router.post("/api/explorer/sync")
async def api_sync(payload: dict | None = None, _: bool = Depends(require_auth)):
    payload = payload or {}
    allowed = configured_channels()
    wanted = payload.get("channels")
    channels = [c for c in allowed if not wanted or c in [int(x) for x in wanted]]
    return await get_indexer().start(StreamBot, channels, full=bool(payload.get("full")))


@router.post("/api/explorer/sync/cancel")
async def api_sync_cancel(_: bool = Depends(require_auth)):
    return {"ok": get_indexer().cancel()}


@router.get("/api/explorer/sync/status")
async def api_sync_status(_: bool = Depends(require_auth)):
    return get_indexer().get_status()


#----- Virtual folders / favorites -----------------------------------------------------------------------
def _err(e: Exception):
    if isinstance(e, LookupError):
        raise HTTPException(status_code=404, detail=str(e))
    raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/explorer/folders")
async def api_folder_create(payload: dict, _: bool = Depends(require_auth)):
    try:
        return await get_store().create_folder(payload.get("name", ""), payload.get("parent_id"))
    except (ValueError, LookupError) as e:
        _err(e)


@router.patch("/api/explorer/folders/{folder_id}")
async def api_folder_update(folder_id: str, payload: dict, _: bool = Depends(require_auth)):
    try:
        return await get_store().update_folder(folder_id, name=payload.get("name"),
                                               parent_id=payload.get("parent_id"), move="parent_id" in payload)
    except (ValueError, LookupError) as e:
        _err(e)


@router.delete("/api/explorer/folders/{folder_id}")
async def api_folder_delete(folder_id: str, _: bool = Depends(require_auth)):
    try:
        return await get_store().delete_folder(folder_id)
    except (ValueError, LookupError) as e:
        _err(e)


@router.post("/api/explorer/move")
async def api_move(payload: dict, _: bool = Depends(require_auth)):
    fids: List[str] = [f for f in (payload.get("file_ids") or []) if isinstance(f, str)]
    try:
        for f in fids:
            parse_fid(f)
        return {"moved": await get_store().move_files(fids, payload.get("folder_id") or None)}
    except (ValueError, LookupError) as e:
        _err(e)


@router.post("/api/explorer/favorite")
async def api_favorite(payload: dict, _: bool = Depends(require_auth)):
    fids: List[str] = [f for f in (payload.get("file_ids") or []) if isinstance(f, str)]
    try:
        for f in fids:
            parse_fid(f)
    except ValueError as e:
        _err(e)
    return {"updated": await get_store().set_favorite(fids, bool(payload.get("value", True)))}


#----- Links: the admin panel's stream link / VLC / Stremio -------------------------------------------------------------
@router.get("/api/explorer/links/{fid}")
async def api_links(fid: str, request: Request, _: bool = Depends(require_auth)):
    doc = await _doc_or_404(fid)
    base = _public_base(request)
    path = await _dl_path(doc)
    is_media = doc.get("kind") in MEDIA_KINDS
    out = {
        "stream_path": path,                       # the page prefixes location.origin, exactly like the admin "Copy Link"
        "stream": f"{base}{path}",
        "download": None if is_media else path,    # no download for video/audio; other files use the same /dl link
        "vlc_playlist": f"{base}/_ex/vlc/{fid}.m3u",
        "vlc_setup": f"{base}/_ex/vlc-setup.reg",
        "stremio": None,
    }
    try:
        entry = await get_store().catalog_entry(doc["chat_id"], doc["msg_id"])
    except Exception as e:
        LOGGER.debug(f"[Explorer] catalog lookup failed: {e}")
        entry = None
    if entry and entry.get("content_id"):
        out["stremio"] = {"url": f"/open/stremio/{entry['media_type']}/{quote(entry['content_id'], safe=':')}",
                          "title": entry.get("title", "")}
    return out


@router.get("/_ex/vlc/{fid}.m3u")
async def vlc_playlist(fid: str, request: Request, _: bool = Depends(require_auth)):
    doc = await _doc_or_404(fid)
    path = await _dl_path(doc)
    title = doc["name"].replace("\n", " ").replace("\r", " ")
    body = f"#EXTM3U\r\n#EXTINF:{doc.get('duration') or -1},{title}\r\n{_public_base(request)}{path}\r\n"
    return Response(content=body, media_type="audio/x-mpegurl", headers={
        "Content-Disposition": _content_disposition("play.m3u", "attachment"),
        "Cache-Control": "no-store",
    })


def build_vlc_reg(vlc_path: str = DEFAULT_VLC_PATH) -> str:
    """Registry file that makes Windows (and thus the browser) launch VLC for vlc://play/<encoded-url> links."""
    if not _VLC_PATH_RE.match(vlc_path or ""):
        vlc_path = DEFAULT_VLC_PATH
    x86 = vlc_path.replace("Program Files\\", "Program Files (x86)\\", 1) if "(x86)" not in vlc_path else vlc_path
    ps = ("$u=[uri]::UnescapeDataString(('%1' -replace '^vlc://play/','').TrimEnd('/')); "
          f"$p='{vlc_path}'; if(!(Test-Path $p)){{$p='{x86}'}}; "
          "Start-Process -FilePath $p -ArgumentList $u")
    cmd = 'powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -Command "' + ps + '"'
    reg_cmd = cmd.replace("\\", "\\\\").replace('"', '\\"')
    return ("Windows Registry Editor Version 5.00\r\n\r\n"
            "[HKEY_CURRENT_USER\\Software\\Classes\\vlc]\r\n@=\"URL:VLC Protocol\"\r\n\"URL Protocol\"=\"\"\r\n\r\n"
            "[HKEY_CURRENT_USER\\Software\\Classes\\vlc\\shell\\open\\command]\r\n"
            f"@=\"{reg_cmd}\"\r\n")


@router.get("/_ex/vlc-setup.reg")
async def vlc_protocol_reg(path: str = DEFAULT_VLC_PATH, _: bool = Depends(require_auth)):
    return Response(content=build_vlc_reg(path), media_type="application/octet-stream", headers={
        "Content-Disposition": _content_disposition("vlc-setup.reg", "attachment")})


#----- In-page preview (images / PDF) --------------------------------------------------------------------------------
@router.get("/_ex/preview/{fid}/{name}")
async def preview(fid: str, name: str, _: bool = Depends(require_auth)):
    return await _serve_preview(fid, await _doc_or_404(fid))


#----- Thumbnails + previews -----------------------------------------------------------------------------------------
_no_tg: dict = {}                            # fid -> expiry: Telegram has no thumbnail stored on this message
_NO_TG_TTL = 6 * 3600
_thumb_sem = asyncio.Semaphore(4)            # never hammer Telegram with a screenful of thumbnail requests at once


async def _telegram_thumb(doc: dict) -> Optional[bytes]:
    """The thumbnail Telegram stored on the message (videos, documents, audio covers, photos). None if there isn't one."""
    client = _client()
    async with _thumb_sem:
        msg = await client.get_messages(doc["chat_id"], doc["msg_id"])
        if getattr(msg, "empty", False):
            return None
        _, obj = extract_media(msg)
        thumbs = getattr(obj, "thumbs", None) if obj else None
        if thumbs:
            buf = await client.download_media(thumbs[-1].file_id, in_memory=True)
            return buf.getvalue()
        if doc.get("attr") == "photo":
            buf = await client.download_media(msg, in_memory=True)
            return buf.getvalue()
    return None


def _jpeg(data: bytes):
    return Response(content=data, media_type="image/jpeg", headers={
        "Cache-Control": "private, max-age=86400", "Content-Security-Policy": "sandbox"})


@router.get("/_ex/thumb/{fid}")
async def thumb(fid: str, _: bool = Depends(require_auth)):
    """200 = JPEG, 202 = being generated (ask again in a few seconds), 302 = catalog poster, 404 = nothing available."""
    doc = await _doc_or_404(fid)
    data = _thumb_cache.get(fid) or TH.cached(fid)
    if data:
        _lru_put(_thumb_cache, fid, data, _THUMB_MAX)
        return _jpeg(data)
    #----- 1) the thumbnail Telegram has for this file
    if _no_tg.get(fid, 0) <= time.time():
        try:
            data = await _telegram_thumb(doc)
        except Exception as e:
            LOGGER.warning(f"[Explorer] thumbnail fetch failed for {fid}: {e}")
            data = None
        if data:
            TH.store(fid, data)
            _lru_put(_thumb_cache, fid, data, _THUMB_MAX)
            return _jpeg(data)
        _no_tg[fid] = time.time() + _NO_TG_TTL
        if len(_no_tg) > 20000:
            _no_tg.clear()
    #----- 2) a frame decoded from the video itself (MKV etc. sent as documents have no Telegram thumbnail)
    if doc.get("kind") == "video" and TH.ENABLED:
        try:
            url = f"http://127.0.0.1:{Telegram.PORT}{await _dl_path(doc)}"
            state = TH.request(fid, url, doc.get("duration") or 0)
        except HTTPException:
            state = "failed"
        if state == "pending":
            return JSONResponse({"status": "pending"}, status_code=202, headers={"Retry-After": "3", "Cache-Control": "no-store"})
        if state == "ready":
            data = TH.cached(fid)
            if data:
                return _jpeg(data)
    #----- 3) fall back to the poster of the matching catalog entry (movies/series known to Stremio)
    poster = ""
    try:
        entry = await get_store().catalog_entry(doc["chat_id"], doc["msg_id"])
        poster = (entry or {}).get("poster") or ""
    except Exception as e:
        LOGGER.debug(f"[Explorer] poster lookup failed for {fid}: {e}")
    if poster.startswith(("http://", "https://")):
        return RedirectResponse(poster, status_code=302, headers={"Cache-Control": "private, max-age=86400"})
    raise HTTPException(status_code=404, detail="No thumbnail")


@router.get("/api/explorer/text/{fid}")
async def text_preview(fid: str, _: bool = Depends(require_auth)):
    doc = await _doc_or_404(fid)
    if doc.get("kind") not in ("document", "subtitle", "other"):
        raise HTTPException(status_code=400, detail="Not a text-like file")
    client = _client()
    try:
        msg = await client.get_messages(doc["chat_id"], doc["msg_id"])
        data = b""
        async for chunk in client.stream_media(msg, limit=1):
            data = bytes(chunk)
            break
    except Exception as e:
        LOGGER.debug(f"[Explorer] text preview failed for {fid}: {e}")
        raise HTTPException(status_code=502, detail="Telegram could not provide this file right now")
    truncated = len(data) >= _TEXT_PREVIEW_BYTES or doc.get("size", 0) > len(data)
    if b"\x00" in data[:4096]:
        raise HTTPException(status_code=415, detail="Binary file - no text preview")
    return {"text": data[:_TEXT_PREVIEW_BYTES].decode("utf-8", "replace"), "truncated": truncated}


#----- Pages: keep these LAST. /explorer and /explorer/<anything> serve the same single-page app; the browser-side
#----- router turns the path into a view (e.g. /explorer/Movies/2024 -> virtual folder Movies > 2024).
def _page(request: Request):
    ctx = _base_context(request)
    ctx["current_user"] = get_current_user(request)
    return templates.TemplateResponse("explorer.html", ctx)


@router.get("/explorer", response_class=HTMLResponse)
async def explorer_page(request: Request, _: bool = Depends(require_auth)):
    return _page(request)


@router.get("/explorer/{path:path}", response_class=HTMLResponse)
async def explorer_page_path(path: str, request: Request, _: bool = Depends(require_auth)):
    return _page(request)


#----- Startup hook (called from main.py) -----------------------------------------------------------------------------
async def explorer_startup() -> None:
    try:
        await get_store().ensure_indexes()
    except Exception as e:
        LOGGER.warning(f"[Explorer] startup index check failed: {e}")
    if os.getenv("EXPLORER_AUTO_SYNC", "false").lower() == "true":
        async def _delayed():
            await asyncio.sleep(60)
            await get_indexer().start(StreamBot, configured_channels(), full=False)
        asyncio.create_task(_delayed())
