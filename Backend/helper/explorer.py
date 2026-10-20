"""File Explorer backend: Telegram attachment index, virtual folders, favorites and incremental channel indexer.

Everything lives in the existing `tracking` MongoDB database in new collections
(`explorer_files`, `explorer_folders`, `explorer_state`). No second Telegram client
is created - the existing StreamBot / Userbot are reused.
"""
from __future__ import annotations

import asyncio
import mimetypes
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from pyrogram.errors import ChannelPrivate, ChatAdminRequired, FloodWait

from Backend.logger import LOGGER

#----- Config (environment, all optional)
SCAN_BATCH_SIZE = 200
SCAN_MAX_EMPTY_BATCHES = 10
SCAN_MAX_ID_CAP = 5_000_000
SCAN_BATCH_DELAY = 0.5

FILES = "explorer_files"
FOLDERS = "explorer_folders"
STATE = "explorer_state"

KINDS = ("video", "audio", "image", "document", "archive", "subtitle", "other")

VIDEO_EXT = {"mp4", "mkv", "avi", "mov", "webm", "m4v", "ts", "m2ts", "mts", "wmv", "flv", "mpg", "mpeg", "3gp", "ogv", "vob", "divx"}
AUDIO_EXT = {"mp3", "flac", "wav", "aac", "m4a", "ogg", "oga", "opus", "wma", "aiff", "ac3", "dts", "mka", "amr"}
IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "tif", "heic", "heif", "avif", "svg", "ico"}
SUBTITLE_EXT = {"srt", "vtt", "ass", "ssa", "sub", "idx", "sup"}
ARCHIVE_EXT = {"zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "iso", "zst", "cab", "z01", "z02"}
DOC_EXT = {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md", "rtf", "odt", "ods", "odp", "csv", "json",
           "xml", "html", "htm", "epub", "mobi", "azw3", "log", "nfo", "yaml", "yml", "ini", "cbz", "cbr"}
SPLIT_PART_RE = re.compile(r"\.\d{3}$")
FID_RE = re.compile(r"^-?\d+_\d+$")
MEDIA_ATTRS = ("video", "audio", "voice", "video_note", "animation", "document", "photo")


#----- Root-level folder names that would clash with built-in /explorer/<view> URLs
RESERVED_ROOT_NAMES = {"home", "all files", "videos", "audio", "images", "documents", "archives", "subtitles",
                       "other", "favorites", "folders", "channels"}


def check_folder_name(name: str, parent_id: Optional[str]) -> str:
    name = (name or "").strip()
    if not name or len(name) > 120 or any(c in name for c in '/\\?#%') or name in (".", ".."):
        raise ValueError("Invalid folder name (no / \\ ? # % characters)")
    if not parent_id and name.lower() in RESERVED_ROOT_NAMES:
        raise ValueError(f"'{name}' is a built-in view name - choose another name")
    return name


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


#----- Extension (lowercase, no dot) of a filename
def ext_of(name: str) -> str:
    base = (name or "").rsplit("/", 1)[-1]
    if "." not in base:
        return ""
    return base.rsplit(".", 1)[-1].lower()


#----- Map a file to one of KINDS
def classify(name: str, mime: str, attr: str) -> str:
    ext = ext_of(name)
    mime = (mime or "").lower()
    if ext in SUBTITLE_EXT:
        return "subtitle"
    if SPLIT_PART_RE.search(name or ""):
        return "archive"
    if attr in ("video", "animation", "video_note"):
        return "video"
    if attr in ("audio", "voice"):
        return "audio"
    if attr == "photo":
        return "image"
    if ext in VIDEO_EXT or mime.startswith("video/"):
        return "video"
    if ext in AUDIO_EXT or mime.startswith("audio/"):
        return "audio"
    if ext in IMAGE_EXT or mime.startswith("image/"):
        return "image"
    if ext in ARCHIVE_EXT or "zip" in mime or "rar" in mime or "x-7z" in mime or "x-tar" in mime:
        return "archive"
    if ext in DOC_EXT or mime.startswith("text/") or mime in ("application/pdf", "application/json", "application/epub+zip"):
        return "document"
    return "other"


#----- fid <-> (chat_id, msg_id). fid is "<full chat id>_<message id>", e.g. "-1001234567890_42"
def make_fid(chat_id: int, msg_id: int) -> str:
    return f"{int(chat_id)}_{int(msg_id)}"


def parse_fid(fid: str) -> Tuple[int, int]:
    if not fid or not FID_RE.match(fid):
        raise ValueError("invalid file id")
    chat, msg = fid.rsplit("_", 1)
    return int(chat), int(msg)


#----- Pull the first media object off a pyrogram message
def extract_media(message) -> Tuple[Optional[str], Any]:
    for attr in MEDIA_ATTRS:
        obj = getattr(message, attr, None)
        if obj:
            return attr, obj
    return None, None


_SYNTH_EXT = {"voice": "ogg", "video_note": "mp4", "animation": "mp4", "photo": "jpg", "video": "mp4", "audio": "mp3"}


#----- Convert a pyrogram Message into an index document (None when it has no attachment)
def message_to_doc(message, channel_name: str = "") -> Optional[dict]:
    if message is None or getattr(message, "empty", False):
        return None
    attr, obj = extract_media(message)
    if obj is None or attr == "sticker":
        return None
    chat_id = int(message.chat.id) if getattr(message, "chat", None) else 0
    msg_id = int(message.id)
    name = getattr(obj, "file_name", None) or ""
    synthetic = False
    mime = getattr(obj, "mime_type", None) or ""
    if not name:
        synthetic = True
        if attr == "audio" and (getattr(obj, "title", None) or getattr(obj, "performer", None)):
            stem = " - ".join(x for x in (getattr(obj, "performer", None), getattr(obj, "title", None)) if x)
        else:
            stem = f"{attr}_{msg_id}"
        guessed = mimetypes.guess_extension(mime) if mime else None
        name = f"{stem}{guessed or '.' + _SYNTH_EXT.get(attr, 'bin')}"
    if not mime:
        mime = mimetypes.guess_type(name)[0] or ("image/jpeg" if attr == "photo" else "application/octet-stream")
    caption = (getattr(message, "caption", None) or "")[:500]
    date = getattr(message, "date", None)
    if isinstance(date, datetime) and date.tzinfo is not None:
        date = date.astimezone(timezone.utc).replace(tzinfo=None)
    return {
        "_id": make_fid(chat_id, msg_id),
        "chat_id": chat_id,
        "msg_id": msg_id,
        "name": name,
        "name_lc": name.lower(),
        "caption": caption,
        "caption_lc": caption.lower(),
        "ext": ext_of(name),
        "size": int(getattr(obj, "file_size", 0) or 0),
        "mime": mime,
        "kind": classify(name, mime, attr),
        "attr": attr,
        "date": date or _now(),
        "duration": int(getattr(obj, "duration", 0) or 0),
        "width": int(getattr(obj, "width", 0) or 0),
        "height": int(getattr(obj, "height", 0) or 0),
        "has_thumb": bool(getattr(obj, "thumbs", None)) or attr == "photo",
        "synthetic_name": synthetic,
        "channel_name": channel_name or "",
    }


#----- JSON-friendly view of an index document
def public_doc(doc: dict) -> dict:
    d = doc.get("date")
    return {
        "id": doc["_id"],
        "chat_id": doc["chat_id"],
        "msg_id": doc["msg_id"],
        "name": doc["name"],
        "ext": doc.get("ext", ""),
        "size": doc.get("size", 0),
        "mime": doc.get("mime", ""),
        "kind": doc.get("kind", "other"),
        "attr": doc.get("attr", ""),
        "date": (d.replace(tzinfo=timezone.utc).isoformat() if isinstance(d, datetime) else None),
        "caption": doc.get("caption", ""),
        "duration": doc.get("duration", 0),
        "width": doc.get("width", 0),
        "height": doc.get("height", 0),
        "has_thumb": bool(doc.get("has_thumb")),
        "channel": doc.get("channel_name") or str(doc.get("chat_id")),
        "folder_id": doc.get("folder_id"),
        "favorite": bool(doc.get("fav")),
    }


#----- Index + virtual folder store
class ExplorerStore:
    def __init__(self, db) -> None:
        self._db = db
        self._indexes_ready = False

    @property
    def _tracking(self):
        return self._db.dbs["tracking"]

    @property
    def files(self):
        return self._tracking[FILES]

    @property
    def folders(self):
        return self._tracking[FOLDERS]

    @property
    def state(self):
        return self._tracking[STATE]

    async def ensure_indexes(self) -> None:
        if self._indexes_ready:
            return
        try:
            f = self.files
            await f.create_index([("chat_id", 1), ("msg_id", -1)])
            await f.create_index([("kind", 1), ("date", -1)])
            await f.create_index([("date", -1)])
            await f.create_index([("name_lc", 1)])
            await f.create_index([("size", -1)])
            await f.create_index([("folder_id", 1)])
            await f.create_index([("fav", 1)])
            self._indexes_ready = True
        except Exception as e:
            LOGGER.warning(f"[Explorer] index creation failed: {e}")

    #----- Index writes
    async def upsert_doc(self, doc: dict) -> None:
        d = dict(doc)
        _id = d.pop("_id")
        d["indexed_at"] = _now()
        #----- folder_id / fav are only set on insert so re-indexing never undoes the user's organisation
        await self.files.update_one(
            {"_id": _id},
            {"$set": d, "$setOnInsert": {"folder_id": None, "fav": False}},
            upsert=True,
        )

    async def delete_messages(self, chat_id: int, msg_ids: List[int]) -> int:
        if not msg_ids:
            return 0
        res = await self.files.delete_many({"chat_id": int(chat_id), "msg_id": {"$in": [int(m) for m in msg_ids]}})
        return getattr(res, "deleted_count", 0)

    async def get_file(self, fid: str) -> Optional[dict]:
        return await self.files.find_one({"_id": fid})

    #----- Listing / search / pagination
    @staticmethod
    def build_query(kind: str = "", channel: Optional[int] = None, folder_id: Optional[str] = None,
                    fav: bool = False, q: str = "", ext: str = "") -> dict:
        query: Dict[str, Any] = {}
        if kind and kind != "all":
            query["kind"] = kind
        if channel is not None:
            query["chat_id"] = int(channel)
        if folder_id:
            query["folder_id"] = folder_id
        if fav:
            query["fav"] = True
        if ext:
            query["ext"] = ext.lower().lstrip(".")
        q = (q or "").strip().lower()
        if q:
            pattern = re.escape(q)
            query["$or"] = [{"name_lc": {"$regex": pattern}}, {"caption_lc": {"$regex": pattern}}]
        return query

    async def list_files(self, *, kind: str = "", channel: Optional[int] = None, folder_id: Optional[str] = None,
                         fav: bool = False, q: str = "", ext: str = "", sort: str = "date", order: str = "desc",
                         page: int = 1, limit: int = 60) -> dict:
        query = self.build_query(kind, channel, folder_id, fav, q, ext)
        limit = max(1, min(int(limit or 60), 200))
        page = max(1, int(page or 1))
        direction = 1 if order == "asc" else -1
        sort_spec = {
            "name": [("name_lc", direction)],
            "size": [("size", direction), ("name_lc", 1)],
            "date": [("date", direction), ("msg_id", direction)],
            "type": [("ext", direction), ("name_lc", 1)],
        }.get(sort, [("date", -1)])
        total = await self.files.count_documents(query)
        cursor = self.files.find(query).sort(sort_spec).skip((page - 1) * limit).limit(limit)
        items = [public_doc(d) async for d in cursor]
        return {"items": items, "total": total, "page": page, "limit": limit,
                "pages": max(1, -(-total // limit))}

    async def summary(self) -> dict:
        kinds = {k: 0 for k in KINDS}
        async for row in self.files.aggregate([{"$group": {"_id": "$kind", "n": {"$sum": 1}}}]):
            kinds[row["_id"] or "other"] = row["n"]
        channels = []
        async for row in self.files.aggregate([
            {"$group": {"_id": "$chat_id", "n": {"$sum": 1}, "name": {"$last": "$channel_name"}, "size": {"$sum": "$size"}}},
            {"$sort": {"n": -1}},
        ]):
            channels.append({"chat_id": row["_id"], "name": row.get("name") or str(row["_id"]),
                             "count": row["n"], "size": row.get("size", 0)})
        favs = await self.files.count_documents({"fav": True})
        total = await self.files.count_documents({})
        return {"total": total, "kinds": kinds, "channels": channels, "favorites": favs,
                "folders": await self.list_folders()}

    #----- Virtual folders (nested, stored separately from Telegram)
    async def list_folders(self) -> List[dict]:
        out = []
        async for f in self.folders.find({}).sort("name_lc", 1):
            n = await self.files.count_documents({"folder_id": f["_id"]})
            out.append({"id": f["_id"], "name": f["name"], "parent_id": f.get("parent_id"), "count": n})
        return out

    async def create_folder(self, name: str, parent_id: Optional[str] = None) -> dict:
        name = check_folder_name(name, parent_id)
        if parent_id and not await self.folders.find_one({"_id": parent_id}):
            raise ValueError("Parent folder not found")
        if await self.folders.find_one({"parent_id": parent_id or None, "name_lc": name.lower()}):
            raise ValueError("A folder with that name already exists here")
        doc = {"_id": uuid.uuid4().hex[:12], "name": name, "name_lc": name.lower(),
               "parent_id": parent_id or None, "created_at": _now()}
        await self.folders.insert_one(doc)
        return {"id": doc["_id"], "name": name, "parent_id": doc["parent_id"], "count": 0}

    async def _descendants(self, folder_id: str) -> List[str]:
        out, stack = [], [folder_id]
        while stack:
            cur = stack.pop()
            async for c in self.folders.find({"parent_id": cur}, {"_id": 1}):
                out.append(c["_id"])
                stack.append(c["_id"])
        return out

    async def update_folder(self, folder_id: str, name: Optional[str] = None,
                            parent_id: Optional[str] = None, move: bool = False) -> dict:
        folder = await self.folders.find_one({"_id": folder_id})
        if not folder:
            raise LookupError("Folder not found")
        upd: Dict[str, Any] = {}
        new_parent = folder.get("parent_id")
        if move:
            if parent_id and (parent_id == folder_id or parent_id in await self._descendants(folder_id)):
                raise ValueError("Cannot move a folder into itself")
            if parent_id and not await self.folders.find_one({"_id": parent_id}):
                raise ValueError("Target folder not found")
            new_parent = parent_id or None
            check_folder_name(folder["name"], new_parent)
            upd["parent_id"] = new_parent
        new_name = folder["name"]
        if name is not None:
            new_name = check_folder_name(name, new_parent)
            upd.update({"name": new_name, "name_lc": new_name.lower()})
        clash = await self.folders.find_one({"parent_id": new_parent, "name_lc": new_name.lower(), "_id": {"$ne": folder_id}})
        if clash:
            raise ValueError("A folder with that name already exists here")
        if upd:
            await self.folders.update_one({"_id": folder_id}, {"$set": upd})
        return {"id": folder_id, "name": new_name, "parent_id": new_parent}

    async def delete_folder(self, folder_id: str) -> dict:
        """Deletes the folder only; subfolders and files move up one level. Telegram files are never touched."""
        folder = await self.folders.find_one({"_id": folder_id})
        if not folder:
            raise LookupError("Folder not found")
        parent = folder.get("parent_id")
        await self.folders.update_many({"parent_id": folder_id}, {"$set": {"parent_id": parent}})
        res = await self.files.update_many({"folder_id": folder_id}, {"$set": {"folder_id": parent}})
        await self.folders.delete_one({"_id": folder_id})
        return {"moved_files": getattr(res, "modified_count", 0)}

    async def move_files(self, fids: List[str], folder_id: Optional[str]) -> int:
        if folder_id and not await self.folders.find_one({"_id": folder_id}):
            raise LookupError("Folder not found")
        res = await self.files.update_many({"_id": {"$in": list(fids)}}, {"$set": {"folder_id": folder_id or None}})
        return getattr(res, "modified_count", 0)

    async def set_favorite(self, fids: List[str], value: bool) -> int:
        res = await self.files.update_many({"_id": {"$in": list(fids)}}, {"$set": {"fav": bool(value)}})
        return getattr(res, "modified_count", 0)

    #----- Catalog (Stremio) lookup for the "Open in Stremio" action
    async def catalog_entry(self, chat_id: int, msg_id: int) -> Optional[dict]:
        from Backend.helper.encrypt import encode_string
        short = int(str(chat_id).replace("-100", "", 1)) if str(chat_id).startswith("-100") else int(chat_id)
        try:
            legacy = await encode_string({"chat_id": short, "msg_id": msg_id})
        except Exception:
            legacy = None
        part_match = {"$elemMatch": {"chat_id": short, "msg_id": msg_id}}
        proj = {"imdb_id": 1, "tmdb_id": 1, "title": 1, "poster": 1}
        for i in range(1, self._db.current_db_index + 1):
            storage = self._db.dbs.get(f"storage_{i}")
            if storage is None:
                continue
            m_or = [{"telegram.parts": part_match}] + ([{"telegram.id": legacy}] if legacy else [])
            doc = await storage["movie"].find_one({"$or": m_or}, proj)
            if doc:
                return {"media_type": "movie", **self._catalog_ids(doc)}
            t_or = [{"seasons.episodes.telegram.parts": part_match}] + ([{"seasons.episodes.telegram.id": legacy}] if legacy else [])
            doc = await storage["tv"].find_one({"$or": t_or}, proj)
            if doc:
                return {"media_type": "series", **self._catalog_ids(doc)}
        return None

    @staticmethod
    def _catalog_ids(doc: dict) -> dict:
        imdb = doc.get("imdb_id") or ""
        content_id = imdb if str(imdb).startswith("tt") else (f"tmdb:{doc.get('tmdb_id')}" if doc.get("tmdb_id") is not None else "")
        return {"content_id": content_id, "title": doc.get("title") or "", "poster": doc.get("poster") or ""}


#----- Incremental channel indexer (bots cannot read history, so - like the existing scanner -
#----- it walks message-id ranges with get_messages and resumes from a stored cursor)
class ExplorerIndexer:
    def __init__(self, store: ExplorerStore) -> None:
        self.store = store
        self._task: Optional[asyncio.Task] = None
        self._cancel = False
        self.status: Dict[str, Any] = self._blank()

    @staticmethod
    def _blank() -> Dict[str, Any]:
        return {"state": "idle", "current": None, "current_name": "", "progress": 0,
                "counters": {"scanned": 0, "indexed": 0, "removed": 0, "errors": 0},
                "started_at": 0, "finished_at": 0, "error": None}

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def get_status(self) -> dict:
        s = dict(self.status)
        s["counters"] = dict(s["counters"])
        s["running"] = self.running
        return s

    def cancel(self) -> bool:
        if not self.running:
            return False
        self._cancel = True
        return True

    async def start(self, client, channels: List[int], full: bool = False) -> dict:
        if self.running:
            return {"ok": False, "message": "An index run is already in progress."}
        if not channels:
            return {"ok": False, "message": "No channels configured (add AUTH_CHANNEL / manual channels in Settings)."}
        self._cancel = False
        self.status = self._blank()
        self.status.update({"state": "running", "started_at": time.time()})
        self._task = asyncio.create_task(self._run(client, channels, full))
        return {"ok": True, "message": "Indexing started."}

    async def _run(self, client, channels: List[int], full: bool) -> None:
        try:
            await self.store.ensure_indexes()
            for ch in channels:
                if self._cancel:
                    break
                await self._index_channel(client, int(ch), full)
            self.status["state"] = "cancelled" if self._cancel else "done"
        except (ChannelPrivate, ChatAdminRequired) as e:
            self.status.update({"state": "error", "error": f"Access denied - the bot must be a member/admin of the channel ({e})"})
        except asyncio.CancelledError:
            self.status["state"] = "cancelled"
            raise
        except Exception as e:
            LOGGER.error(f"[Explorer] indexer failed: {e}", exc_info=True)
            self.status.update({"state": "error", "error": str(e)})
        finally:
            self.status["finished_at"] = time.time()
            self.status["current"] = None

    async def _probe_last_id(self, client, chat_id: int) -> Optional[int]:
        import Backend.pyrofork.bot as botmod
        ub = botmod.Userbot
        if ub is not None:
            try:
                async for m in ub.get_chat_history(chat_id, limit=1):
                    return int(m.id)
            except Exception as e:
                LOGGER.debug(f"[Explorer] userbot history probe failed for {chat_id}: {e}")
        try:
            from Backend.helper.scan_manager import scan_manager
            return await scan_manager._probe_last_message_id(client, chat_id)
        except Exception as e:
            LOGGER.warning(f"[Explorer] last-id probe failed for {chat_id}: {e}")
            return None

    async def _get_cursor(self, chat_id: int) -> int:
        doc = await self.store.state.find_one({"_id": f"cursor:{chat_id}"})
        return int((doc or {}).get("cursor", 1) or 1)

    async def _set_cursor(self, chat_id: int, cursor: int) -> None:
        await self.store.state.update_one({"_id": f"cursor:{chat_id}"},
                                          {"$set": {"cursor": int(cursor), "updated_at": _now()}}, upsert=True)

    async def _index_channel(self, client, chat_id: int, full: bool) -> None:
        st = self.status
        name = str(chat_id)
        try:
            chat = await client.get_chat(chat_id)
            name = getattr(chat, "title", None) or name
        except (ChannelPrivate, ChatAdminRequired):
            raise
        except Exception as e:
            LOGGER.warning(f"[Explorer] could not resolve channel {chat_id}: {e}")
        st["current"], st["current_name"], st["progress"] = chat_id, name, 0

        if full:
            await self._set_cursor(chat_id, 1)
        current = await self._get_cursor(chat_id)
        last_id = await self._probe_last_id(client, chat_id)
        use_probe = bool(last_id and last_id >= 1)
        LOGGER.info(f"[Explorer] indexing {name} ({chat_id}) from id {current}"
                    + (f" to {last_id}" if use_probe else " (heuristic mode)"))

        empty_streak, max_seen = 0, current - 1
        while not self._cancel and current < SCAN_MAX_ID_CAP:
            if use_probe and current > last_id:
                break
            if not use_probe and empty_streak >= SCAN_MAX_EMPTY_BATCHES:
                break
            upper = min(current + SCAN_BATCH_SIZE, SCAN_MAX_ID_CAP)
            if use_probe:
                upper = min(upper, last_id + 1)
            ids = list(range(current, upper))
            try:
                messages = await client.get_messages(chat_id, ids)
            except FloodWait as e:
                LOGGER.info(f"[Explorer] FloodWait {e.value}s")
                await asyncio.sleep(e.value)
                continue
            except Exception as e:
                LOGGER.error(f"[Explorer] batch {current}-{upper} failed: {e}")
                st["counters"]["errors"] += 1
                current, empty_streak = upper, empty_streak + 1
                continue
            if not isinstance(messages, list):
                messages = [messages]

            gone: List[int] = []
            had_content = False
            for mid, m in zip(ids, messages):
                st["counters"]["scanned"] += 1
                if m is None or getattr(m, "empty", False):
                    gone.append(mid)
                    continue
                had_content = True
                max_seen = max(max_seen, mid)
                doc = message_to_doc(m, name)
                if doc:
                    await self.store.upsert_doc(doc)
                    st["counters"]["indexed"] += 1
            if gone:
                st["counters"]["removed"] += await self.store.delete_messages(chat_id, gone)

            empty_streak = 0 if had_content else empty_streak + 1
            current = upper
            await self._set_cursor(chat_id, current if use_probe else max_seen + 1)
            if use_probe and last_id:
                st["progress"] = min(100, int(current / last_id * 100))
            await asyncio.sleep(SCAN_BATCH_DELAY)
        st["progress"] = 100 if not self._cancel else st["progress"]


#----- Module-level singletons (bound on first use so import never needs the DB)
_store: Optional[ExplorerStore] = None
_indexer: Optional[ExplorerIndexer] = None


def get_store() -> ExplorerStore:
    global _store
    if _store is None:
        from Backend import db
        _store = ExplorerStore(db)
    return _store


def get_indexer() -> ExplorerIndexer:
    global _indexer
    if _indexer is None:
        _indexer = ExplorerIndexer(get_store())
    return _indexer


#----- Channels the Explorer may index: AUTH_CHANNEL + manual channels
def configured_channels() -> List[int]:
    from Backend.helper.settings_manager import SettingsManager
    s = SettingsManager.current()
    out: List[int] = []
    for c in list(s.auth_channels) + list(s.manual_channels):
        c = str(c).strip()
        if c.lstrip("-").isdigit():
            v = int(c)
            if v not in out:
                out.append(v)
    return out
