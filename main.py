"""
M3U-to-Xtream Proxy (M3U2X) - Simple Xtream Codes API facade
Takes M3U playlists and exposes them as an Xtream Codes API for Dispatcharr
All streams are direct links - no proxying!
"""

from fastapi import FastAPI, HTTPException, Depends, Query, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, Text, ForeignKey, LargeBinary
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
import os
import re
import requests
import urllib.parse
import ssl
import urllib3
import logging
import tempfile

# ===================== CONFIGURATION =====================
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./m3u2x.db")
XTREAM_USERNAME = os.getenv("XTREAM_USERNAME", "admin")
XTREAM_PASSWORD = os.getenv("XTREAM_PASSWORD", "admin")
VERIFY_SSL = os.getenv("VERIFY_SSL", "false").lower() == "true"

# Disable SSL warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Create a requests session with SSL verification disabled
session = requests.Session()
session.verify = VERIFY_SSL

if not VERIFY_SSL:
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    
    from requests.adapters import HTTPAdapter
    class UnverifiedSSLAdapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            kwargs['ssl_context'] = ssl_context
            return super().init_poolmanager(*args, **kwargs)
    
    session.mount('https://', UnverifiedSSLAdapter())

# ===================== DATABASE MODELS =====================
Base = declarative_base()

class M3USource(Base):
    __tablename__ = "sources"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    url = Column(String, nullable=True)  # Can be null for file uploads
    m3u_content = Column(Text, nullable=True)  # Store M3U content directly in DB
    file_name = Column(String, nullable=True)  # Original filename for uploaded files
    type = Column(String, default="mixed")
    enabled = Column(Boolean, default=True)
    last_sync = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    streams = relationship("Stream", back_populates="source", cascade="all, delete-orphan")

class Stream(Base):
    __tablename__ = "streams"
    id = Column(Integer, primary_key=True, index=True)
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=False)
    name = Column(String, nullable=False)
    type = Column(String, default="vod")
    category = Column(String, nullable=True)  # Group title from M3U
    stream_url = Column(Text, nullable=False)
    logo = Column(String, nullable=True)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    source = relationship("M3USource", back_populates="streams")

# ===================== DATABASE SETUP =====================
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base.metadata.create_all(bind=engine)

