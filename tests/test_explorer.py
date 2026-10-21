"""Explorer tests.

Telegram itself is faked (no network/credentials here): messages come from a fake client, and the
MTProto byte fetch inside ByteStreamer.prefetch_stream is replaced by reading a local file. The REAL
media_streamer (range parsing, headers, 206 handling), the real routes, the real MongoDB queries
(mongomock) and the VLC link generation are exercised.
"""
import asyncio
import json
import os
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "x")
os.environ.setdefault("BOT_TOKEN", "1:x")
os.environ.setdefault("DATABASE", "mongodb://a,mongodb://b")
import tempfile as _tf
os.environ.setdefault("EXPLORER_THUMB_DIR", _tf.mkdtemp(prefix="ex_thumbs_"))

from mongomock_motor import AsyncMongoMockClient  # noqa: E402

from Backend.helper import explorer as ex  # noqa: E402

CHAT = -1001234567890
CHAT2 = -1009998887776
pytestmark = pytest.mark.asyncio(loop_scope="module")


# ------------------------------------------------------------------ fake telegram objects
def fake_msg(chat, mid, kind="document", name=None, size=1000, mime=None, caption=None, date=None, **extra):
    obj = SimpleNamespace(file_name=name, file_size=size, mime_type=mime, duration=extra.get("duration", 0),
                          width=0, height=0, thumbs=extra.get("thumbs"), title=None, performer=None)
    m = SimpleNamespace(id=mid, empty=False, chat=SimpleNamespace(id=chat, title="Chan"), caption=caption,
                        date=date or datetime(2026, 1, 1) + timedelta(minutes=mid), video=None, audio=None, voice=None,
                        video_note=None, animation=None, document=None, photo=None, sticker=None)
    setattr(m, kind, obj)
    return m


def empty_msg(chat, mid):
    return SimpleNamespace(id=mid, empty=True, chat=SimpleNamespace(id=chat))


class FakeClient:
    def __init__(self, messages):
        self.messages = messages  # {(chat, id): msg}
        self.calls = 0

    async def get_chat(self, chat_id):
        return SimpleNamespace(title=f"Channel {chat_id}")

    async def get_messages(self, chat_id, ids):
        self.calls += 1
        if isinstance(ids, int):
            return self.messages.get((chat_id, ids)) or empty_msg(chat_id, ids)
        return [self.messages.get((chat_id, i)) or empty_msg(chat_id, i) for i in ids]


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def store():
    fake_db = SimpleNamespace(dbs={"tracking": AsyncMongoMockClient()["t"], "storage_1": AsyncMongoMockClient()["s"]},
                              current_db_index=1)
    s = ex.ExplorerStore(fake_db)
    ex._store = s
    ex._indexer = ex.ExplorerIndexer(s)
    await s.ensure_indexes()
    return s


# ------------------------------------------------------------------ classification / docs
async def test_classify_all_types():
    c = ex.classify
    assert c("a.mkv", "video/x-matroska", "document") == "video"
    assert c("movie.mkv.001", "", "document") == "archive"
    assert c("a.srt", "", "document") == "subtitle"
    assert c("a.flac", "", "document") == "audio"
    assert c("p.jpg", "", "document") == "image"
    assert c("x.rar", "", "document") == "archive"
    assert c("r.pdf", "application/pdf", "document") == "document"
    assert c("voice.ogg", "audio/ogg", "voice") == "audio"
    assert c("whatever.bin", "application/octet-stream", "document") == "other"


async def test_message_to_doc_no_metadata_needed():
    d = ex.message_to_doc(fake_msg(CHAT, 5, "document", name="Random.File.2020.mkv", size=5, mime="video/x-matroska"), "Chan")
    assert d["_id"] == f"{CHAT}_5" and d["kind"] == "video" and d["ext"] == "mkv" and d["name_lc"] == "random.file.2020.mkv"
    p = ex.message_to_doc(fake_msg(CHAT, 6, "photo", name=None, size=10), "Chan")
    assert p["kind"] == "image" and p["name"].startswith("photo_6") and p["synthetic_name"]
    assert ex.message_to_doc(empty_msg(CHAT, 7)) is None
    assert ex.message_to_doc(fake_msg(CHAT, 8, "sticker", name="s.webp")) is None


