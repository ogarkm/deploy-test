import os
import importlib.util
import json
import socket
import threading
import uvicorn
import time
import asyncio
import uuid
from urllib.parse import urlparse
from contextlib import asynccontextmanager
import re
import shutil
import subprocess
import signal
import traceback
import zipfile
import io
from fastapi import FastAPI, HTTPException, Query, Body, status, UploadFile, File, APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from functools import wraps
import time
import httpx
from fastapi.responses import StreamingResponse, JSONResponse, Response
from zeroconf import ServiceInfo, Zeroconf
from fpdf import FPDF
import base64

# --- Tunnel Configuration ---
TUNNEL_TOKEN = os.environ.get("TUNNEL_TOKEN", "change-this-secret-token")
active_tunnel: Optional[WebSocket] = None
pending_requests: Dict[str, asyncio.Future] = {}

# --- Global Event for Animation Control ---
server_ready_event = threading.Event()

def animate_loading(stop_event: threading.Event):
    animation_chars = ["⢿", "⣻", "⣽", "⣾", "⣷", "⣯", "⣟", "⡿"]
    idx = 0
    print("🎬 Starting Animex Cloud Relay...", end="", flush=True)
    while not stop_event.is_set():
        char = animation_chars[idx % len(animation_chars)]
        print(f" {char}", end="\r", flush=True)
        idx += 1
        time.sleep(0.08)
    print("\n📺 Animex Cloud Server is ready!")

# --- Tunnel Helper Logic ---
async def tunnel_request(method: str, url: str, headers: dict = None, json_data: any = None, timeout: int = 30) -> Dict:
    """Sends a request to the home agent via WebSocket and waits for the response."""
    global active_tunnel
    if not active_tunnel:
        # Fallback to local fetch if home agent is not connected
        async with httpx.AsyncClient() as client:
            resp = await client.request(method, url, headers=headers, json=json_data, timeout=timeout)
            return {
                "status": resp.status_code,
                "headers": dict(resp.headers),
                "body": base64.b64encode(resp.content).decode('utf-8')
            }

    req_id = str(uuid.uuid4())
    future = asyncio.get_event_loop().create_future()
    pending_requests[req_id] = future

    payload = {
        "type": "request",
        "id": req_id,
        "method": method,
        "url": url,
        "headers": headers or {},
        "json": json_data
    }

    try:
        await active_tunnel.send_json(payload)
        # Wait for response with timeout
        result = await asyncio.wait_for(future, timeout=timeout)
        return result
    except Exception as e:
        if req_id in pending_requests:
            del pending_requests[req_id]
        raise HTTPException(status_code=502, detail=f"Tunnel request failed: {str(e)}")

# --- Smart HTTP Client Replacement ---
class HybridClient:
    def __init__(self, use_tunnel=True):
        self.use_tunnel = use_tunnel
    
    def _create_mock_response(self, res_data):
        class MockResponse:
            def __init__(self, d):
                self.status_code = d.get("status", 500)
                self.headers = d.get("headers", {})
                # Decode binary body from tunnel
                try:
                    self.content = base64.b64decode(d.get("body", ""))
                except Exception:
                    self.content = b""
                self.text = self.content.decode('utf-8', errors='ignore')
            
            def json(self): 
                return json.loads(self.text)
                
            def raise_for_status(self):
                if self.status_code >= 400: 
                    raise httpx.HTTPStatusError(f"Error {self.status_code}", request=None, response=self)
        return MockResponse(res_data)

    async def get(self, url, headers=None, params=None, timeout=30, **kwargs):
        # 1. Properly encode query params into the URL for the tunnel
        if params:
            from urllib.parse import urlencode
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{urlencode(params)}"
        
        if self.use_tunnel:
            # Send the request through the WebSocket tunnel
            res = await tunnel_request("GET", url, headers=headers, timeout=timeout)
            return self._create_mock_response(res)
        else:
            # Direct fetch fallback
            async with httpx.AsyncClient(follow_redirects=True) as client:
                return await client.get(url, headers=headers, timeout=timeout, **kwargs)

    async def post(self, url, json=None, headers=None, params=None, timeout=30, **kwargs):
        # 1. Encode query params into URL (Some APIs like AnimeKai use POST with URL params)
        if params:
            from urllib.parse import urlencode
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{urlencode(params)}"
            
        # 2. Support all common JSON payload argument names used by modules
        json_payload = json or kwargs.get("json_data") or kwargs.get("json_payload") or kwargs.get("json_body")
        
        if self.use_tunnel:
            # Send the request through the WebSocket tunnel
            res = await tunnel_request("POST", url, headers=headers, json_data=json_payload, timeout=timeout)
            return self._create_mock_response(res)
        else:
            # Direct fetch fallback
            async with httpx.AsyncClient(follow_redirects=True) as client:
                return await client.post(url, json=json_payload, headers=headers, timeout=timeout, **kwargs)
            
# --- Utility Functions ---
def natural_sort_key(s):
    return [int(text) if text.isdigit() else text.lower() for text in re.split('([0-9]+)', s)]

# --- Zeroconf (Keep for local, but won't trigger on Render) ---
zeroconf = Zeroconf()
service_info = None

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

