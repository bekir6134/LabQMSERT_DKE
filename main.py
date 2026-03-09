from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRouter
from pydantic import BaseModel
from typing import Optional
import httpx, os, asyncio, json
from datetime import datetime, timedelta
import asyncpg

# ── API router (tüm /api rotaları burada)
api = APIRouter(prefix="/api")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
_token_cache: dict = {}
TURKAK_BASE  = "https://api.turkak.org.tr"
_pool = None

def clean_db_url(url: str) -> str:
    return url.split("?")[0] if "?" in url else url

async def get_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            clean_db_url(DATABASE_URL), ssl="require", min_size=1, max_size=5)
    return _pool

async def init_db():
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS app_state (
                    key     TEXT PRIMARY KEY,
                    value   JSONB NOT NULL,
                    updated TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        print("DB init OK")
    except Exception as e:
        print("DB init error:", e)

# ── STATE ─────────────────────────────────────
@api.get("/health")
async def health():
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"db": "ok"}
    except Exception as e:
        return {"db": "error", "detail": str(e)}

@api.get("/state")
async def get_all_state():
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value FROM app_state")
        return {r["key"]: json.loads(r["value"]) for r in rows}
    except Exception as e:
        raise HTTPException(500, str(e))

@api.post("/state-batch")
async def save_state_batch(request: Request):
    try:
        data = await request.json()
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                for key, value in data.items():
                    await conn.execute("""
                        INSERT INTO app_state (key, value, updated)
                        VALUES ($1, $2::jsonb, NOW())
                        ON CONFLICT (key) DO UPDATE
                        SET value=EXCLUDED.value, updated=NOW()
                    """, key, json.dumps(value))
        return {"ok": True, "count": len(data)}
    except Exception as e:
        raise HTTPException(500, str(e))