# ===================== FASTAPI APP =====================
app = FastAPI(title="M3U2X - Xtream Codes Facade", version="4.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===================== HELPER FUNCTIONS =====================
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def authenticate_xtream(username: str, password: str) -> bool:
    return username == XTREAM_USERNAME and password == XTREAM_PASSWORD

def get_extension_from_url(url: str) -> str:
    """Extract file extension from URL for Content-Type headers"""
    parsed = urllib.parse.urlparse(url)
    path = parsed.path
    if '.' in path:
        ext = path.split('.')[-1].lower()
        if ext in ['mp4', 'mkv', 'avi', 'mov', 'webm', 'flv', 'm4v', 'wmv', 'ts', 'm3u8']:
            return ext
    return 'mp4'

def get_content_type(extension: str) -> str:
    content_types = {
        'mp4': 'video/mp4',
        'mkv': 'video/x-matroska',
        'avi': 'video/x-msvideo',
        'mov': 'video/quicktime',
        'webm': 'video/webm',
        'flv': 'video/x-flv',
        'm3u8': 'application/vnd.apple.mpegurl',
        'ts': 'video/MP2T',
    }
    return content_types.get(extension, 'video/mp4')

class M3UParser:
    @staticmethod
    def parse(content: str) -> List[dict]:
        """Parse M3U content and return list of streams with group/category info"""
        streams = []
        lines = content.strip().split('\n')

        i = 0
        while i < len(lines):
            line = lines[i].strip()

            if line.startswith('#EXTINF:'):
                stream_info = {}
                
                # Extract metadata
                tvg_name = re.search(r'tvg-name="([^"]*)"', line)
                tvg_logo = re.search(r'tvg-logo="([^"]*)"', line)
                group_title = re.search(r'group-title="([^"]*)"', line)
                
                # Extract stream name (after last comma)
                name_match = re.search(r',(.+)$', line)
                stream_info['name'] = name_match.group(1).strip() if name_match else "Unknown"
                stream_info['logo'] = tvg_logo.group(1) if tvg_logo else None
                stream_info['category'] = group_title.group(1) if group_title else "Uncategorized"
                stream_info['tvg_name'] = tvg_name.group(1) if tvg_name else stream_info['name']

                # Get URL from next line
                i += 1
                if i < len(lines) and not lines[i].startswith('#'):
                    stream_info['url'] = lines[i].strip()
                    streams.append(stream_info)

            i += 1
        return streams

def read_uploaded_file(file: UploadFile) -> str:
    """Read content from uploaded file"""
    try:
        content = file.file.read().decode('utf-8', errors='ignore')
        file.file.seek(0)  # Reset file pointer
        return content
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to read uploaded file: {str(e)}")

def fetch_m3u_from_url(url: str) -> str:
    """Fetch M3U content from URL with SSL verification disabled"""
    try:
        response = session.get(url, timeout=30)
        response.raise_for_status()
        return response.text
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to fetch M3U from URL: {str(e)}")

def sync_source(source: M3USource, db: Session):
    """Sync M3U source with database - uses stored m3u_content"""
    try:
        if not source.m3u_content:
            raise HTTPException(status_code=400, detail="Source has no M3U content")
        
        # Parse streams from stored content
        parsed_streams = M3UParser.parse(source.m3u_content)

        # Clear existing streams from this source
        db.query(Stream).filter(Stream.source_id == source.id).delete()

        # Add parsed streams
        for stream_data in parsed_streams:
            stream = Stream(
                source_id=source.id,
                name=stream_data['name'],
                type=source.type,
                category=stream_data['category'],
                stream_url=stream_data['url'],
                logo=stream_data['logo']
            )
            db.add(stream)

        source.last_sync = datetime.utcnow()
        db.commit()
        return len(parsed_streams)

    except Exception as e:
        db.rollback()
        raise

# ===================== XTREAM CODES API =====================
@app.get("/player_api.php")
async def xtream_api(
    username: str = Query(...),
    password: str = Query(...),
    action: str = Query(None),
    category_id: str = Query(None),
    vod_id: str = Query(None),
    series_id: str = Query(None),
    db: Session = Depends(get_db)
):
    """Xtream Codes API endpoint - Dispatcharr expects this format"""
    
    if not authenticate_xtream(username, password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # User/Server info
    if not action:
        return {
            "user_info": {
                "username": username,
                "password": password,
                "status": "Active",
                "exp_date": "1999999999",
                "is_trial": "0",
                "active_cons": "0",
                "created_at": "1609459200",
                "max_connections": "1"
            },
            "server_info": {
                "url": "localhost",
                "port": "8000",
                "https_port": "8000",
                "server_protocol": "http",
                "rtmp_port": "1935",
                "timezone": "UTC"
            }
        }

    # Get VOD categories (from group titles in M3U)
    if action == "get_vod_categories":
        categories = db.query(Stream.category).filter(
            Stream.type == "vod", 
            Stream.enabled == True,
            Stream.category.isnot(None)
        ).distinct().all()
        
        return [
            {
                "category_id": str(idx + 1),
                "category_name": cat[0],
                "parent_id": 0
            }
            for idx, cat in enumerate(categories)
        ]

    # Get VOD streams
    elif action == "get_vod_streams":
        streams = db.query(Stream).filter(Stream.type == "vod", Stream.enabled == True)
        
        # Filter by category if specified
        if category_id and category_id.isdigit():
            categories = db.query(Stream.category).filter(
                Stream.type == "vod"
            ).distinct().all()
            
            if int(category_id) <= len(categories):
                category_name = categories[int(category_id) - 1][0]
                streams = streams.filter(Stream.category == category_name)
        
        return [
            {
                "num": stream.id,
                "name": stream.name,
                "stream_type": "movie",
                "stream_id": stream.id,
                "stream_icon": stream.logo or "",
                "rating": "5.0",
                "rating_5based": 5,
                "added": str(int(stream.created_at.timestamp())),
                "category_id": category_id or "1",
                "container_extension": get_extension_from_url(stream.stream_url),
                "direct_source": stream.stream_url
            }
            for stream in streams.all()
        ]

    # Get VOD info
    elif action == "get_vod_info":
        if not vod_id:
            raise HTTPException(status_code=400, detail="vod_id required")

        stream = db.query(Stream).filter(Stream.id == int(vod_id), Stream.type == "vod").first()
        if not stream:
            raise HTTPException(status_code=404, detail="VOD not found")

        return {
            "info": {
                "name": stream.name,
                "o_name": stream.name,
                "cover_big": stream.logo or "",
                "movie_image": stream.logo or "",
                "description": stream.name,
                "plot": stream.name,
                "duration": "120",
                "genre": stream.category or "Unknown",
                "country": "Unknown",
                "rating": "5.0"
            },
            "movie_data": {
                "stream_id": stream.id,
                "name": stream.name,
                "added": str(int(stream.created_at.timestamp())),
                "category_id": "1",
                "container_extension": get_extension_from_url(stream.stream_url),
                "custom_sid": "",
                "direct_source": stream.stream_url
            }
        }

    # Get series categories (for TV shows)
    elif action == "get_series_categories":
        categories = db.query(Stream.category).filter(
            Stream.type == "series", 
            Stream.enabled == True,
            Stream.category.isnot(None)
        ).distinct().all()
        
        return [
            {
                "category_id": str(idx + 1),
                "category_name": cat[0],
                "parent_id": 0
            }
            for idx, cat in enumerate(categories)
        ]

    # Get series
    elif action == "get_series":
        streams = db.query(Stream).filter(Stream.type == "series", Stream.enabled == True)
        
        if category_id and category_id.isdigit():
            categories = db.query(Stream.category).filter(
                Stream.type == "series"
            ).distinct().all()
            
            if int(category_id) <= len(categories):
                category_name = categories[int(category_id) - 1][0]
                streams = streams.filter(Stream.category == category_name)
        
        return [
            {
                "num": stream.id,
                "name": stream.name,
                "series_id": stream.id,
                "cover": stream.logo or "",
                "category_id": category_id or "1"
            }
            for stream in streams.all()
        ]

    # Get series info
    elif action == "get_series_info":
        if not series_id:
            raise HTTPException(status_code=400, detail="series_id required")

        stream = db.query(Stream).filter(Stream.id == int(series_id), Stream.type == "series").first()
        if not stream:
            raise HTTPException(status_code=404, detail="Series not found")

        return {
            "seasons": [{"season": 1}],
            "info": {
                "name": stream.name,
                "cover": stream.logo or "",
                "plot": stream.name,
                "genre": stream.category or "Unknown",
                "releaseDate": "",
                "rating": "5.0",
                "category_id": "1"
            },
            "episodes": {
                "1": [
                    {
                        "id": stream.id,
                        "title": stream.name,
                        "container_extension": get_extension_from_url(stream.stream_url),
                        "direct_source": stream.stream_url
                    }
                ]
            }
        }

    # Get live categories (if you have live TV)
    elif action == "get_live_categories":
        categories = db.query(Stream.category).filter(
            Stream.type == "live", 
            Stream.enabled == True,
            Stream.category.isnot(None)
        ).distinct().all()
        
        return [
            {
                "category_id": str(idx + 1),
                "category_name": cat[0],
                "parent_id": 0
            }
            for idx, cat in enumerate(categories)
        ]

    # Get live streams
    elif action == "get_live_streams":
        streams = db.query(Stream).filter(Stream.type == "live", Stream.enabled == True)
        
        if category_id and category_id.isdigit():
            categories = db.query(Stream.category).filter(
                Stream.type == "live"
            ).distinct().all()
            
            if int(category_id) <= len(categories):
                category_name = categories[int(category_id) - 1][0]
                streams = streams.filter(Stream.category == category_name)
        
        return [
            {
                "num": stream.id,
                "name": stream.name,
                "stream_type": "live",
                "stream_id": stream.id,
                "stream_icon": stream.logo or "",
                "category_id": category_id or "1",
                "direct_source": stream.stream_url
            }
            for stream in streams.all()
        ]

    return {"error": "Unknown action"}

# ===================== STREAM REDIRECT ENDPOINTS =====================
@app.get("/movie/{username}/{password}/{stream_id}.{extension}")
async def redirect_movie_stream(
    username: str,
    password: str,
    stream_id: int,
    extension: str,
    db: Session = Depends(get_db)
):
    """Redirect to direct movie URL - Dispatcharr/VLC will follow this"""
    if not authenticate_xtream(username, password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    stream = db.query(Stream).filter(Stream.id == stream_id, Stream.type == "vod").first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")

    # Get correct content type for the file
    actual_extension = get_extension_from_url(stream.stream_url)
    content_type = get_content_type(actual_extension)
    
    # Simple 302 redirect to the actual file
    return RedirectResponse(
        url=stream.stream_url,
        status_code=302,
        headers={
            'Content-Type': content_type,
            'Accept-Ranges': 'bytes',
            'Location': stream.stream_url
        }
    )

@app.get("/series/{username}/{password}/{stream_id}.{extension}")
async def redirect_series_stream(
    username: str,
    password: str,
    stream_id: int,
    extension: str,
    db: Session = Depends(get_db)
):
    """Redirect to direct series/episode URL"""
    if not authenticate_xtream(username, password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    stream = db.query(Stream).filter(Stream.id == stream_id, Stream.type == "series").first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")

    actual_extension = get_extension_from_url(stream.stream_url)
    content_type = get_content_type(actual_extension)
    
    return RedirectResponse(
        url=stream.stream_url,
        status_code=302,
        headers={
            'Content-Type': content_type,
            'Accept-Ranges': 'bytes',
            'Location': stream.stream_url
        }
    )

@app.get("/live/{username}/{password}/{stream_id}.{extension}")
async def redirect_live_stream(
    username: str,
    password: str,
    stream_id: int,
    extension: str,
    db: Session = Depends(get_db)
):
    """Redirect to direct live stream URL"""
    if not authenticate_xtream(username, password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    stream = db.query(Stream).filter(Stream.id == stream_id, Stream.type == "live").first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")

    actual_extension = get_extension_from_url(stream.stream_url)
    content_type = 'application/vnd.apple.mpegurl' if actual_extension == 'm3u8' else get_content_type(actual_extension)
    
    return RedirectResponse(
        url=stream.stream_url,
        status_code=302,
        headers={
            'Content-Type': content_type,
            'Location': stream.stream_url
        }
    )

# ===================== ADMIN API =====================
class SourceCreate(BaseModel):
    name: str
    url: Optional[str] = None
    type: str = "vod"

@app.post("/api/sources")
async def create_source(
    name: str = Form(...),
    source_type: str = Form("vod"),
    url: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db)
):
    """Add a new M3U source via URL or file upload - stores content in DB"""
    
    if not url and not file:
        raise HTTPException(status_code=400, detail="Either URL or file must be provided")
    
    m3u_content = None
    file_name = None
    
    # Handle file upload
    if file and file.filename:
        if not file.filename.endswith('.m3u') and not file.filename.endswith('.m3u8'):
            raise HTTPException(status_code=400, detail="File must be an M3U file (.m3u or .m3u8)")
        m3u_content = read_uploaded_file(file)
        file_name = file.filename
    
    # Handle URL
    elif url:
        m3u_content = fetch_m3u_from_url(url)
        file_name = url.split('/')[-1] if '/' in url else url
    
    # Create source in database
    db_source = M3USource(
        name=name,
        url=url,
        m3u_content=m3u_content,
        file_name=file_name,
        type=source_type
    )
    db.add(db_source)
    db.commit()
    db.refresh(db_source)
    
    # Auto-sync after creation
    try:
        count = sync_source(db_source, db)
        return {
            "message": f"Source created and synced {count} streams",
            "source": {
                "id": db_source.id,
                "name": db_source.name,
                "type": db_source.type,
                "stream_count": count,
                "has_content": bool(m3u_content)
            }
        }
    except Exception as e:
        # If sync fails, still return the source but with error
        return {
            "message": f"Source created but sync failed: {str(e)}",
            "source": {
                "id": db_source.id,
                "name": db_source.name,
                "type": db_source.type,
                "error": str(e)
            }
        }

@app.get("/api/sources")
async def list_sources(db: Session = Depends(get_db)):
    """List all M3U sources with stream counts"""
    sources = db.query(M3USource).all()
    result = []
    for source in sources:
        stream_count = db.query(Stream).filter(Stream.source_id == source.id).count()
        content_size = len(source.m3u_content) if source.m3u_content else 0
        result.append({
            "id": source.id,
            "name": source.name,
            "type": source.type,
            "url": source.url,
            "file_name": source.file_name,
            "content_size": content_size,
            "enabled": source.enabled,
            "last_sync": source.last_sync,
            "stream_count": stream_count,
            "created_at": source.created_at
        })
    return result

@app.get("/api/sources/{source_id}/content")
async def get_source_content(source_id: int, db: Session = Depends(get_db)):
    """Get the raw M3U content for a source"""
    source = db.query(M3USource).filter(M3USource.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    
    if not source.m3u_content:
        raise HTTPException(status_code=404, detail="Source has no M3U content")
    
    # Return as plain text with proper headers
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(
        content=source.m3u_content,
        headers={
            "Content-Disposition": f"attachment; filename={source.file_name or 'playlist.m3u'}",
            "Content-Type": "audio/x-mpegurl"
        }
    )

@app.post("/api/sources/{source_id}/sync")
async def sync_source_endpoint(source_id: int, db: Session = Depends(get_db)):
    """Sync/parse an M3U source"""
    source = db.query(M3USource).filter(M3USource.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    count = sync_source(source, db)
    return {"message": f"Synced {count} streams", "count": count}

@app.delete("/api/sources/{source_id}")
async def delete_source(source_id: int, db: Session = Depends(get_db)):
    """Delete an M3U source and its streams - content is automatically deleted"""
    source = db.query(M3USource).filter(M3USource.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    db.delete(source)
    db.commit()
    return {"message": "Source deleted (including all M3U content and streams)"}

@app.get("/api/streams")
async def list_streams(
    source_id: Optional[int] = None,
    type: Optional[str] = None,
    category: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """List streams with optional filtering"""
    query = db.query(Stream)
    if source_id:
        query = query.filter(Stream.source_id == source_id)
    if type:
        query = query.filter(Stream.type == type)
    if category:
        query = query.filter(Stream.category == category)
    
    streams = query.all()
    return [
        {
            "id": s.id,
            "name": s.name,
            "type": s.type,
            "category": s.category,
            "stream_url": s.stream_url,
            "logo": s.logo,
            "enabled": s.enabled,
            "created_at": s.created_at,
            "source_id": s.source_id
        }
        for s in streams
    ]

# ===================== WEB INTERFACE =====================
@app.get("/")
async def admin_interface():
    """Enhanced web interface for managing M3U sources with file upload"""
    return HTMLResponse("""
    <!DOCTYPE html>
    <html>
    <head>
        <title>M3U2X - Xtream Codes Facade</title>
        <style>
            body { font-family: Arial, sans-serif; max-width: 1000px; margin: 20px auto; padding: 20px; }
            h1 { color: #333; }
            .section { background: #f5f5f5; padding: 20px; margin: 20px 0; border-radius: 5px; }
            input, select, textarea { padding: 8px; margin: 5px; width: 100%; max-width: 400px; box-sizing: border-box; }
            button { padding: 10px 20px; background: #007bff; color: white; border: none; border-radius: 3px; cursor: pointer; }
            button:hover { background: #0056b3; }
            button.secondary { background: #6c757d; }
            button.secondary:hover { background: #545b62; }
            button.success { background: #28a745; }
            button.success:hover { background: #1e7e34; }
            table { width: 100%; border-collapse: collapse; margin: 20px 0; }
            th, td { padding: 10px; text-align: left; border-bottom: 1px solid #ddd; }
            th { background: #007bff; color: white; }
            .tab-container { display: flex; margin-bottom: 20px; }
            .tab { padding: 10px 20px; cursor: pointer; background: #e9ecef; border-radius: 5px 5px 0 0; margin-right: 5px; }
            .tab.active { background: #007bff; color: white; }
            .tab-content { display: none; }
            .tab-content.active { display: block; }
            .upload-area { border: 2px dashed #007bff; padding: 40px; text-align: center; border-radius: 5px; margin: 20px 0; cursor: pointer; }
            .upload-area:hover { background: #e9ecef; }
            .file-name { margin-top: 10px; color: #666; }
            .stats { display: flex; gap: 20px; margin: 20px 0; }
            .stat-card { background: white; padding: 15px; border-radius: 5px; flex: 1; text-align: center; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
            .stat-value { font-size: 24px; font-weight: bold; color: #007bff; }
            .stat-label { color: #666; font-size: 14px; }
            .source-info { font-size: 12px; color: #666; margin-top: 5px; }
            .content-size { font-size: 11px; color: #888; }
        </style>
    </head>
    <body>
        <h1>M3U2X - Xtream Codes Facade</h1>
        <p><em>M3U files stored in database - no external file dependencies</em></p>
        
        <div class="stats" id="stats">
            <div class="stat-card">
                <div class="stat-value" id="sourceCount">0</div>
                <div class="stat-label">M3U Sources</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" id="streamCount">0</div>
                <div class="stat-label">Total Streams</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" id="vodCount">0</div>
                <div class="stat-label">VOD/Movies</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" id="seriesCount">0</div>
                <div class="stat-label">TV Series</div>
            </div>
        </div>
        
        <div class="tab-container">
            <div class="tab active" onclick="switchTab('add')">Add M3U Source</div>
            <div class="tab" onclick="switchTab('sources')">Your Sources</div>
            <div class="tab" onclick="switchTab('config')">Configuration</div>
        </div>
        
        <div class="tab-content active" id="tab-add">
            <div class="section">
                <h2>Add M3U Source</h2>
                
                <div style="margin-bottom: 20px;">
                    <button class="secondary" onclick="setUploadMethod('url')">From URL</button>
                    <button class="secondary" onclick="setUploadMethod('file')">From File</button>
                </div>
                
                <form id="addSourceForm" onsubmit="addSource(event)">
                    <div>
                        <label>Source Name:</label>
                        <input type="text" id="sourceName" placeholder="My Movie Collection" required>
                    </div>
                    
                    <div id="urlInput" style="display: block;">
                        <label>M3U URL:</label>
                        <input type="url" id="sourceUrl" placeholder="https://example.com/playlist.m3u">
                    </div>
                    
                    <div id="fileInput" style="display: none;">
                        <label>M3U File:</label>
                        <div class="upload-area" onclick="document.getElementById('fileInputField').click()">
                            <div>📁 Click to select M3U file</div>
                            <div class="file-name" id="fileName">No file selected</div>
                            <input type="file" id="fileInputField" style="display: none;" accept=".m3u,.m3u8" onchange="updateFileName()">
                        </div>
                    </div>
                    
                    <div>
                        <label>Content Type:</label>
                        <select id="sourceType">
                            <option value="vod">VOD/Movies</option>
                            <option value="series">TV Series</option>
                            <option value="live">Live TV</option>
                            <option value="mixed">Mixed (Auto-detect)</option>
                        </select>
                    </div>
                    
                    <div style="margin-top: 20px;">
                        <button type="submit" class="success">➕ Add Source</button>
                    </div>
                </form>
            </div>
        </div>
        
        <div class="tab-content" id="tab-sources">
            <div class="section">
                <h2>Your Sources <small>(M3U content stored in database)</small></h2>
                <div id="sources">Loading...</div>
            </div>
        </div>
        
        <div class="tab-content" id="tab-config">
            <div class="section">
                <h2>Dispatcharr Configuration</h2>
                <p><strong>Xtream API URL:</strong> <code id="apiUrl"></code></p>
                <p><strong>Username:</strong> admin</p>
                <p><strong>Password:</strong> admin</p>
                <p><strong>How to use:</strong></p>
                <ol>
                    <li>Add M3U URLs or upload M3U files (content stored in database)</li>
                    <li>Click "Sync" to parse the M3U</li>
                    <li>Add this server to Dispatcharr as an Xtream Codes source</li>
                    <li>Dispatcharr will show all your streams organized by group/category</li>
                    <li>When playing, Dispatcharr/VLC gets redirected to the direct file URLs</li>
                </ol>
                <p><strong>Note:</strong> All streams are direct links - no video proxying through this server!</p>
            </div>
        </div>
        
        <script>
            let currentUploadMethod = 'url';
            
            function switchTab(tabName) {
                // Update tabs
                document.querySelectorAll('.tab').forEach(tab => tab.classList.remove('active'));
                document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('active'));
                
                event.target.classList.add('active');
                document.getElementById('tab-' + tabName).classList.add('active');
            }
            
            function setUploadMethod(method) {
                currentUploadMethod = method;
                if (method === 'url') {
                    document.getElementById('urlInput').style.display = 'block';
                    document.getElementById('fileInput').style.display = 'none';
                } else {
                    document.getElementById('urlInput').style.display = 'none';
                    document.getElementById('fileInput').style.display = 'block';
                }
            }
            
            function updateFileName() {
                const fileInput = document.getElementById('fileInputField');
                const fileName = document.getElementById('fileName');
                if (fileInput.files.length > 0) {
                    fileName.textContent = fileInput.files[0].name + ' (' + formatBytes(fileInput.files[0].size) + ')';
                } else {
                    fileName.textContent = 'No file selected';
                }
            }
            
            function formatBytes(bytes) {
                if (bytes === 0) return '0 Bytes';
                const k = 1024;
                const sizes = ['Bytes', 'KB', 'MB', 'GB'];
                const i = Math.floor(Math.log(bytes) / Math.log(k));
                return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
            }
            
            async function addSource(event) {
                event.preventDefault();
                
                const name = document.getElementById('sourceName').value;
                const type = document.getElementById('sourceType').value;
                
                const formData = new FormData();
                formData.append('name', name);
                formData.append('source_type', type);
                
                if (currentUploadMethod === 'url') {
                    const url = document.getElementById('sourceUrl').value;
                    if (!url) {
                        alert('Please enter a URL');
                        return;
                    }
                    formData.append('url', url);
                } else {
                    const fileInput = document.getElementById('fileInputField');
                    if (fileInput.files.length === 0) {
                        alert('Please select a file');
                        return;
                    }
                    formData.append('file', fileInput.files[0]);
                }
                
                try {
                    const response = await fetch('/api/sources', {
                        method: 'POST',
                        body: formData
                    });
                    
                    if (response.ok) {
                        const result = await response.json();
                        alert(result.message);
                        document.getElementById('sourceName').value = '';
                        document.getElementById('sourceUrl').value = '';
                        document.getElementById('fileInputField').value = '';
                        document.getElementById('fileName').textContent = 'No file selected';
                        loadSources();
                        loadStats();
                    } else {
                        const error = await response.json();
                        alert('Error: ' + error.detail);
                    }
                } catch (error) {
                    alert('Error: ' + error.message);
                }
            }
            
            async function loadSources() {
                const res = await fetch('/api/sources');
                const sources = await res.json();
                const html = sources.length ? `
                    <table>
                        <tr><th>Name</th><th>Type</th><th>Streams</th><th>Source</th><th>Size</th><th>Last Sync</th><th>Actions</th></tr>
                        ${sources.map(s => `
                            <tr>
                                <td>
                                    <strong>${s.name}</strong>
                                    <div class="source-info">
                                        ${s.url ? 'URL: ' + s.url.substring(0, 30) + '...' : 
                                          s.file_name ? 'File: ' + s.file_name : 'Unknown source'}
                                    </div>
                                </td>
                                <td>${s.type}</td>
                                <td>${s.stream_count}</td>
                                <td>${s.url ? '🌐 URL' : s.file_name ? '📁 File' : '❓ Unknown'}</td>
                                <td>
                                    <div class="content-size">${formatBytes(s.content_size)}</div>
                                    ${s.content_size > 0 ? 
                                        '<button class="secondary" onclick="downloadContent(' + s.id + ')">Download</button>' : 
                                        'No content'}
                                </td>
                                <td>${s.last_sync ? new Date(s.last_sync).toLocaleString() : 'Never'}</td>
                                <td>
                                    <button onclick="syncSource(${s.id})">Sync</button>
                                    <button onclick="deleteSource(${s.id})">Delete</button>
                                </td>
                            </tr>
                        `).join('')}
                    </table>
                ` : '<p>No sources added yet. Add one using the "Add M3U Source" tab.</p>';
                document.getElementById('sources').innerHTML = html;
            }
            
            async function loadStats() {
                const sourcesRes = await fetch('/api/sources');
                const sources = await sourcesRes.json();
                
                const streamsRes = await fetch('/api/streams');
                const streams = await streamsRes.json();
                
                const vodStreams = streams.filter(s => s.type === 'vod').length;
                const seriesStreams = streams.filter(s => s.type === 'series').length;
                const liveStreams = streams.filter(s => s.type === 'live').length;
                
                document.getElementById('sourceCount').textContent = sources.length;
                document.getElementById('streamCount').textContent = streams.length;
                document.getElementById('vodCount').textContent = vodStreams;
                document.getElementById('seriesCount').textContent = seriesStreams;
            }
            
            async function downloadContent(sourceId) {
                window.open(`/api/sources/${sourceId}/content`, '_blank');
            }
            
            async function syncSource(id) {
                await fetch(`/api/sources/${id}/sync`, {method: 'POST'});
                alert('Sync started!');
                loadSources();
                loadStats();
            }
            
            async function deleteSource(id) {
                if (confirm('Delete this source and all its streams? M3U content will also be deleted from database.')) {
                    await fetch(`/api/sources/${id}`, {method: 'DELETE'});
                    loadSources();
                    loadStats();
                }
            }
            
            // Set API URL
            document.getElementById('apiUrl').textContent = window.location.origin + '/player_api.php';
            
            // Load initial data
            loadSources();
            loadStats();
            setInterval(loadStats, 30000); // Refresh stats every 30 seconds
        </script>
    </body>
    </html>
    """)

@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}

# ===================== MAIN =====================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