def register_service():
    global service_info
    try:
        host_ip = get_local_ip()
        host_name = socket.gethostname()
        port = 7275
        service_info = ServiceInfo(
            "_http._tcp.local.",
            f"Animex Extension API @ {host_name}._http._tcp.local.",
            addresses=[socket.inet_aton(host_ip)],
            port=port,
            properties={'app': 'animex-extension-api'},
            server=f"{host_name}.local.",
        )
        zeroconf.register_service(service_info)
    except Exception:
        pass

async def unregister_service():
    if service_info:
        zeroconf.close()
        
# --- Caching Setup ---
DATA_DIR = "data"
CACHE_DIR_JIKAN = os.path.join(DATA_DIR, "cache", "jikan")
CACHE_DIR_GENERIC = os.path.join(DATA_DIR, "cache", "generic")
os.makedirs(CACHE_DIR_JIKAN, exist_ok=True)
os.makedirs(CACHE_DIR_GENERIC, exist_ok=True)

MEMORY_CACHE = {"anime_db": None, "anime_db_timestamp": 0}

def get_cache_key(url: str) -> str:
    import hashlib
    return hashlib.md5(url.encode('utf-8')).hexdigest() + ".json"

def get_cache_path(cache_key: str) -> str:
    return os.path.join(CACHE_DIR_GENERIC, get_cache_key(cache_key))

def load_named_cache(cache_key: str, ttl: int = 86400):
    filepath = get_cache_path(cache_key)
    if not os.path.exists(filepath): return None
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if time.time() - data.get("_timestamp", 0) > ttl: return None
        return data.get("payload")
    except Exception: return None

def save_named_cache(cache_key: str, payload: dict):
    filepath = get_cache_path(cache_key)
    cache_obj = {"payload": payload, "_timestamp": time.time()}
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(cache_obj, f)
    except Exception: pass

def load_cache(url: str):
    filepath = os.path.join(CACHE_DIR_JIKAN, get_cache_key(url))
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if data.get("_is_permanent", False): return data["payload"]
            if time.time() - data["_timestamp"] > 86400: return None
            return data["payload"]
        except Exception: return None
    return None

def save_cache(url: str, payload: dict):
    filepath = os.path.join(CACHE_DIR_JIKAN, get_cache_key(url))
    is_permanent = False
    data_content = payload.get("data")
    if isinstance(data_content, dict):
        status = data_content.get("status", "")
        if status in ["Finished Airing", "Finished"]: is_permanent = True
    cache_obj = {"payload": payload, "_timestamp": time.time(), "_is_permanent": is_permanent}
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(cache_obj, f)
    except Exception: pass

ANILIST_URL = "https://graphql.anilist.co"
JIKAN_RELATIONS = "https://api.jikan.moe/v4/anime/{mal_id}/relations"

MEDIA_QUERY = """
query ($idMal: Int, $id: Int) {
  Media(idMal: $idMal, id: $id, type: ANIME) {
    id
    idMal
    title { romaji english native }
    format
    episodes
    season
    seasonYear
    startDate { year month day }
    relations {
      edges {
        relationType
        node {
          id
          idMal
          title { romaji english native }
          format
          season
          seasonYear
          startDate { year month day }
        }
      }
    }
  }
}
"""

TRAVERSE_RELATIONS = {"SEQUEL", "PREQUEL", "PARENT", "CHILD"}
SEASON_FORMATS = {"TV", "TV_SHORT"}
PART_RE = re.compile(r'\b(?:part|pt|p|section)\s*[:.]?\s*([0-9]+|[ivx]+)\b', re.I)
SEASON_RE = re.compile(r'\b(?:season|s)\s*[:.]?\s*([0-9]+|[ivx]+)\b', re.I)
COUR_RE = re.compile(r'\b(?:cour)\s*[:.]?\s*([0-9]+)\b', re.I)
FINAL_RE = re.compile(r'\b(final season|final chapters?|finale)\b', re.I)
ROMAN_MAP = {'I':1,'V':5,'X':10,'L':50,'C':100,'D':500,'M':1000}

def roman_to_int(s: str) -> Optional[int]:
    if not s: return None
    s = s.upper().strip()
    total = 0
    prev = 0
    for ch in s[::-1]:
        val = ROMAN_MAP.get(ch)
        if val is None: return None
        if val < prev: total -= val
        else: total += val
        prev = val
    return total if total > 0 else None

def extract_part_from_title(title: str) -> Optional[int]:
    if not title: return None
    m = SEASON_RE.search(title)
    if m:
        raw = m.group(1)
        try: return int(raw)
        except ValueError: return roman_to_int(raw)
    m = PART_RE.search(title)
    if m:
        raw = m.group(1)
        try: return int(raw)
        except ValueError: return roman_to_int(raw)
    m = COUR_RE.search(title)
    if m:
        try: return int(m.group(1))
        except Exception: return None
    return None

def detect_final_in_title(title: str) -> bool:
    return bool(title and FINAL_RE.search(title))

def safe_date_tuple(sd: Dict[str, Any]) -> Tuple[int,int,int]:
    if not sd: return (0,0,0)
    return (sd.get("year") or 0, sd.get("month") or 0, sd.get("day") or 0)

@dataclass
class MediaEntry:
    anilist_id: int
    mal_id: Optional[int]
    title_romaji: str
    title_english: Optional[str]
    title_native: Optional[str]
    format: Optional[str]
    start_date: Dict[str, Optional[int]]
    season: Optional[str]
    season_year: Optional[int]
    episodes: Optional[int]
    relation_type_from_parent: Optional[str] = None
    inferred_part: Optional[int] = None
    is_season: bool = False
    is_final_from_title: bool = False

    def title_display(self) -> str:
        return (self.title_english or self.title_romaji or self.title_native or "")

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["title_display"] = self.title_display()
        d["_start_tuple"] = safe_date_tuple(self.start_date)
        d["is_final_season"] = self.is_final_from_title
        return d

