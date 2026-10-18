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
    ex._secret = lambda: b"test-secret"
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


async def test_fid_and_signing():
    assert ex.parse_fid(f"{CHAT}_42") == (CHAT, 42)
    for bad in ("", "abc", "1_2_3", "../x_1", "12_a"):
        with pytest.raises(ValueError):
            ex.parse_fid(bad)
    exp, sig = ex.sign_link("1_2", 60)
    assert ex.verify_link("1_2", exp, sig)
    assert not ex.verify_link("1_3", exp, sig)               # different file
    assert not ex.verify_link("1_2", exp, "0" * 32)          # forged
    exp2, sig2 = ex.sign_link("1_2", -5)
    assert not ex.verify_link("1_2", exp2, sig2)             # expired


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
    botmod.multi_clients[0] = object()
    botmod.work_loads[0] = 0
    from Backend.fastapi.routes import explorer_routes as er
    er.multi_clients[0] = botmod.multi_clients[0]
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
    yield SimpleNamespace(base=f"http://127.0.0.1:{port}", files=files)
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


async def test_json_named_file_streams(server, indexed, store, media):
    """A file literally named *.json must stream through /explorer/stream like any other file"""
    j = media["mp4"].parent / "data.json"; j.write_text('{"a": 1}')
    server.files[(CHAT2, 7777)] = j
    await store.upsert_doc(ex.message_to_doc(fake_msg(CHAT2, 7777, "document", name="data.json", size=j.stat().st_size, mime="application/json"), "T"))
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{server.base}/_ex/stream/{CHAT2}_7777/data.json")
        assert r.status_code == 200 and r.json() == {"a": 1}


async def test_byte_range_streaming_matches_file(server, indexed, media):
    fid = indexed["mp4"]; data = media["mp4"].read_bytes(); n = len(data)
    async with httpx.AsyncClient() as c:
        u = f"{server.base}/_ex/stream/{fid}/ok.mp4"
        r = await c.get(u)
        assert r.status_code == 200 and r.content == data and r.headers["accept-ranges"] == "bytes"
        for rng, (s, e) in {"bytes=100-5000": (100, 5000), f"bytes={n // 2}-": (n // 2, n - 1), "bytes=-777": (n - 777, n - 1)}.items():
            r = await c.get(u, headers={"Range": rng})
            assert r.status_code == 206, rng
            assert r.content == data[s:e + 1], rng
            assert r.headers["content-range"] == f"bytes {s}-{e}/{n}"
        h = await c.head(u, headers={"Range": "bytes=0-9"})
        assert h.status_code == 206
        r = await c.get(u, headers={"Range": f"bytes={n + 10}-{n + 20}"})
        assert r.status_code == 416


async def test_signed_links_and_access_control(server, indexed, media):
    fid = indexed["mp4"]; data = media["mp4"].read_bytes()
    async with httpx.AsyncClient() as c:
        L = (await c.get(f"{server.base}/api/explorer/links/{fid}")).json()
        assert L["stream"].startswith("http") and "/_ex/s/" in L["stream"]
        assert "token" not in L["stream"].lower() and "bot" not in L["stream"].lower()  # no Telegram credentials
        r = await c.get(L["stream"].replace("http://127.0.0.1:" + server.base.rsplit(":", 1)[1], server.base))
        assert r.status_code == 200 and r.content == data
        assert L["download"] is None                                   # no download link for video/audio
        r = await c.get(L["stream"] + "?dl=1")
        assert r.status_code == 200 and "attachment" not in r.headers["content-disposition"] and r.content == data   # ?dl=1 ignored for media
        bad = L["stream"].replace("/s/", "/s/").rsplit("/", 3)
        tampered = L["stream"][:-len("ok.mp4") - 1] + "/ok.mp4"
        parts = L["stream"].split("/_ex/s/")[1].split("/")
        forged = f"{server.base}/_ex/s/{parts[0]}/{'0' * 32}/{parts[2]}/{parts[3]}"
        assert (await c.get(forged)).status_code == 403
        expired = f"{server.base}/_ex/s/1/{'0' * 32}/{parts[2]}/{parts[3]}"
        assert (await c.get(expired)).status_code == 403
        assert (await c.get(f"{server.base}/_ex/stream/not-an-id/x")).status_code == 400
        assert (await c.get(f"{server.base}/_ex/stream/{CHAT2}_99999/x")).status_code == 404   # not in index


async def test_vlc_playlist_and_reg(server, indexed):
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{server.base}/_ex/vlc/{indexed['mp4']}.m3u")
        assert r.status_code == 200 and r.text.startswith("#EXTM3U") and "/_ex/s/" in r.text
        assert "attachment" in r.headers["content-disposition"]
        reg = await c.get(f"{server.base}/_ex/vlc-setup.reg")
        assert reg.text.startswith("Windows Registry Editor Version 5.00") and "URL Protocol" in reg.text and "vlc.exe" in reg.text










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


async def test_every_media_type_streams_untouched_for_vlc(server, indexed, media, store):
    """Every video container (mp4/mkv/avi) is served as the original bytes, with range support, via signed link."""
    async with httpx.AsyncClient() as c:
        for k in ("mp4", "mkv", "avi"):
            L = (await c.get(f"{server.base}/api/explorer/links/{indexed[k]}")).json()
            url = L["stream"]; data = media[k].read_bytes()
            r = await c.get(url, headers={"Range": "bytes=1000-9999"})
            assert r.status_code == 206 and r.content == data[1000:10000], k
            m3u = (await c.get(L["vlc_playlist"])).text
            assert url.split("/_ex/s/")[1].split("/")[2] in m3u and m3u.startswith("#EXTM3U"), k
            from urllib.parse import quote
            assert L["vlc_uri"] == "vlc://play/" + quote(url, safe="") and L["download"] is None
    # missing Telegram message -> clean error instead of a crash
    await store.upsert_doc(ex.message_to_doc(fake_msg(CHAT2, 6666, "document", name="gone.mp4", size=10, mime="video/mp4"), "T"))
    async with httpx.AsyncClient() as c:
        assert (await c.get(f"{server.base}/_ex/stream/{CHAT2}_6666/gone.mp4")).status_code in (404, 502)


async def test_no_server_side_conversion_endpoints(server, indexed):
    async with httpx.AsyncClient() as c:
        assert (await c.get(f"{server.base}/api/explorer/probe/{indexed['mp4']}")).status_code == 404


async def test_non_media_download_still_offered(server, indexed, store, media):
    j = media["mp4"].parent / "notes.txt"; j.write_text("hello")
    server.files[(CHAT2, 8888)] = j
    await store.upsert_doc(ex.message_to_doc(fake_msg(CHAT2, 8888, "document", name="notes.txt", size=5, mime="text/plain"), "T"))
    async with httpx.AsyncClient() as c:
        L = (await c.get(f"{server.base}/api/explorer/links/{CHAT2}_8888")).json()
        assert L["download"] and (await c.get(L["download"])).headers["content-disposition"].startswith("attachment")


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