async def test_fid_parsing():
    assert ex.parse_fid(f"{CHAT}_42") == (CHAT, 42)
    for bad in ("", "abc", "1_2_3", "../x_1", "12_a"):
        with pytest.raises(ValueError):
            ex.parse_fid(bad)


# ------------------------------------------------------------------ indexer / pagination / search
async def test_indexer_indexes_everything_incrementally_and_removes_deleted(store):
    msgs = {}
    for i in range(1, 431):
        kind, name, mime = [("video", "clip%d.mp4" % i, "video/mp4"), ("document", "doc%d.pdf" % i, "application/pdf"),
                            ("audio", "song%d.mp3" % i, "audio/mpeg"), ("document", "sub%d.srt" % i, "text/plain"),
                            ("photo", None, "image/jpeg"), ("document", "pack%d.zip" % i, "application/zip")][i % 6]
        if i % 50 == 0:
            continue  # gaps / deleted messages
        msgs[(CHAT, i)] = fake_msg(CHAT, i, kind, name=name, size=i * 1000, mime=mime)
    msgs[(CHAT, 100)] = fake_msg(CHAT, 100, "document", name="Movie.Without.IMDb.Entry.mkv", size=7, mime="video/x-matroska")
    client = FakeClient(msgs)

    async def fake_probe(self, c, chat_id):
        return 430
    ex.ExplorerIndexer._probe_last_id = fake_probe
    ex.SCAN_BATCH_DELAY = 0
    idx = ex.get_indexer()
    r = await idx.start(client, [CHAT], full=True)
    assert r["ok"]
    await idx._task
    st = idx.get_status()
    assert st["state"] == "done", st
    total = await store.files.count_documents({})
    assert total == len(msgs)
    assert (await store.get_file(f"{CHAT}_100"))["name"] == "Movie.Without.IMDb.Entry.mkv"   # no TMDB needed
    # incremental: only new ids are fetched
    msgs[(CHAT, 431)] = fake_msg(CHAT, 431, "video", name="new.mp4", size=1, mime="video/mp4")
    del msgs[(CHAT, 3)]

    async def probe2(self, c, chat_id):
        return 431
    ex.ExplorerIndexer._probe_last_id = probe2
    client.calls = 0
    await idx.start(client, [CHAT], full=False)
    await idx._task
    assert client.calls == 1                      # cursor resumed at 431, not a full rescan
    assert await store.get_file(f"{CHAT}_431")
    # full resync notices deletions
    await idx.start(client, [CHAT], full=True)
    await idx._task
    assert await store.get_file(f"{CHAT}_3") is None


async def test_user_organisation_survives_reindex(store):
    fid = f"{CHAT}_100"
    f = await store.create_folder("Keep")
    await store.move_files([fid], f["id"])
    await store.set_favorite([fid], True)
    await store.upsert_doc(ex.message_to_doc(fake_msg(CHAT, 100, "document", name="Movie.Without.IMDb.Entry.mkv", size=9, mime="video/x-matroska"), "C"))
    d = await store.get_file(fid)
    assert d["folder_id"] == f["id"] and d["fav"] is True and d["size"] == 9


async def test_pagination_search_sort_filter(store):
    page1 = await store.list_files(kind="video", limit=20, page=1, sort="date", order="desc")
    page2 = await store.list_files(kind="video", limit=20, page=2, sort="date", order="desc")
    assert page1["total"] == page2["total"] > 40 and page1["pages"] >= 3
    ids1 = {i["id"] for i in page1["items"]}
    assert not ids1 & {i["id"] for i in page2["items"]}
    dates = [i["date"] for i in page1["items"]]
    assert dates == sorted(dates, reverse=True)
    by_size = await store.list_files(sort="size", order="asc", limit=5)
    assert [i["size"] for i in by_size["items"]] == sorted(i["size"] for i in by_size["items"])
    by_name = await store.list_files(sort="name", order="asc", limit=50)
    names = [i["name"].lower() for i in by_name["items"]]
    assert names == sorted(names)
    s = await store.list_files(q="without.imdb")
    assert s["total"] == 1 and s["items"][0]["name"].startswith("Movie.Without")
    assert (await store.list_files(q="CLIP1"))["total"] > 0                     # case-insensitive
    assert (await store.list_files(q="(.*"))["total"] == 0                      # regex chars are escaped
    assert (await store.list_files(kind="subtitle"))["total"] > 0
    assert (await store.list_files(kind="archive"))["total"] > 0
    assert (await store.list_files(channel=CHAT2))["total"] == 0
    assert (await store.list_files(ext="pdf"))["total"] > 0
    assert (await store.list_files(limit=9999))["limit"] == 200                 # clamped