_ANILIST_CACHE: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}
_ANILIST_CACHE_TTL = 3600

def anilist_cache_get(key: str) -> Optional[Dict[str, Any]]:
    rec = _ANILIST_CACHE.get(key)
    if not rec: return None
    ts, val = rec
    if time.time() - ts > _ANILIST_CACHE_TTL:
        del _ANILIST_CACHE[key]
        return None
    return val

def anilist_cache_set(key: str, value: Optional[Dict[str, Any]]):
    _ANILIST_CACHE[key] = (time.time(), value)

async def fetch_media(client: HybridClient, mal_id: Optional[int]=None, aid: Optional[int]=None) -> Optional[Dict[str,Any]]:
    if mal_id: cache_key = f"mal:{mal_id}"
    elif aid: cache_key = f"aid:{aid}"
    else: return None
    cached = anilist_cache_get(cache_key)
    if cached is not None: return cached
    variables = {}
    if mal_id: variables["idMal"] = mal_id
    if aid: variables["id"] = aid
    try:
        r = await client.post(ANILIST_URL, json={"query": MEDIA_QUERY, "variables": variables}, timeout=15)
        media = r.json().get("data", {}).get("Media")
        anilist_cache_set(cache_key, media)
        return media
    except Exception:
        anilist_cache_set(cache_key, None)
        return None

async def fetch_jikan_relations(client: HybridClient, mal_id: int) -> List[Dict[str, Any]]:
    try:
        r = await client.get(JIKAN_RELATIONS.format(mal_id=mal_id), timeout=12)
        if r.status_code != 200: return []
        js = r.json()
        entries = []
        for rel in js.get("data", []):
            rel_type = rel.get("relation")
            for entry in rel.get("entry", []):
                if entry.get("type") != "anime": continue
                entries.append({"mal_id": entry.get("mal_id"), "title": entry.get("name"), "relation": rel_type})
        return entries
    except Exception: return []

async def collect_franchise(client: HybridClient, root_mal: int, max_visits: int = 100) -> List[MediaEntry]:
    seen_aid = set()
    results_by_key: Dict[str, MediaEntry] = {}
    stack = [("mal", root_mal)]
    visits = 0
    while stack and visits < max_visits:
        visits += 1
        kind, val = stack.pop()
        media = await fetch_media(client, mal_id=val if kind=="mal" else None, aid=val if kind=="aid" else None)
        if not media: continue
        aid = media.get("id")
        if aid in seen_aid: continue
        seen_aid.add(aid)
        tid = media.get("title") or {}
        romaji, english, native = tid.get("romaji", ""), tid.get("english"), tid.get("native")
        title_for_parsing = english or romaji or native or ""
        entry = MediaEntry(
            anilist_id=media.get("id"),
            mal_id=media.get("idMal"),
            title_romaji=romaji,
            title_english=english,
            title_native=native,
            format=media.get("format"),
            start_date=media.get("startDate") or {"year": None, "month": None, "day": None},
            season=media.get("season"),
            season_year=media.get("seasonYear"),
            episodes=media.get("episodes"),
            inferred_part=extract_part_from_title(title_for_parsing),
            is_season=(media.get("format") in SEASON_FORMATS),
            is_final_from_title=detect_final_in_title(title_for_parsing)
        )
        results_by_key[f"aid_{aid}"] = entry
        rels = (media.get("relations") or {}).get("edges", []) or []
        if not rels and media.get("idMal"):
            jikan_entries = await fetch_jikan_relations(client, media.get("idMal"))
            for je in jikan_entries:
                if je.get("mal_id"): stack.append(("mal", je["mal_id"]))
        else:
            for edge in rels:
                if edge.get("relationType") in TRAVERSE_RELATIONS:
                    node = edge.get("node") or {}
                    if node.get("idMal"): stack.append(("mal", node["idMal"]))
                    elif node.get("id"): stack.append(("aid", node["id"]))
    return list(results_by_key.values())

def produce_season_labeling(entries: List[MediaEntry]) -> Dict[str, Any]:
    seasons = sorted([e for e in entries if e.is_season], key=lambda x: safe_date_tuple(x.start_date))
    extras = sorted([e for e in entries if not e.is_season], key=lambda x: safe_date_tuple(x.start_date))
    groups_map: Dict[str, List[MediaEntry]] = {}
    auto_counter = 1
    for s in seasons:
        if s.is_final_from_title: key = "final"
        elif s.inferred_part: key = f"num_{s.inferred_part}"
        else:
            key = f"auto_{auto_counter}"
            auto_counter += 1
        groups_map.setdefault(key, []).append(s)
    ordered_keys = sorted(groups_map.keys(), key=lambda gk: min(safe_date_tuple(e.start_date) for e in groups_map[gk]))
    group_index_map = {k: i+1 for i, k in enumerate(ordered_keys)}
    season_groups_output = []
    for gk in ordered_keys:
        idx = group_index_map[gk]
        group_entries = sorted(groups_map[gk], key=lambda x: safe_date_tuple(x.start_date))
        parts_out = []
        for p_i, ent in enumerate(group_entries, start=1):
            short_label = f"S{idx}" if p_i == 1 else f"S{idx}{chr(ord('A') + (p_i - 1))}"
            parts_out.append({"short_label": short_label, "title": ent.title_display(), "mal_id": ent.mal_id, "anilist_id": ent.anilist_id, "start_date": ent.start_date, "format": ent.format, "is_final": ent.is_final_from_title})
        season_groups_output.append({"season_index": idx, "group_label": "Final Season" if any(e.is_final_from_title for e in group_entries) else f"Season {idx}", "parts": parts_out})
    return {
        "entries": [e.to_dict() for e in entries],
        "season_groups": season_groups_output,
        "extras": [{"title": e.title_display(), "mal_id": e.mal_id, "anilist_id": e.anilist_id, "format": e.format, "start_date": e.start_date} for e in extras]
    }

