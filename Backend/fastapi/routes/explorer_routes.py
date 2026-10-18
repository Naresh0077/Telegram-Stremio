"""File Explorer routes: /explorer page, /api/explorer/* JSON API and streaming endpoints.

Video/audio playback is done in VLC (signed link / playlist); no server-side conversion.
Streaming deliberately reuses stream_routes.media_streamer (the exact Telegram/MTProto path Stremio
uses: range requests, multi-client pool, prefetch, file-reference refresh) instead of re-implementing it.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from Backend.fastapi.routes.stream_routes import _content_disposition, media_streamer, select_best_client
from Backend.fastapi.routes.template_routes import _base_context, templates
from Backend.fastapi.security.credentials import get_current_user, require_auth
from Backend.helper.exceptions import FileNotFound
from Backend.helper.explorer import (
    KINDS, LINK_TTL, configured_channels, extract_media, get_indexer, get_store, parse_fid,
    public_doc, sign_link, verify_link,
)
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER
from Backend.config import Telegram
from Backend.pyrofork.bot import StreamBot, multi_clients

router = APIRouter(tags=["Explorer"])

_thumb_cache: "OrderedDict[str, bytes]" = OrderedDict()
_img_cache: "OrderedDict[str, tuple]" = OrderedDict()
_THUMB_MAX, _IMG_MAX = 600, 12
_MAX_IMAGE_BYTES = 40 * 1024 * 1024
_TEXT_PREVIEW_BYTES = 256 * 1024


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


def _signed_path(fid: str, name: str, ttl: int = LINK_TTL) -> tuple:
    exp, sig = sign_link(fid, ttl)
    return f"/explorer/s/{exp}/{sig}/{fid}/{quote(name, safe='')}", exp


def _lru_put(cache: OrderedDict, key, value, limit: int) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


#----- Core Telegram byte-range streaming (shared by session + signed endpoints) ---------------------
async def _serve_file(request: Request, fid: str, disposition: str = "inline"):
    doc = await _doc_or_404(fid)
    if not multi_clients:
        raise HTTPException(status_code=503, detail="No Telegram client available")
    if doc.get("attr") == "photo":
        return await _serve_image(fid, doc, disposition)
    try:
        resp = await media_streamer(request=request, chat_id=doc["chat_id"], msg_id=doc["msg_id"],
                                    token="explorer", token_data=None, stream_id_hash=None)
    except HTTPException:
        raise
    except FileNotFound:
        raise HTTPException(status_code=404, detail="The Telegram message or file no longer exists")
    except Exception as e:
        LOGGER.warning(f"[Explorer] stream failed for {fid}: {e}")
        raise HTTPException(status_code=502, detail="Telegram could not provide this file right now")
    resp.headers["Content-Disposition"] = _content_disposition(doc["name"], disposition)
    resp.headers["Cache-Control"] = "private, max-age=300"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


async def _serve_image(fid: str, doc: dict, disposition: str = "inline"):
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
                raise HTTPException(status_code=413, detail="Image too large to preview; use Download")
            buf = await client.download_media(msg, in_memory=True)
            data, mime = buf.getvalue(), doc.get("mime") or "image/jpeg"
        except HTTPException:
            raise
        except FileNotFound:
            raise HTTPException(status_code=404, detail="The Telegram message or file no longer exists")
        except Exception as e:
            LOGGER.warning(f"[Explorer] image fetch failed for {fid}: {e}")
            raise HTTPException(status_code=502, detail="Telegram could not provide this image right now")
        _lru_put(_img_cache, fid, (data, mime), _IMG_MAX)
    return Response(content=data, media_type=mime, headers={
        "Content-Disposition": _content_disposition(doc["name"], disposition),
        "Cache-Control": "private, max-age=300",
        "Content-Security-Policy": "sandbox",
        "X-Content-Type-Options": "nosniff",
    })


#----- Page ------------------------------------------------------------------------------------------
@router.get("/explorer", response_class=HTMLResponse)
async def explorer_page(request: Request, _: bool = Depends(require_auth)):
    ctx = _base_context(request)
    ctx["current_user"] = get_current_user(request)
    ctx["kinds"] = list(KINDS)
    return templates.TemplateResponse("explorer.html", ctx)


#----- Index / listing -------------------------------------------------------------------------------
@router.get("/api/explorer/summary")
async def api_summary(_: bool = Depends(require_auth)):
    store = get_store()
    await store.ensure_indexes()
    data = await store.summary()
    data["status"] = get_indexer().get_status()
    data["configured_channels"] = configured_channels()
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


#----- Links: stream / download / VLC / Stremio -------------------------------------------------------------
@router.get("/api/explorer/links/{fid}")
async def api_links(fid: str, request: Request, _: bool = Depends(require_auth)):
    doc = await _doc_or_404(fid)
    base = _public_base(request)
    path, exp = _signed_path(fid, doc["name"])
    stream = f"{base}{path}"
    out = {
        "stream": stream,
        "download": f"{stream}?dl=1",
        "vlc_playlist": f"{base}/explorer/vlc/{fid}.m3u",
        "vlc_uri": f"vlc://{stream}",
        "vlc_setup": f"{base}/explorer/vlc-setup.reg",
        "expires_at": exp,
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


@router.get("/explorer/vlc/{fid}.m3u")
async def vlc_playlist(fid: str, request: Request, _: bool = Depends(require_auth)):
    doc = await _doc_or_404(fid)
    path, _exp = _signed_path(fid, doc["name"])
    title = doc["name"].replace("\n", " ").replace("\r", " ")
    body = f"#EXTM3U\r\n#EXTINF:{doc.get('duration') or -1},{title}\r\n{_public_base(request)}{path}\r\n"
    return Response(content=body, media_type="audio/x-mpegurl", headers={
        "Content-Disposition": _content_disposition("stream.m3u", "attachment"),
        "Cache-Control": "no-store",
    })


@router.get("/explorer/vlc-setup.reg")
async def vlc_protocol_reg(_: bool = Depends(require_auth)):
    ps = ("$u='%1' -replace '^vlc://',''; "
          "$p='C:\\Program Files\\VideoLAN\\VLC\\vlc.exe'; "
          "if(!(Test-Path $p)){$p='C:\\Program Files (x86)\\VideoLAN\\VLC\\vlc.exe'}; "
          "& $p $u")
    cmd = 'powershell.exe -NoProfile -WindowStyle Hidden -Command "' + ps + '"'
    reg_cmd = cmd.replace("\\", "\\\\").replace('"', '\\"')
    reg = ("Windows Registry Editor Version 5.00\r\n\r\n"
           "[HKEY_CURRENT_USER\\Software\\Classes\\vlc]\r\n@=\"URL:VLC Protocol\"\r\n\"URL Protocol\"=\"\"\r\n\r\n"
           "[HKEY_CURRENT_USER\\Software\\Classes\\vlc\\shell\\open\\command]\r\n"
           f"@=\"{reg_cmd}\"\r\n")
    return Response(content=reg, media_type="application/octet-stream", headers={
        "Content-Disposition": _content_disposition("vlc-protocol.reg", "attachment")})


#----- Raw bytes (range-capable) ---------------------------------------------------------------------------
@router.get("/explorer/stream/{fid}/{name}")
@router.head("/explorer/stream/{fid}/{name}")
async def stream_session(request: Request, fid: str, name: str, dl: int = 0, _: bool = Depends(require_auth)):
    return await _serve_file(request, fid, "attachment" if dl else "inline")


@router.get("/explorer/s/{exp}/{sig}/{fid}/{name}")
@router.head("/explorer/s/{exp}/{sig}/{fid}/{name}")
async def stream_signed(request: Request, exp: int, sig: str, fid: str, name: str, dl: int = 0):
    if not verify_link(fid, exp, sig):
        raise HTTPException(status_code=403, detail="Link expired or invalid")
    return await _serve_file(request, fid, "attachment" if dl else "inline")


#----- Thumbnails + previews -----------------------------------------------------------------------------------------
@router.get("/explorer/thumb/{fid}")
async def thumb(fid: str, _: bool = Depends(require_auth)):
    doc = await _doc_or_404(fid)
    data = _thumb_cache.get(fid)
    if data is None:
        if not doc.get("has_thumb"):
            raise HTTPException(status_code=404, detail="No thumbnail")
        client = _client()
        try:
            msg = await client.get_messages(doc["chat_id"], doc["msg_id"])
            _, obj = extract_media(msg)
            thumbs = getattr(obj, "thumbs", None) if obj else None
            if thumbs:
                buf = await client.download_media(thumbs[-1].file_id, in_memory=True)
            elif doc.get("attr") == "photo":
                buf = await client.download_media(msg, in_memory=True)
            else:
                raise HTTPException(status_code=404, detail="No thumbnail")
            data = buf.getvalue()
        except HTTPException:
            raise
        except Exception as e:
            LOGGER.debug(f"[Explorer] thumb failed for {fid}: {e}")
            raise HTTPException(status_code=404, detail="Thumbnail unavailable")
        _lru_put(_thumb_cache, fid, data, _THUMB_MAX)
    return Response(content=data, media_type="image/jpeg", headers={
        "Cache-Control": "private, max-age=86400", "Content-Security-Policy": "sandbox"})


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


#----- Startup hook (called from main.py) -----------------------------------------------------------------------------
async def explorer_startup() -> None:
    try:
        await get_store().ensure_indexes()
    except Exception as e:
        LOGGER.warning(f"[Explorer] startup index check failed: {e}")
    import os
    if os.getenv("EXPLORER_AUTO_SYNC", "false").lower() == "true":
        async def _delayed():
            await asyncio.sleep(60)
            await get_indexer().start(StreamBot, configured_channels(), full=False)
        asyncio.create_task(_delayed())