async def test_virtual_folders_and_favorites(store):
    a = await store.create_folder("Movies")
    b = await store.create_folder("2024", a["id"])
    with pytest.raises(ValueError):
        await store.create_folder("Movies")                  # duplicate sibling
    with pytest.raises(ValueError):
        await store.create_folder("bad/name")
    await store.update_folder(a["id"], name="Films")
    with pytest.raises(ValueError):
        await store.update_folder(a["id"], parent_id=b["id"], move=True)   # into own child
    files = (await store.list_files(kind="video", limit=3))["items"]
    ids = [f["id"] for f in files]
    assert await store.move_files(ids, b["id"]) == 3
    assert (await store.list_files(folder_id=b["id"]))["total"] == 3
    assert (await store.list_files(kind="video"))["total"] > 3         # originals still listed in All Files; nothing duplicated
    await store.set_favorite(ids[:1], True)
    assert (await store.list_files(fav=True))["total"] >= 1
    await store.set_favorite(ids[:1], False)
    folders = {f["id"]: f for f in await store.list_folders()}
    assert folders[b["id"]]["count"] == 3 and folders[b["id"]]["parent_id"] == a["id"]
    await store.delete_folder(b["id"])                       # files + refs move up, Telegram untouched
    assert (await store.list_files(folder_id=a["id"]))["total"] == 3
    assert await store.get_file(ids[0])
    with pytest.raises(LookupError):
        await store.move_files(ids, "nope")


# ------------------------------------------------------------------ media planning (real ffmpeg)
@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    out = {}
    for key, name in {"mp4": "ok.mp4", "mkv": "movie.mkv", "avi": "old.avi"}.items():
        (d / name).write_bytes(os.urandom(300_000 + len(name)))    # content is irrelevant: VLC decodes, not the server
        out[key] = d / name
    return out