@api.post("/state/{key}")
async def save_state_key(key: str, request: Request):
    try:
        data = await request.json()
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO app_state (key, value, updated)
                VALUES ($1, $2::jsonb, NOW())
                ON CONFLICT (key) DO UPDATE
                SET value=EXCLUDED.value, updated=NOW()
            """, key, json.dumps(data))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))

# ── TÜRKAK ────────────────────────────────────
class TokenRequest(BaseModel):
    username: str; password: str; apiUrl: Optional[str] = TURKAK_BASE

class FirmaModel(BaseModel):
    ad: str; adres: str=""; tel: str=""; mail: str=""

class CihazModel(BaseModel):
    ad: str; seriNo: str=""; marka: str=""; model: str=""

class KalibModel(BaseModel):
    tarih: str; yapan: str=""; yer: str=""

class NumaraAlRequest(BaseModel):
    token: str; apiUrl: Optional[str]=TURKAK_BASE
    firma: FirmaModel; cihaz: CihazModel; kalibrasyon: KalibModel
    fileId: Optional[str]=None

class RevizeRequest(BaseModel):
    token: str; tbdsId: str; revizeTarih: str
    revizeNot: str=""; apiUrl: Optional[str]=TURKAK_BASE

async def turkak_get_token(username, password, api_url):
    cached = _token_cache.get(username)
    if cached and cached["expires"] > datetime.now():
        return cached["token"]
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{api_url}/SSO/signin",
                              json={"Username": username, "Password": password})
        r.raise_for_status()
        data = r.json()
        token = data.get("Token") or data.get("token")
        if not token: raise HTTPException(400, "Token alinamadi")
        _token_cache[username] = {"token": token,
            "expires": datetime.now() + timedelta(hours=11, minutes=50)}
        return token

@api.post("/turkak/token")
async def get_token(req: TokenRequest):
    try:
        token = await turkak_get_token(req.username, req.password, req.apiUrl)
        expiry = _token_cache.get(req.username,{}).get("expires",datetime.now()).strftime("%d.%m.%Y %H:%M")
        return {"token": token, "expiry": expiry}
    except httpx.HTTPStatusError as e:
        raise HTTPException(401, f"Turkak login hatasi: {e.response.status_code}")
    except Exception as e:
        raise HTTPException(500, str(e))

@api.post("/turkak/numara-al")
async def numara_al(req: NumaraAlRequest):
    try:
        api_url = req.apiUrl or TURKAK_BASE
        headers = {"Authorization": f"Bearer {req.token}"}
        file_id = req.fileId
        if not file_id:
            async with httpx.AsyncClient(timeout=15) as client:
                meta_r = await client.get(f"{api_url}/TBDS/api/v1/CalibrationService/CalibrationCustomerGetData/tr", headers=headers)
                meta = meta_r.json() if meta_r.status_code == 200 else {}
                files = meta.get("Files", [])
                if files: file_id = files[0]["ID"]
        if not file_id: raise HTTPException(400, "Turkak dosya ID bulunamadi")
        kal_tarih = req.kalibrasyon.tarih or datetime.now().strftime("%Y-%m-%d")
        payload = [{"FileID": file_id,
            "MachineOrDeviceType": f"{req.cihaz.marka} {req.cihaz.model} {req.cihaz.ad}".strip(),
            "DeviceSerialNumber": req.cihaz.seriNo,
            "PersonnelPerformingCalibration": req.kalibrasyon.yapan,
            "CalibrationLocation": req.kalibrasyon.yer,
            "CalibrationDate": f"{kal_tarih}T00:00:00",
            "FirstReleaseDateOfTheDocument": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}]
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{api_url}/TBDS/api/v1/CalibrationService/CalibrationCertificateSaveData/",
                                  json=payload, headers=headers)
            result = r.json()
        item1=result.get("Item1",[]); item2=result.get("Item2",[])
        if not item1 and item2: raise HTTPException(400, item2[0].get("ErrorDescription","Kayit hatasi"))
        if not item1: raise HTTPException(400, "Sertifika kaydedilemedi")
        cert_id = item1[0]["ID"]
        await asyncio.sleep(2)
        async with httpx.AsyncClient(timeout=15) as client:
            r2 = await client.get(f"{api_url}/TBDS/api/v1/CalibrationService/CalibrationCertificateGetCertificate/{cert_id}",
                                  headers=headers)
            cert_data = r2.json()
        return {"id": cert_id, "tbdsNo": cert_data.get("TBDSNumber",""),
                "sertNo": cert_data.get("CertificationBodyDocumentNumber",""),
                "state": cert_data.get("State","")}
    except HTTPException: raise
    except Exception as e: raise HTTPException(500, str(e))

@api.get("/turkak/sertifika-durum/{cert_id}")
async def sertifika_durum(cert_id: str, authorization: str=Header(None), x_api_url: str=Header(None)):
    token = (authorization or "").replace("Bearer ","").strip()
    api_url = x_api_url or TURKAK_BASE
    if not token: raise HTTPException(401, "Token gerekli")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{api_url}/TBDS/api/v1/CalibrationService/CalibrationCertificateGetCertificate/{cert_id}",
                headers={"Authorization": f"Bearer {token}"})
            data = r.json()
        return {"id": data.get("ID",""), "tbdsNo": data.get("TBDSNumber",""),
                "sertNo": data.get("CertificationBodyDocumentNumber",""),
                "state": data.get("State","")}
    except Exception as e: raise HTTPException(500, str(e))

@api.post("/turkak/revize")
async def revize(req: RevizeRequest):
    api_url = req.apiUrl or TURKAK_BASE
    headers = {"Authorization": f"Bearer {req.token}"}
    payload = [{"ID": req.tbdsId, "RevisionDate": req.revizeTarih, "RevisionNote": req.revizeNot}]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{api_url}/TBDS/api/v1/CalibrationService/CalibrationCertificateSaveData/",
                                  json=payload, headers=headers)
            result = r.json()
        item1=result.get("Item1",[]); item2=result.get("Item2",[])
        if item2 and not item1: raise HTTPException(400, item2[0].get("ErrorDescription","Revize hatasi"))
        return {"ok": True, "id": item1[0]["ID"] if item1 else req.tbdsId}
    except HTTPException: raise
    except Exception as e: raise HTTPException(500, str(e))

# ── APP ───────────────────────────────────────
app = FastAPI(title="LabQCert")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Önce API router'ı ekle
app.include_router(api)

# Sonra startup
@app.on_event("startup")
async def startup():
    if DATABASE_URL:
        await init_db()
    else:
        print("WARNING: DATABASE_URL yok")

# En sona static + catch_all
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def root():
    return FileResponse("static/index.html")

@app.get("/{path:path}")
def catch_all(path: str):
    return FileResponse("static/index.html")