# --- Module & Extension Loading ---
MODULES_DIR = "modules"
EXTENSIONS_DIR = "extensions"
loaded_modules = {}
module_states = {}
loaded_extensions = {}

def load_modules():
    if not os.path.exists(MODULES_DIR): os.makedirs(MODULES_DIR)
    for filename in sorted([f for f in os.listdir(MODULES_DIR) if f.endswith(".module")]):
        module_name = filename.split(".")[0]
        try:
            with open(os.path.join(MODULES_DIR, filename), 'r', encoding='utf-8') as f:
                content = f.read()
                meta_str, _, code_str = content.partition("\n---\n")
                spec = importlib.util.spec_from_loader(module_name, loader=None)
                module = importlib.util.module_from_spec(spec)
                # Inject hybrid client logic if needed into the module scope
                exec(code_str, module.__dict__)
                module_info = json.loads(meta_str)
                module_info['id'] = module_name
                loaded_modules[module_name] = {"info": module_info, "instance": module}
                module_states[module_name] = True
        except Exception as e: print(f"Module load fail: {e}")

def load_extensions(app: FastAPI):
    if not os.path.exists(EXTENSIONS_DIR): return
    for ext_name in os.listdir(EXTENSIONS_DIR):
        ext_path = os.path.join(EXTENSIONS_DIR, ext_name)
        if not os.path.isdir(ext_path): continue
        package_json_path = os.path.join(ext_path, "package.json")
        if not os.path.exists(package_json_path): continue
        try:
            with open(package_json_path, 'r', encoding='utf-8') as f:
                ext_meta = json.load(f)
            main_file = ext_meta.get("main", "extension.extn")
            ext_file_path = os.path.join(ext_path, main_file)
            module_name = f"extensions.{ext_name}.{main_file.split('.')[0]}"
            spec = importlib.util.spec_from_file_location(module_name, ext_file_path)
            ext_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(ext_module)
            ext_module.EXT_PATH = os.path.abspath(ext_path)
            loaded_extensions[ext_name] = {"info": ext_meta, "instance": ext_module, "process": None, "server_url": None}
            if "port" in ext_meta and "start_command" in ext_meta:
                p = subprocess.Popen(ext_meta["start_command"], shell=True, preexec_fn=os.setsid, cwd=ext_path)
                loaded_extensions[ext_name]["process"] = p
                loaded_extensions[ext_name]["server_url"] = f"http://127.0.0.1:{ext_meta['port']}"
            if "static_folder" in ext_meta:
                app.mount(f"/ext/{ext_name}/static", StaticFiles(directory=os.path.join(ext_path, ext_meta["static_folder"])), name=f"ext_{ext_name}_static")
        except Exception as e: print(f"Ext load fail {ext_name}: {e}")

# --- Standard Guest Profile ---
STANDARD_PROFILE_ID = "guest"

# --- FastAPI App Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global hybrid_client
    hybrid_client = HybridClient(use_tunnel=True)
    load_modules()
    load_extensions(app)
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, register_service)
    server_ready_event.set()
    yield
    for ext_name, ext_data in loaded_extensions.items():
        if ext_data.get("process"):
            try: os.killpg(os.getpgid(ext_data["process"].pid), signal.SIGTERM)
            except Exception: pass
    await unregister_service()