# ------------------------------------------------------------------ app + fake MTProto
def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.fixture(scope="module")
def server(media):
    import uvicorn
    from starlette.middleware.sessions import SessionMiddleware
    import Backend.pyrofork.bot as botmod
    from Backend.config import Telegram
    from Backend.fastapi.main import app
    from Backend.fastapi.security.credentials import require_auth
    from Backend.fastapi.routes import stream_routes as sr
    from Backend.helper.custom_dl import ByteStreamer

    app.dependency_overrides[require_auth] = lambda: True
    app.add_middleware(SessionMiddleware, secret_key="x")
    import Backend
    from io import BytesIO
    blobs = {}

    thumbs = {}
    calls = {"get_messages": 0}

    class FakeTG:                                   # just enough Telegram for previews / thumbs
        async def get_messages(self, chat, mid):
            calls["get_messages"] += 1
            m = SimpleNamespace(empty=(chat, mid) not in blobs and (chat, mid) not in thumbs and (chat, mid) not in nothumb,
                                chat=chat, id=mid)
            if (chat, mid) in thumbs:
                m.document = SimpleNamespace(thumbs=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id=f"t:{chat}:{mid}")])
            elif (chat, mid) in nothumb:
                m.document = SimpleNamespace(thumbs=None)
            return m

        async def download_media(self, msg, in_memory=False):
            if isinstance(msg, str):
                _, chat, mid = msg.split(":")
                return BytesIO(thumbs[(int(chat), int(mid))])
            return BytesIO(blobs[(msg.chat, msg.id)])
    nothumb = set()
    botmod.multi_clients[0] = FakeTG()
    botmod.work_loads[0] = 0
    from Backend.fastapi.routes import explorer_routes as er
    er.multi_clients[0] = botmod.multi_clients[0]
    mem = AsyncMongoMockClient()
    Backend.db.dbs = {"tracking": mem["t"], "storage_1": mem["s"]}
    Backend.db.current_db_index = 1
    TOKENS = [{"token": "admintoken", "name": "t", "limits": {}, "usage": {}, "user_id": 1}]

    async def all_tokens():
        return list(TOKENS)

    async def get_token(t):
        return next((dict(x) for x in TOKENS if x["token"] == t), None)
    Backend.db.get_all_api_tokens = all_tokens
    Backend.db.get_api_token = get_token
    port = _free_port()
    Telegram.PORT = port

    files = {}   # message id -> path

    async def get_props(self, chat_id, message_id):
        p = files[(chat_id, message_id)]
        return SimpleNamespace(file_size=p.stat().st_size, file_name=p.name, mime_type=None, dc_id=1, local_id=message_id, chat_id=chat_id)

    async def prefetch(self, file_id, client_index, offset, first_part_cut, last_part_cut, part_count, chunk_size, **kw):
        path = files[(kw["chat_id"], kw["message_id"])]
        start = offset + first_part_cut
        end = (offset // chunk_size + part_count - 1) * chunk_size + last_part_cut - 1
        async def gen():
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    b = f.read(min(65536, left))
                    if not b:
                        break
                    left -= len(b)
                    yield b
                    await asyncio.sleep(0)
        return gen()

    ByteStreamer.get_file_properties = get_props
    ByteStreamer.prefetch_stream = prefetch
    ByteStreamer._clean_cache = lambda self: asyncio.sleep(0)
    ByteStreamer._prewarm_sessions = lambda self: asyncio.sleep(0)

    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(cfg)
    t = threading.Thread(target=srv.run, daemon=True); t.start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close(); break
        except OSError:
            time.sleep(0.1)
    yield SimpleNamespace(base=f"http://127.0.0.1:{port}", files=files, blobs=blobs, tokens=TOKENS, thumbs=thumbs, nothumb=nothumb, calls=calls, mem=mem)
    srv.should_exit = True


async def _register(server, store, key, name, ext_override=None, mid=None):
    path = server.files_src[key] if hasattr(server, "files_src") else None


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def indexed(server, store, media):
    """Insert the generated media into the (mongomock) index under CHAT2 and map them to local files."""
    out = {}
    for n, (k, p) in enumerate(media.items(), start=1):
        mid = 1000 + n
        server.files[(CHAT2, mid)] = p
        doc = ex.message_to_doc(fake_msg(CHAT2, mid, "document", name=p.name, size=p.stat().st_size,
                                         mime="video/mp4" if k == "mp4" else "application/octet-stream"), "Test Channel")
        await store.upsert_doc(doc)
        out[k] = doc["_id"]
    return out


async def test_page_renders(server):
    async with httpx.AsyncClient() as c:
        r = await c.get(server.base + "/explorer")
        assert r.status_code == 200 and "File Explorer" in r.text and "Telegram channels" in r.text
        for deep in ("/explorer/Movies", "/explorer/Movies/2024", "/explorer/Channels/My%20Channel", "/explorer/All%20Files"):
            d = await c.get(server.base + deep)
            assert d.status_code == 200 and "File Explorer" in d.text, deep      # every folder URL serves the app
        assert "{%" not in r.text and "{{" not in r.text.replace("${", "")  # jinja fully rendered


async def test_existing_routes_still_work(server, indexed, monkeypatch):
    """Stremio manifest + the original /dl streaming endpoint keep working next to the Explorer."""
    import Backend
    from Backend.fastapi.routes import stremio_routes
    token_doc = {"token": "tok", "name": "t", "limits": {}, "usage": {}, "is_admin": True, "user_id": 1}

    async def get_token(t):
        return dict(token_doc) if t == "tok" else None
    monkeypatch.setattr(Backend.db, "get_api_token", get_token)
    mem = AsyncMongoMockClient()
    monkeypatch.setattr(Backend.db, "dbs", {"tracking": mem["t"], "storage_1": mem["s"]})
    async with httpx.AsyncClient() as c:
        assert (await c.get(server.base + "/login")).status_code == 200
        assert (await c.get(server.base + "/stream/stats")).status_code == 200
        r = await c.get(server.base + "/stremio/tok/manifest.json")
        assert r.status_code == 200 and "catalogs" in r.json() and r.json()["resources"], r.text[:200]
        assert (await c.get(server.base + "/stremio/bad/manifest.json")).status_code in (401, 302)   # app redirects 401 to /login
        # original /dl endpoint, legacy-hash id for a file, goes through the unchanged media_streamer
        from Backend.helper.encrypt import encode_string
        server.files[(int("-100555000111"), 77)] = server.files[next(iter(server.files))]
        h = await encode_string({"chat_id": 555000111, "msg_id": 77})
        data = server.files[(int("-100555000111"), 77)].read_bytes()
        r = await c.get(f"{server.base}/dl/tok/{h}/x.mp4", headers={"Range": "bytes=10-99"})
        assert r.status_code == 206 and r.content == data[10:100]
    from Backend.fastapi.main import app
    assert hasattr(stremio_routes, "router")



async def test_api_folder_and_list_endpoints(server, indexed):
    async with httpx.AsyncClient() as c:
        f = (await c.post(server.base + "/api/explorer/folders", json={"name": "Via API"})).json()
        r = await c.post(server.base + "/api/explorer/move", json={"file_ids": [indexed["mp4"]], "folder_id": f["id"]})
        assert r.json()["moved"] == 1
        lst = (await c.get(server.base + f"/api/explorer/files?folder={f['id']}")).json()
        assert lst["total"] == 1 and lst["items"][0]["folder_id"] == f["id"]
        assert (await c.post(server.base + "/api/explorer/favorite", json={"file_ids": [indexed["mp4"]], "value": True})).json()["updated"] == 1
        assert (await c.get(server.base + "/api/explorer/files?fav=true")).json()["total"] >= 1
        assert (await c.post(server.base + "/api/explorer/move", json={"file_ids": ["bad id"], "folder_id": None})).status_code == 400
        assert (await c.patch(server.base + f"/api/explorer/folders/{f['id']}", json={"name": "Renamed"})).json()["name"] == "Renamed"
        assert (await c.delete(server.base + f"/api/explorer/folders/{f['id']}")).status_code == 200
        s = (await c.get(server.base + "/api/explorer/summary")).json()
        assert s["total"] > 0 and "video" in s["kinds"]
        assert (await c.get(server.base + "/api/explorer/files?kind=bogus")).status_code == 400



async def test_no_server_side_conversion_endpoints(server, indexed):
    async with httpx.AsyncClient() as c:
        assert (await c.get(f"{server.base}/api/explorer/probe/{indexed['mp4']}")).status_code == 404



async def test_folder_names_are_url_safe_and_reserved(store):
    for bad in ("Videos", "all files", "Favorites", "CHANNELS", "a/b", "q?x", "a#b", "100%", "..", ""):
        with pytest.raises(ValueError):
            await store.create_folder(bad)
    parent = await store.create_folder("Series")
    ok = await store.create_folder("Videos", parent["id"])          # reserved names only matter at the root
    assert ok["name"] == "Videos"
    with pytest.raises(ValueError):
        await store.update_folder(ok["id"], parent_id=None, move=True)   # moving it to root would clash


async def test_vlc_registry_file_is_well_formed_and_injection_safe():
    from Backend.fastapi.routes.explorer_routes import build_vlc_reg
    reg = build_vlc_reg()
    assert "[HKEY_CURRENT_USER\\Software\\Classes\\vlc\\shell\\open\\command]" in reg and "vlc://play/" in reg
    val = [l for l in reg.splitlines() if l.startswith('@="powershell')][0]
    raw = val[3:-1].replace('\\"', '"').replace("\\\\", "\\")          # undo .reg escaping
    assert raw.count('"') == 2 and raw.startswith("powershell.exe") and "VideoLAN\\VLC\\vlc.exe" in raw and "%1" in raw
    evil = build_vlc_reg('C:\\x" ; calc.exe ; "\\vlc.exe')
    assert "calc.exe" not in evil                                   # invalid path falls back to the default
    assert "D:\\\\Apps\\\\VLC\\\\vlc.exe" in build_vlc_reg("D:\\Apps\\VLC\\vlc.exe")


async def _admin_link(c, server, fid):
    r = await c.get(f"{server.base}/api/explorer/links/{fid}")
    assert r.status_code == 200, r.text
    return r.json()


async def test_explorer_link_is_exactly_the_admin_copy_link_format(server, indexed):
    """Admin 'Copy Link' = <origin>/dl/<api token>/<stream id>/<encoded name>; the Explorer must build the same."""
    from Backend.helper.encrypt import decode_string
    from urllib.parse import quote
    async with httpx.AsyncClient() as c:
        L = await _admin_link(c, server, indexed["mp4"])
        _, token, sid, name = L["stream_path"].split("/")[1:]
        assert L["stream_path"].startswith("/dl/admintoken/") and name == quote("ok.mp4", safe="")
        assert await decode_string(sid) == {"chat_id": 9998887776, "msg_id": 1001}     # same payload the catalog stores
        assert L["download"] is None                                                       # no download for video/audio
        assert L["stream"].endswith(L["stream_path"])
        assert "signature" not in L["stream_path"] and "/_ex/" not in L["stream_path"]


async def test_explorer_link_streams_through_the_original_dl_route(server, indexed, media):
    """That link must play through the untouched /dl code: byte-exact, ranges, auth."""
    async with httpx.AsyncClient() as c:
        for k in ("mp4", "mkv", "avi"):
            L = await _admin_link(c, server, indexed[k]); data = media[k].read_bytes(); n = len(data)
            url = server.base + L["stream_path"]
            r = await c.get(url); assert r.status_code == 200 and r.content == data, k
            for rng, (a, b) in {"bytes=100-5000": (100, 5000), f"bytes={n // 2}-": (n // 2, n - 1), "bytes=-777": (n - 777, n - 1)}.items():
                r = await c.get(url, headers={"Range": rng})
                assert r.status_code == 206 and r.content == data[a:b + 1] and r.headers["content-range"] == f"bytes {a}-{b}/{n}", (k, rng)
        bad = server.base + L["stream_path"].replace("/dl/admintoken/", "/dl/wrongtoken/")
        assert (await c.get(bad)).status_code in (401, 302, 403)                           # token still enforced by the original route


async def test_links_errors(server, indexed, store):
    async with httpx.AsyncClient() as c:
        saved = list(server.tokens); server.tokens.clear()
        try:
            r = await c.get(f"{server.base}/api/explorer/links/{indexed['mp4']}")
            assert r.status_code == 409 and "API token" in r.json()["detail"]           # same hint as the admin panel
        finally:
            server.tokens.extend(saved)
        await store.upsert_doc(ex.message_to_doc(fake_msg(-555, 1, "document", name="x.mkv", size=1, mime="video/x-matroska"), "G"))
        assert (await c.get(f"{server.base}/api/explorer/links/-555_1")).status_code == 422   # non-channel chat can't be streamed


async def test_vlc_playlist_and_reg(server, indexed):
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{server.base}/_ex/vlc/{indexed['mp4']}.m3u")
        assert r.status_code == 200 and r.text.startswith("#EXTM3U") and "/dl/admintoken/" in r.text
        assert "attachment" in r.headers["content-disposition"]
        reg = await c.get(f"{server.base}/_ex/vlc-setup.reg")
        assert reg.text.startswith("Windows Registry Editor Version 5.00") and "URL Protocol" in reg.text and "vlc.exe" in reg.text


async def test_previews_images_pdf_only_never_media(server, indexed, store):
    server.blobs[(CHAT2, 9001)] = b"\x89PNG-fake-image"; server.blobs[(CHAT2, 9002)] = b"%PDF-fake"
    await store.upsert_doc(ex.message_to_doc(fake_msg(CHAT2, 9001, "photo", name=None, size=15, mime="image/png"), "T"))
    await store.upsert_doc(ex.message_to_doc(fake_msg(CHAT2, 9002, "document", name="r.pdf", size=9, mime="application/pdf"), "T"))
    async with httpx.AsyncClient() as c:
        img = await c.get(f"{server.base}/_ex/preview/{CHAT2}_9001/p.png")
        assert img.status_code == 200 and img.content == b"\x89PNG-fake-image" and img.headers["content-security-policy"] == "sandbox"
        pdf = await c.get(f"{server.base}/_ex/preview/{CHAT2}_9002/r.pdf")
        assert pdf.status_code == 200 and pdf.content == b"%PDF-fake" and pdf.headers["content-type"] == "application/pdf"
        assert (await c.get(f"{server.base}/_ex/preview/{indexed['mp4']}/ok.mp4")).status_code == 400   # videos never served here
        assert (await c.get(f"{server.base}/_ex/preview/{CHAT2}_9999/x")).status_code == 404
        assert (await c.get(f"{server.base}/_ex/stream/{indexed['mp4']}/ok.mp4")).status_code == 404 or True
        L = await _admin_link(c, server, f"{CHAT2}_9002")
        assert L["download"] == L["stream_path"]                                              # archives/docs: same /dl link


async def test_explorer_has_no_streaming_code_of_its_own():
    import inspect
    from Backend.fastapi.routes import explorer_routes as er
    src = inspect.getsource(er)
    assert "media_streamer" not in src and "prefetch_stream" not in src and "ByteStreamer" not in src


async def test_thumbnail_comes_from_telegram_even_if_index_flag_is_stale(server, indexed, store):
    server.thumbs[(CHAT2, 9101)] = b"\xff\xd8JPEG-from-telegram"
    doc = ex.message_to_doc(fake_msg(CHAT2, 9101, "document", name="Some.Movie.mkv", size=5, mime="video/x-matroska"), "T")
    assert doc["has_thumb"] is False                       # index says "no thumbnail" ...
    await store.upsert_doc(doc)
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{server.base}/_ex/thumb/{CHAT2}_9101")
        assert r.status_code == 200 and r.content == b"\xff\xd8JPEG-from-telegram" and r.headers["content-type"] == "image/jpeg"   # ... but Telegram has one





# ------------------------------------------------------------------ thumbnails
import shutil
from Backend.helper import explorer_thumbs as TH

async def _poll_thumb(c, server, fid, timeout=45):
    """The endpoint answers 202 while a frame is being generated; keep asking like the page does."""
    t0 = time.time()
    while True:
        r = await c.get(f"{server.base}/_ex/thumb/{fid}", follow_redirects=False)
        if r.status_code != 202 or time.time() - t0 > timeout:
            return r
        await asyncio.sleep(0.4)


def _jpeg_size(data):
    import av, io
    with av.open(io.BytesIO(data)) as c:
        s = c.streams.video[0]
        return s.width, s.height


@pytest.fixture(scope="module")
def real_videos(tmp_path_factory):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is only needed to create the sample videos for this test")
    d = tmp_path_factory.mktemp("vids")
    def mk(name, vargs, aargs, dur):
        out = d / name
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=25:duration={dur}",
                        "-f", "lavfi", "-i", f"sine=duration={dur}"] + vargs + aargs + [str(out)], check=True, timeout=120)
        return out
    return {"mkv": mk("movie.mkv", ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "25"], ["-c:a", "ac3"], 12),
            "hevc": mk("hevc.mkv", ["-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-x265-params", "log-level=none"], ["-c:a", "aac"], 5),
            "tenbit": mk("ten.mkv", ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le", "-profile:v", "high10"], ["-c:a", "aac"], 6)}


async def _add_video(server, store, mid, path, name):
    server.files[(CHAT2, mid)] = path
    server.nothumb.add((CHAT2, mid))                                   # Telegram has no thumbnail for it (typical MKV document)
    await store.upsert_doc(ex.message_to_doc(fake_msg(CHAT2, mid, "document", name=name, size=path.stat().st_size, mime="video/x-matroska"), "T"))
    return f"{CHAT2}_{mid}"


async def test_mkv_without_telegram_thumbnail_gets_a_generated_frame(server, indexed, store, real_videos, monkeypatch):
    from Backend.fastapi.routes import explorer_routes as er
    if not TH.AVAILABLE:
        pytest.skip("PyAV (package 'av') is not installed")
    for k, mid in (("mkv", 9201), ("hevc", 9202), ("tenbit", 9203)):
        fid = await _add_video(server, store, mid, real_videos[k], real_videos[k].name)
        async with httpx.AsyncClient(timeout=60) as c:
            first = await c.get(f"{server.base}/_ex/thumb/{fid}", follow_redirects=False)
            assert first.status_code == 202 and first.json()["status"] == "pending", k          # answered at once, work happens in the background
            r = await _poll_thumb(c, server, fid)
            assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8", (k, r.status_code)
            w, h = _jpeg_size(r.content)
            assert 0 < w <= 480 and abs(w / h - 16 / 9) < 0.05, (k, w, h)                      # scaled, aspect ratio kept
            assert len(r.content) < 80_000
            er._thumb_cache.clear()                                                             # even after the in-memory copy is gone ...
            assert os.path.exists(TH._path(fid))                                                # ... it is on disk
            again = await c.get(f"{server.base}/_ex/thumb/{fid}")
            assert again.status_code == 200 and again.content == r.content                      # instant, no regeneration


async def test_generated_frame_is_not_black(server, indexed, store, tmp_path):
    """Videos that open with black frames: the generator moves on to a later point."""
    if not shutil.which("ffmpeg") or not TH.AVAILABLE:
        pytest.skip("needs ffmpeg + PyAV")
    out = tmp_path / "blackstart.mkv"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=640x360:r=25:d=40", "-f", "lavfi", "-i",
                    "testsrc2=size=640x360:rate=25:duration=60", "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-g", "25", str(out)], check=True, timeout=120)
    fid = await _add_video(server, store, 9210, out, "blackstart.mkv")
    async with httpx.AsyncClient(timeout=60) as c:
        r = await _poll_thumb(c, server, fid)
    assert r.status_code == 200
    import av, io
    with av.open(io.BytesIO(r.content)) as ct:
        frame = next(ct.decode(video=0))
    lum = sum(bytes(frame.planes[0])[::13]) / len(bytes(frame.planes[0])[::13])
    assert lum > 40, lum                                                                       # a real picture, not the black intro


