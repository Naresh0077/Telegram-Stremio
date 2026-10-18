import asyncio, os, sys, random, hashlib, threading, socket, time
sys.path.insert(0, os.getcwd())
os.environ.update(API_ID="1", API_HASH="x", BOT_TOKEN="1:x", DATABASE="mongodb://a,mongodb://b")
from types import SimpleNamespace
import httpx, uvicorn
from fastapi import FastAPI, Request
import Backend.pyrofork.bot as botmod
from Backend.helper.custom_dl import ByteStreamer
from Backend.fastapi.routes import stream_routes as sr

CH=1024*1024
SIZE = 5*CH + 12345             # not a multiple of the chunk size
DATA = os.urandom(SIZE)
SIZE2 = 3*CH                    # exact multiple
DATA2 = os.urandom(SIZE2)
files = {1: DATA, 2: DATA2}

class FakeSession:
    def __init__(self, data): self.data=data
    async def send(self, req):
        off, lim = req.offset, req.limit
        assert off % 4096 == 0 and lim in (CH,) , (off, lim)         # Telegram's own GetFile rules
        return SimpleNamespace(bytes=self.data[off:off+lim])

async def get_props(self, chat_id, message_id):
    d = files[message_id]
    return SimpleNamespace(file_size=len(d), file_name="x.mkv", mime_type="video/x-matroska", dc_id=1, local_id=message_id, chat_id=chat_id, _d=d)
async def media_session(self, file_id): return FakeSession(file_id._d)
async def location(file_id): return object()
ByteStreamer.get_file_properties = get_props
ByteStreamer._get_media_session = media_session
ByteStreamer._get_location = staticmethod(location)
ByteStreamer._clean_cache = lambda s: asyncio.sleep(0)
ByteStreamer._prewarm_sessions = lambda s: asyncio.sleep(0)
botmod.multi_clients[0]=object(); botmod.work_loads[0]=0
sr.multi_clients[0]=botmod.multi_clients[0]

app = FastAPI()
@app.get("/f/{mid}/{name}")
@app.head("/f/{mid}/{name}")
async def f(request: Request, mid: int, name: str):
    return await sr.media_streamer(request, -100123, mid, "t", None, None)

def port():
    s=socket.socket(); s.bind(("127.0.0.1",0)); p=s.getsockname()[1]; s.close(); return p
P=port()
srv=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=P,log_level="error")); threading.Thread(target=srv.run,daemon=True).start()
for _ in range(50):
    try: socket.create_connection(("127.0.0.1",P),.2).close(); break
    except OSError: time.sleep(.1)

async def main():
    bad=[]; n=0
    cases=[]
    for mid,size in ((1,SIZE),(2,SIZE2)):
        cases += [(mid,0,size-1),(mid,0,0),(mid,size-1,size-1),(mid,size-100,size-1)]
        for k in range(0,size//CH+1):
            b=k*CH
            for s,e in ((b,b),(b,b+1),(b-1,b),(b-1,b+1),(b,b+CH-1),(b,b+CH),(b-5,b+CH-1),(max(0,b-1),min(size-1,b+CH))):
                if 0<=s<=e<size: cases.append((mid,s,e))
        random.seed(7)
        for _ in range(150):
            s=random.randrange(size); e=random.randrange(s,size); cases.append((mid,s,e))
    async with httpx.AsyncClient(timeout=30) as c:
        for mid,s,e in cases:
            d=files[mid]; n+=1
            exp=d[s:e+1]
            try:
                r=await c.get(f"http://127.0.0.1:{P}/f/{mid}/x.mkv",headers={"Range":f"bytes={s}-{e}"})
                if r.status_code!=206 or r.content!=exp:
                    bad.append((mid,s,e,len(exp),len(r.content),r.status_code))
            except Exception as ex:
                bad.append((mid,s,e,len(exp),"SHORT/ERR",type(ex).__name__))
        # open-ended & suffix ranges like players use
        for mid in (1,2):
            d=files[mid]
            for rng,exp in ((f"bytes=0-",d),(f"bytes={len(d)//2}-",d[len(d)//2:]),("bytes=-65536",d[-65536:]),("bytes=-1",d[-1:])):
                n+=1
                try:
                    r=await c.get(f"http://127.0.0.1:{P}/f/{mid}/x.mkv",headers={"Range":rng})
                    if r.content!=exp: bad.append((mid,rng,len(exp),len(r.content),r.status_code))
                except Exception as ex: bad.append((mid,rng,"SHORT/ERR",type(ex).__name__))
        n+=1
        try:
            r=await c.get(f"http://127.0.0.1:{P}/f/1/x.mkv")
            if r.content!=DATA: bad.append(("full",len(DATA),len(r.content)))
        except Exception as ex: bad.append(("full","SHORT/ERR",type(ex).__name__))
    print(f"{n} range requests through the REAL media_streamer/prefetch_stream; mismatches: {len(bad)}")
    for b in bad[:40]:
        if isinstance(b[1],int):
            print("  BAD", b, " start%CH=",b[1]%CH," end%CH=",b[2]%CH)
        else: print("  BAD", b)
asyncio.get_event_loop().run_until_complete(main()) if False else asyncio.run(main())