app = FastAPI(title="Animex Cloud API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# --- Tunnel WebSocket Endpoint ---
@app.websocket("/ws/tunnel")
async def websocket_tunnel(websocket: WebSocket, token: str = Query(...)):
    global active_tunnel
    if token != TUNNEL_TOKEN:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    
    await websocket.accept()
    active_tunnel = websocket
    print("🏠 Home Agent connected to Cloud Tunnel.")
    
    try:
        while True:
            data = await websocket.receive_json()
            if data.get("type") == "response":
                req_id = data.get("id")
                if req_id in pending_requests:
                    pending_requests[req_id].set_result(data)
                    del pending_requests[req_id]
    except WebSocketDisconnect:
        print("🏠 Home Agent disconnected.")
    finally:
        active_tunnel = None

# --- Core Endpoints ---


# --- MISSING HELPERS & GRAPHQL QUERIES ---

def _get_cover_url_from_manga(manga: Dict[str, Any]) -> Optional[str]:
    """Helper to extract cover URL from a MangaDex manga object with included cover_art."""
    cover_rel = next((rel for rel in manga.get("relationships", []) if rel.get("type") == "cover_art"), None)
    if cover_rel:
        file_name = cover_rel.get("attributes", {}).get("fileName")
        if file_name:
            return f"/mangadex/cover/{manga['id']}/{file_name}"
    return None

VA_QUERY = """
query ($idMal: Int, $page: Int) {
  Media(idMal: $idMal, type: ANIME) {
    id
    title { romaji english }
    characters(page: $page, perPage: 25) {
      pageInfo { hasNextPage }
      edges {
        role
        node {
          id
          name { full native }
          image { large }
        }
        voiceActors {
          id
          name { full native }
          language
          image { large }
        }
      }
    }
  }
}
"""

ANILIST_QUERY = """
query ($malId: Int) {
  Media(idMal: $malId, type: ANIME) {
    id
    bannerImage
    coverImage { extraLarge large medium }
  }
}
"""

ANILIST_MANGA_QUERY = """
query ($malId: Int) {
  Media(idMal: $malId, type: MANGA) {
    id
    bannerImage
    coverImage { extraLarge large medium }
  }
}
"""

ANIMETHEMES_API = "https://api.animethemes.moe"


@app.get("/identify")
def identify_server(): return {"app": "Animex Extension API", "version": "2.0-Tunnel", "tunnel_active": active_tunnel is not None}

@app.get("/status")
def get_status(): return {"status": "online", "tunnel": "connected" if active_tunnel else "disconnected"}

# --- Proxy Logic (Always Tunnel) ---
@app.get("/proxy")
async def generic_proxy(url: str = Query(...), referer: Optional[str] = Query(None)):
    if "localhost" in url or "127.0.0.1" in url: raise HTTPException(status_code=400)
    headers = {"User-Agent": "Mozilla/5.0"}
    if referer: headers["Referer"] = referer
    
    # We use tunnel_request directly for proxy to ensure binary passing
    res = await tunnel_request("GET", url, headers=headers)
    content = base64.b64decode(res["body"])
    return Response(content=content, media_type=res["headers"].get("content-type"))

@app.get("/proxy-image")
async def proxy_image(url: str):
    res = await tunnel_request("GET", url)
    return Response(content=base64.b64decode(res["body"]), media_type=res["headers"].get("content-type"))

# --- MangaDex Tunnel Support ---
MANGADEX_API_URL = "https://api.mangadex.org"

@app.get("/mangadex/search")
async def search_mangadex(q: str, profile_id: Optional[str] = Query(None)):
    params = {"title": q, "limit": 24, "includes[]": ["cover_art"]}
    res = await tunnel_request("GET", f"{MANGADEX_API_URL}/manga", headers={"params": params})
    data = json.loads(base64.b64decode(res["body"]))
    for m in data.get("data", []):
        rel = next((r for r in m.get("relationships", []) if r["type"] == "cover_art"), None)
        if rel: m["cover_url"] = f"/mangadex/cover/{m['id']}/{rel['attributes']['fileName']}"
    return data

@app.get("/mangadex/cover/{manga_id}/{file_name}")
async def get_mangadex_cover(manga_id: str, file_name: str):
    url = f"https://uploads.mangadex.org/covers/{manga_id}/{file_name}.256.jpg"
    res = await tunnel_request("GET", url, headers={"Referer": "https://mangadex.org/"})
    return Response(content=base64.b64decode(res["body"]), media_type="image/jpeg")


# --- RESTORED MANGADEX EXTENDED FUNCTIONALITY ---

@app.get("/mangadex/list")
async def list_mangadex(
    order: str = Query("latestUploadedChapter", enum=["latestUploadedChapter", "followedCount", "createdAt", "updatedAt"]),
    limit: int = Query(20, ge=1, le=100),
    profile_id: Optional[str] = Query(None)
):
    # Logic: Only include NSFW if profile allows (Defaulting to safe for cloud)
    content_ratings = ["safe", "suggestive"]
    params = {f"order[{order}]": "desc", "limit": limit, "contentRating[]": content_ratings, "includes[]": ["cover_art"]}
    res = await tunnel_request("GET", f"{MANGADEX_API_URL}/manga", headers={"params": params})
    data = json.loads(base64.b64decode(res["body"]))
    for manga in data.get("data", []):
        manga["cover_url"] = _get_cover_url_from_manga(manga)
    return data

@app.get("/mangadex/manga/{manga_id}")
async def get_mangadex_manga_details(manga_id: str):
    url = f"{MANGADEX_API_URL}/manga/{manga_id}?includes[]=cover_art&includes[]=author&includes[]=artist"
    res = await tunnel_request("GET", url)
    data = json.loads(base64.b64decode(res["body"])).get("data")
    if data: data["image_url"] = _get_cover_url_from_manga(data)
    return data

@app.get("/mangadex/manga/{manga_id}/chapters")
async def get_mangadex_manga_chapters(manga_id: str, limit: int = 50, offset: int = 0):
    url = f"{MANGADEX_API_URL}/manga/{manga_id}/feed?limit={limit}&offset={offset}&translatedLanguage[]=en&order[chapter]=asc&order[volume]=asc&includes[]=scanlation_group&contentRating[]=safe&contentRating[]=suggestive&contentRating[]=erotica&contentRating[]=pornographic"
    res = await tunnel_request("GET", url, headers={"Referer": "https://mangadex.org/"})
    data = json.loads(base64.b64decode(res["body"]))
    return {"chapters": data.get("data", []), "total": data.get("total", 0)}

@app.get("/mangadex/manga/{manga_id}/all-chapters")
async def get_all_mangadex_manga_chapters(manga_id: str):
    all_ids = []
    offset = 0
    while True:
        url = f"{MANGADEX_API_URL}/manga/{manga_id}/feed?limit=100&offset={offset}&translatedLanguage[]=en&order[chapter]=asc&contentRating[]=safe&contentRating[]=suggestive&contentRating[]=erotica&contentRating[]=pornographic"
        res = await tunnel_request("GET", url, headers={"Referer": "https://mangadex.org/"})
        data = json.loads(base64.b64decode(res["body"]))
        chaps = data.get("data", [])
        if not chaps: break
        all_ids.extend([c['id'] for c in chaps])
        offset += 100
        if offset >= data.get("total", 0): break
    return {"chapter_ids": all_ids}

@app.get("/mangadex/manga/{manga_id}/chapter-nav-details/{chapter_id}")
async def get_mangadex_chapter_nav_details(manga_id: str, chapter_id: str):
    # Re-using all-chapters logic to find neighbors
    res = await get_all_mangadex_manga_chapters(manga_id)
    ids = res["chapter_ids"]
    try:
        idx = ids.index(chapter_id)
        next_id = ids[idx + 1] if idx + 1 < len(ids) else None
        return {"current_chapter": {"id": chapter_id}, "next_chapter_id": next_id, "total_chapters": len(ids)}
    except ValueError: raise HTTPException(status_code=404)

@app.get("/mangadex/chapter/{chapter_id}")
async def get_mangadex_chapter_images(chapter_id: str):
    url = f"{MANGADEX_API_URL}/at-home/server/{chapter_id}"
    res = await tunnel_request("GET", url, headers={"Referer": "https://mangadex.org/"})
    data = json.loads(base64.b64decode(res["body"]))
    base, hash_id, files = data.get("baseUrl"), data.get("chapter", {}).get("hash"), data.get("chapter", {}).get("data", [])
    server_host = urlparse(base).netloc
    return [f"/mangadex/proxy/{server_host}/data/{hash_id}/{f}" for f in sorted(files, key=natural_sort_key)]

@app.get("/mangadex/proxy/{server_host}/data/{chapter_hash}/{filename:path}")
async def proxy_mangadex_image(server_host: str, chapter_hash: str, filename: str):
    url = f"https://{server_host}/data/{chapter_hash}/{filename}"
    res = await tunnel_request("GET", url, headers={"Referer": "https://mangadex.org/"})
    return Response(content=base64.b64decode(res["body"]), media_type=res["headers"].get("content-type", "image/jpeg"))

# --- RESTORED METADATA & MAPPINGS ---

@app.get("/map/mal/{mal_id}")
async def mal_to_kitsu(mal_id: int):
    url = "https://raw.githubusercontent.com/Fribb/anime-lists/refs/heads/master/anime-offline-database-reduced.json"
    if MEMORY_CACHE["anime_db"] is None or (time.time() - MEMORY_CACHE["anime_db_timestamp"] > 86400):
        r = await hybrid_client.get(url)
        MEMORY_CACHE["anime_db"] = r.json()
        MEMORY_CACHE["anime_db_timestamp"] = time.time()
    for anime in MEMORY_CACHE["anime_db"]:
        if anime.get("mal_id") == mal_id:
            if anime.get("kitsu_id"): return {"kitsu_id": anime["kitsu_id"]}
    raise HTTPException(status_code=404)

@app.get("/map/file/animekai")
async def serve_animekai_map():
    try:
        with open("templates/map.json", "r") as f: return json.load(f)
    except: raise HTTPException(status_code=404)

@app.get("/anime/{mal_id}/ep/{ep_number}/thumbnail")
async def get_episode_thumbnail(mal_id: int, ep_number: int):
    m = await mal_to_kitsu(mal_id)
    url = f"https://kitsu.io/api/edge/anime/{m['kitsu_id']}/episodes?filter[number]={ep_number}"
    r = await hybrid_client.get(url)
    data = r.json().get("data", [])
    if not data: raise HTTPException(status_code=404)
    thumb = data[0].get("attributes", {}).get("thumbnail", {}).get("original")
    return {"mal_id": mal_id, "episode": ep_number, "thumbnail_url": thumb}

@app.get("/anime/{mal_id}/movie/thumbnail")
async def get_movie_thumbnail(mal_id: int):
    m = await mal_to_kitsu(mal_id)
    r = await hybrid_client.get(f"https://kitsu.io/api/edge/anime/{m['kitsu_id']}")
    attr = r.json().get("data", {}).get("attributes", {})
    return {"mal_id": mal_id, "thumbnail_url": attr.get("posterImage", {}).get("original"), "cover_url": attr.get("coverImage", {}).get("original")}

@app.get("/anime/{mal_id}/characters")
async def get_anime_characters(mal_id: int):
    # AniList GraphQL logic
    r = await hybrid_client.post(ANILIST_URL, json={"query": VA_QUERY, "variables": {"idMal": mal_id, "page": 1}})
    media = r.json().get("data", {}).get("Media")
    if not media: raise HTTPException(status_code=404)
    chars = []
    for edge in media["characters"]["edges"]:
        chars.append({"role": edge["role"], "character": {"name": edge["node"]["name"]["full"], "image": edge["node"]["image"]["large"]}, 
                      "voice_actors": [{"name": v["name"]["full"], "image": v["image"]["large"]} for v in edge["voiceActors"]]})
    return {"mal_id": mal_id, "characters": chars}

@app.get("/anime/{mal_id}/seasons")
async def get_anime_seasons_endpoint(mal_id: int):
    cache_key = f"seasons:{mal_id}"
    cached = load_named_cache(cache_key)
    if cached: return cached
    entries = await collect_franchise(hybrid_client, mal_id)
    out = produce_season_labeling(entries)
    save_named_cache(cache_key, out)
    return out

@app.get("/anime/{mal_id}/banner")
async def get_anime_banner(mal_id: int, cover: bool = False):
    r = await hybrid_client.post(ANILIST_URL, json={"query": ANILIST_QUERY, "variables": {"malId": mal_id}})
    media = r.json().get("data", {}).get("Media", {})
    url = (media.get("coverImage", {}).get("extraLarge") if cover else media.get("bannerImage"))
    if not url: raise HTTPException(status_code=404)
    res = await tunnel_request("GET", url)
    return Response(content=base64.b64decode(res["body"]), media_type="image/jpeg")

@app.get("/manga/{mal_id}/banner")
async def get_manga_banner(mal_id: int, cover: bool = False):
    r = await hybrid_client.post(ANILIST_URL, json={"query": ANILIST_MANGA_QUERY, "variables": {"malId": mal_id}})
    media = r.json().get("data", {}).get("Media", {})
    url = (media.get("coverImage", {}).get("extraLarge") if cover else media.get("bannerImage"))
    if not url: raise HTTPException(status_code=404)
    res = await tunnel_request("GET", url)
    return Response(content=base64.b64decode(res["body"]), media_type="image/jpeg")

# --- RESTORED MODULE INTERFACING & DOWNLOADS ---

@app.get("/chapters/{mal_id}")
async def get_manga_chapters_module(mal_id: int):
    for mid, mdata in loaded_modules.items():
        if module_states.get(mid) and "MANGA_READER" in mdata["info"].get("type", []):
            try:
                mdata["instance"].httpx = hybrid_client
                chaps = await mdata["instance"].get_chapters(mal_id)
                if chaps: return {"chapters": chaps, "source_module": mid}
            except: pass
    raise HTTPException(status_code=404)

@app.get("/retrieve/{mal_id}/{chapter_num}")
async def get_manga_images_module(mal_id: int, chapter_num: str):
    for mid, mdata in loaded_modules.items():
        if module_states.get(mid) and "MANGA_READER" in mdata["info"].get("type", []):
            try:
                mdata["instance"].httpx = hybrid_client
                imgs = await mdata["instance"].get_chapter_images(mal_id, chapter_num)
                if imgs: return imgs
            except: pass
    raise HTTPException(status_code=404)

@app.get("/download")
async def get_download_link(mal_id: int, episode: int, dub: bool = False, quality: str = "720p"):
    for mid, mdata in loaded_modules.items():
        if module_states.get(mid) and "ANIME_DOWNLOADER" in mdata["info"].get("type", []):
            try:
                mdata["instance"].httpx = hybrid_client
                link = await mdata["instance"].get_download_link(mal_id, episode, dub, quality)
                if link: return {"download_link": link, "source_module": mid}
            except: pass
    raise HTTPException(status_code=404)

@app.get("/export/series/{mal_id}")
async def export_series_package(mal_id: int, type: str = "anime"):
    r = await hybrid_client.get(f"https://api.jikan.moe/v4/{type}/{mal_id}")
    data = r.json().get("data", {})
    title = data.get("title_english") or data.get("title", "Export")
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w') as zf:
        zf.writestr("meta.json", json.dumps(data, indent=2))
        poster_url = data.get("images", {}).get("jpg", {}).get("large_image_url")
        if poster_url:
            p_res = await tunnel_request("GET", poster_url)
            zf.writestr("poster.png", base64.b64decode(p_res["body"]))
    zip_buffer.seek(0)
    return StreamingResponse(zip_buffer, media_type="application/zip", headers={"Content-Disposition": f"attachment; filename={title}.zip"})

# --- THE "DUMB" LOGIC RESTORED (PDF & THEMES) ---

@app.get("/download-manga/direct/{source}/{manga_id}/{chapter_id}")
async def download_manga_chapter_as_pdf_full(source: str, manga_id: str, chapter_id: str):
    # 1. Get Image URLs
    if source == "mangadex": urls = await get_mangadex_chapter_images(chapter_id)
    else: urls = await get_manga_images_module(int(manga_id), chapter_id)
    
    # 2. Stitching Logic (The Heavy Part)
    from PIL import Image
    import math
    processed = []
    total_h = 0
    width = 595
    for u in urls:
        full_u = u if u.startswith("http") else f"http://localhost:7275{u}"
        res = await tunnel_request("GET", full_u)
        img = Image.open(io.BytesIO(base64.b64decode(res["body"]))).convert("RGB")
        new_h = int(width * (img.height / img.width))
        processed.append(img.resize((width, new_h), Image.Resampling.LANCZOS))
        total_h += new_h

    comp = Image.new('RGB', (width, total_h))
    curr_y = 0
    for img in processed:
        comp.paste(img, (0, curr_y))
        curr_y += img.height

    pdf = FPDF()
    page_h = 842
    for i in range(math.ceil(total_h / page_h)):
        pdf.add_page()
        box = (0, i*page_h, width, (i+1)*page_h)
        page_img = comp.crop(box)
        with io.BytesIO() as buf:
            page_img.save(buf, format="PNG")
            buf.seek(0)
            pdf.image(buf, x=0, y=0, w=pdf.w)
            
    return Response(content=bytes(pdf.output(dest='S')), media_type="application/pdf")

@app.get("/api/themes/{mal_id}")
async def get_themes_full(mal_id: int):
    # Restored Artist extraction and sorting logic
    url = f"{ANIMETHEMES_API}/anime?filter[site]=MyAnimeList&filter[external_id]={mal_id}&include=animethemes.song.artists,animethemes.animethemeentries.videos"
    r = await hybrid_client.get(url)
    anime = r.json().get("anime", [{}])[0]
    themes = []
    for t in anime.get("animethemes", []):
        artists = [a.get("name") for a in t.get("song", {}).get("artists", [])]
        for e in t.get("animethemeentries", []):
            for v in e.get("videos", []):
                themes.append({"title": t.get("song", {}).get("title"), "artists": artists, "type": t["type"], "url": v["link"], "res": v.get("resolution", 0), "nc": v.get("nc", False)})
    themes.sort(key=lambda x: (x["nc"], x["res"]), reverse=True)
    return themes

@app.get("/modules/streaming", response_model=List[Dict[str, Any]])
def get_streaming_modules():
    """Returns a list of all enabled ANIME_STREAMER modules."""
    streaming_modules = []
    for name, mod in loaded_modules.items():
        if module_states.get(name, False):
            module_info = mod.get("info", {})
            module_type = module_info.get("type")
            is_streamer = (isinstance(module_type, list) and "ANIME_STREAMER" in module_type) or \
                          (isinstance(module_type, str) and module_type == "ANIME_STREAMER")
            if is_streamer:
                streaming_modules.append({
                    "id": name,
                    "name": module_info.get("name", name),
                    "version": module_info.get("version", "N/A")
                })
    return streaming_modules

@app.get("/modules/manga", response_model=List[Dict[str, Any]])
def get_manga_modules():
    """Returns a list of all enabled MANGA modules."""
    manga_modules = []
    for name, mod in loaded_modules.items():
        if module_states.get(name, False):
            module_info = mod.get("info", {})
            module_type = module_info.get("type")
            is_manga_reader = (isinstance(module_type, list) and "MANGA_READER" in module_type) or \
                              (isinstance(module_type, str) and module_type == "MANGA_READER")
            if is_manga_reader:
                manga_modules.append({
                    "id": name,
                    "name": module_info.get("name", name),
                    "version": module_info.get("version", "N/A")
                })
    return manga_modules

@app.get("/iframe-src")
async def get_iframe_source(
    mal_id: int = Query(..., description="MyAnimeList ID of the anime"),
    episode: int = Query(..., description="The episode number"),
    dub: bool = Query(False, description="Whether to fetch the dubbed version"),
    prefer_module: Optional[str] = Query(None, description="Exact module name to prefer")
):
    """
    Iterates through enabled modules to find an iframe source.
    Prioritizes 'prefer_module' if provided.
    """
    modules_to_try = []

    # 1. Build the prioritized list of modules
    if prefer_module:
        mod = loaded_modules.get(prefer_module)
        if mod and module_states.get(prefer_module, False):
            # Put the preferred module at the top of the stack
            modules_to_try.append((prefer_module, mod))
        
        # Add all other enabled modules as fallback
        for mid, mdata in sorted(loaded_modules.items()):
            if module_states.get(mid, False) and mid != prefer_module:
                modules_to_try.append((mid, mdata))
    else:
        # No preference: try all enabled modules in alphabetical order
        modules_to_try = [
            (mid, mdata) for mid, mdata in sorted(loaded_modules.items())
            if module_states.get(mid, False)
        ]

    # 2. Iterate and attempt to resolve source
    for module_id, module_data in modules_to_try:
        module_info = module_data.get("info", {})
        module_type = module_info.get("type")
        
        # Verify if the module supports streaming
        is_streamer = (isinstance(module_type, list) and "ANIME_STREAMER" in module_type) or \
                      (isinstance(module_type, str) and module_type == "ANIME_STREAMER")
        
        if not is_streamer:
            continue

        try:
            # INJECT TUNNEL CLIENT: Crucial for cloud relay
            # This forces the module's internal requests to go through your Home Agent
            module_data["instance"].httpx = hybrid_client
            
            source_func = getattr(module_data["instance"], "get_iframe_source", None)
            if not source_func:
                continue
            
            # Execute the module logic
            iframe_src = await source_func(mal_id, episode, dub)

            if iframe_src:
                print(f"Success! Got source from {module_id}: {iframe_src}")
                return {"src": iframe_src, "source_module": module_id}
                
        except Exception as e:
            print(f"Module {module_id} failed: {str(e)}")
            # Silently continue to the next module in the list
            continue
            
    raise HTTPException(
        status_code=404, 
        detail="Could not retrieve an iframe source from any enabled module."
    )



# --- Serve Frontend ---
app.mount("/data", StaticFiles(directory="data"), name="data")

# UPDATE THIS LINE: Change "../animex" to "./animex" 
if os.path.exists("./animex"):
    app.mount("/", StaticFiles(directory="./animex", html=True), name="static_site")

# --- Update the __main__ block at the very bottom ---
if __name__ == "__main__":
    # Render provides a PORT environment variable
    port = int(os.environ.get("PORT", 7275)) 
    uvicorn.run(app, host="0.0.0.0", port=port)