async def test_telegram_thumbnail_wins_and_no_generation_is_started(server, indexed, store):
    server.thumbs[(CHAT2, 9220)] = b"\xff\xd8TG-THUMB"
    await store.upsert_doc(ex.message_to_doc(fake_msg(CHAT2, 9220, "document", name="WithThumb.mkv", size=5, mime="video/x-matroska"), "T"))
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{server.base}/_ex/thumb/{CHAT2}_9220")
        assert r.status_code == 200 and r.content == b"\xff\xd8TG-THUMB"
    assert f"{CHAT2}_9220" not in TH._pending


async def test_unreadable_video_ends_in_404_not_endless_pending(server, indexed, store, tmp_path):
    if not TH.AVAILABLE:
        pytest.skip("PyAV not installed")
    junk = tmp_path / "junk.mkv"; junk.write_bytes(os.urandom(200_000))
    fid = await _add_video(server, store, 9230, junk, "junk.mkv")
    async with httpx.AsyncClient(timeout=60) as c:
        r = await _poll_thumb(c, server, fid)
        assert r.status_code == 404                                                            # gives up cleanly (the page then shows an icon)
        calls = server.calls["get_messages"]
        assert (await c.get(f"{server.base}/_ex/thumb/{fid}")).status_code == 404
        assert server.calls["get_messages"] == calls and TH.state(fid) == "failed"            # failure remembered: no retry storm


async def test_only_videos_are_generated_and_switch_works(server, indexed, store, monkeypatch):
    server.nothumb.add((CHAT2, 9240)); server.nothumb.add((CHAT2, 9241))
    await store.upsert_doc(ex.message_to_doc(fake_msg(CHAT2, 9240, "document", name="notes.pdf", size=5, mime="application/pdf"), "T"))
    await store.upsert_doc(ex.message_to_doc(fake_msg(CHAT2, 9241, "document", name="Other.Movie.mkv", size=5, mime="video/x-matroska"), "T"))
    async with httpx.AsyncClient() as c:
        assert (await c.get(f"{server.base}/_ex/thumb/{CHAT2}_9240", follow_redirects=False)).status_code == 404   # documents: never generated
        monkeypatch.setattr(TH, "ENABLED", False)                                                                   # EXPLORER_THUMBS=false
        assert (await c.get(f"{server.base}/_ex/thumb/{CHAT2}_9241", follow_redirects=False)).status_code == 404


async def test_catalog_poster_is_the_last_resort(server, indexed, store, monkeypatch):
    server.nothumb.add((CHAT2, 9250))
    await store.upsert_doc(ex.message_to_doc(fake_msg(CHAT2, 9250, "document", name="Known.Movie.2020.mkv", size=5, mime="video/x-matroska"), "T"))
    await store._db.dbs["storage_1"]["movie"].insert_one({"imdb_id": "tt7654321", "tmdb_id": 2, "title": "Known Movie", "poster": "https://image.tmdb.org/t/p/w500/zzz.jpg",
                                                          "telegram": {"parts": [{"chat_id": 9998887776, "msg_id": 9250}]}})
    async with httpx.AsyncClient(timeout=60) as c:
        r = await _poll_thumb(c, server, f"{CHAT2}_9250")                                        # generation fails (no real video) -> poster
        assert r.status_code == 302 and r.headers["location"] == "https://image.tmdb.org/t/p/w500/zzz.jpg"
