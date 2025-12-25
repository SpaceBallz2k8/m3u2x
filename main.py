"""
M3U-to-Xtream Proxy (M3U2X) - Main Application
FastAPI-based proxy service for M3U playlists with Xtream Codes API
"""

from fastapi import FastAPI, HTTPException, Depends, Query, Request, Form
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, Text, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
from pydantic import BaseModel, HttpUrl
from typing import Optional, List
from datetime import datetime
import os
import re
import requests
from contextlib import contextmanager
import logging

# Configuration
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./m3u2x.db")
XTREAM_USERNAME = os.getenv("XTREAM_USERNAME", "admin")
XTREAM_PASSWORD = os.getenv("XTREAM_PASSWORD", "admin")
API_KEY = os.getenv("API_KEY", "")

# Database setup
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Database Models
class M3USource(Base):
    __tablename__ = "sources"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    url = Column(String, nullable=False)
    type = Column(String, default="mixed")  # movie, tv, live, mixed
    enabled = Column(Boolean, default=True)
    last_sync = Column(DateTime, nullable=True)
    refresh_interval = Column(Integer, default=3600)
    created_at = Column(DateTime, default=datetime.utcnow)

    streams = relationship("Stream", back_populates="source", cascade="all, delete-orphan")

class Category(Base):
    __tablename__ = "categories"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    type = Column(String, default="vod")  # vod, series, live
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=True)
    is_user_created = Column(Boolean, default=False)
    parent_id = Column(Integer, ForeignKey("categories.id"), nullable=True)
    sort_order = Column(Integer, default=0)

    streams = relationship("Stream", back_populates="category")

class Stream(Base):
    __tablename__ = "streams"
    id = Column(Integer, primary_key=True, index=True)
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=False)
    original_id = Column(String, nullable=True)
    name = Column(String, nullable=False)
    type = Column(String, default="vod")  # vod, series, live
    original_category = Column(String, nullable=True)
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=True)
    stream_url = Column(Text, nullable=False)
    logo = Column(String, nullable=True)
    stream_metadata = Column(Text, nullable=True)  # Renamed from 'metadata' to avoid SQLAlchemy conflict
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    source = relationship("M3USource", back_populates="streams")
    category = relationship("Category", back_populates="streams")

class Settings(Base):
    __tablename__ = "settings"
    id = Column(Integer, primary_key=True)
    key = Column(String, unique=True, nullable=False)
    value = Column(Text, nullable=False)

# Create tables
Base.metadata.create_all(bind=engine)

# FastAPI app
app = FastAPI(title="M3U2X Proxy", version="1.0.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Pydantic models
class SourceCreate(BaseModel):
    name: str
    url: str
    type: str = "mixed"
    refresh_interval: int = 3600

class SourceUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    type: Optional[str] = None
    enabled: Optional[bool] = None
    refresh_interval: Optional[int] = None

class CategoryCreate(BaseModel):
    name: str
    type: str = "vod"
    parent_id: Optional[int] = None

class StreamUpdate(BaseModel):
    name: Optional[str] = None
    category_id: Optional[int] = None
    enabled: Optional[bool] = None
    logo: Optional[str] = None

# M3U Parser
class M3UParser:
    @staticmethod
    def parse(content: str) -> List[dict]:
        """Parse M3U content and return list of streams"""
        streams = []
        lines = content.strip().split('\n')

        i = 0
        while i < len(lines):
            line = lines[i].strip()

            if line.startswith('#EXTINF:'):
                # Parse EXTINF line
                stream_info = {}

                # Extract attributes
                tvg_name = re.search(r'tvg-name="([^"]*)"', line)
                tvg_logo = re.search(r'tvg-logo="([^"]*)"', line)
                group_title = re.search(r'group-title="([^"]*)"', line)

                # Extract name (after last comma)
                name_match = re.search(r',(.+)$', line)

                stream_info['name'] = name_match.group(1).strip() if name_match else "Unknown"
                stream_info['logo'] = tvg_logo.group(1) if tvg_logo else None
                stream_info['category'] = group_title.group(1) if group_title else "Uncategorized"

                # Get URL from next line
                i += 1
                if i < len(lines) and not lines[i].startswith('#'):
                    stream_info['url'] = lines[i].strip()
                    streams.append(stream_info)

            i += 1

        return streams

# Helper functions
def authenticate_xtream(username: str, password: str) -> bool:
    """Authenticate Xtream API request"""
    return username == XTREAM_USERNAME and password == XTREAM_PASSWORD

def fetch_m3u(url: str) -> str:
    """Fetch M3U content from URL"""
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.text
    except Exception as e:
        logger.error(f"Error fetching M3U from {url}: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to fetch M3U: {str(e)}")

def sync_source(source: M3USource, db: Session):
    """Sync M3U source with database"""
    try:
        logger.info(f"Syncing source: {source.name}")

        # Fetch M3U content
        content = fetch_m3u(source.url)

        # Parse streams
        parsed_streams = M3UParser.parse(content)

        # Clear existing streams
        db.query(Stream).filter(Stream.source_id == source.id).delete()

        # Add parsed streams
        for stream_data in parsed_streams:
            # Get or create category
            category = db.query(Category).filter(
                Category.name == stream_data['category'],
                Category.type == source.type
            ).first()

            if not category:
                category = Category(
                    name=stream_data['category'],
                    type=source.type,
                    source_id=source.id
                )
                db.add(category)
                db.flush()

            # Create stream
            stream = Stream(
                source_id=source.id,
                name=stream_data['name'],
                type=source.type,
                original_category=stream_data['category'],
                category_id=category.id,
                stream_url=stream_data['url'],
                logo=stream_data['logo']
            )
            db.add(stream)

        # Update last sync time
        source.last_sync = datetime.utcnow()
        db.commit()

        logger.info(f"Synced {len(parsed_streams)} streams from {source.name}")
        return len(parsed_streams)

    except Exception as e:
        db.rollback()
        logger.error(f"Error syncing source {source.name}: {e}")
        raise

# API Routes - Xtream Codes API
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
    """Xtream Codes API endpoint"""

    if not authenticate_xtream(username, password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not action:
        # Return user info
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

    if action == "get_vod_categories":
        categories = db.query(Category).filter(Category.type == "vod").all()
        return [
            {
                "category_id": str(cat.id),
                "category_name": cat.name,
                "parent_id": cat.parent_id or 0
            }
            for cat in categories
        ]

    elif action == "get_vod_streams":
        streams = db.query(Stream).filter(Stream.type == "vod", Stream.enabled == True)

        if category_id:
            streams = streams.filter(Stream.category_id == int(category_id))

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
                "category_id": str(stream.category_id),
                "container_extension": "mp4",
                "direct_source": stream.stream_url
            }
            for stream in streams.all()
        ]

    elif action == "get_vod_info":
        if not vod_id:
            raise HTTPException(status_code=400, detail="vod_id required")

        stream = db.query(Stream).filter(Stream.id == int(vod_id)).first()
        if not stream:
            raise HTTPException(status_code=404, detail="VOD not found")

        return {
            "info": {
                "kinopoisk_url": "",
                "tmdb_id": "",
                "name": stream.name,
                "o_name": stream.name,
                "cover_big": stream.logo or "",
                "movie_image": stream.logo or "",
                "releasedate": "",
                "youtube_trailer": "",
                "director": "",
                "actors": "",
                "cast": "",
                "description": "",
                "plot": "",
                "age": "",
                "country": "",
                "genre": "",
                "duration_secs": 0,
                "duration": "",
                "video": {},
                "audio": {},
                "bitrate": 0
            },
            "movie_data": {
                "stream_id": stream.id,
                "name": stream.name,
                "added": str(int(stream.created_at.timestamp())),
                "category_id": str(stream.category_id),
                "container_extension": "mp4",
                "custom_sid": "",
                "direct_source": stream.stream_url
            }
        }

    elif action == "get_live_categories":
        categories = db.query(Category).filter(Category.type == "live").all()
        return [
            {
                "category_id": str(cat.id),
                "category_name": cat.name,
                "parent_id": cat.parent_id or 0
            }
            for cat in categories
        ]

    elif action == "get_live_streams":
        streams = db.query(Stream).filter(Stream.type == "live", Stream.enabled == True)

        if category_id:
            streams = streams.filter(Stream.category_id == int(category_id))

        return [
            {
                "num": stream.id,
                "name": stream.name,
                "stream_type": "live",
                "stream_id": stream.id,
                "stream_icon": stream.logo or "",
                "category_id": str(stream.category_id),
                "direct_source": stream.stream_url
            }
            for stream in streams.all()
        ]

    elif action == "get_series_categories":
        categories = db.query(Category).filter(Category.type == "series").all()
        return [
            {
                "category_id": str(cat.id),
                "category_name": cat.name,
                "parent_id": cat.parent_id or 0
            }
            for cat in categories
        ]

    elif action == "get_series":
        streams = db.query(Stream).filter(Stream.type == "series", Stream.enabled == True)

        if category_id:
            streams = streams.filter(Stream.category_id == int(category_id))

        return [
            {
                "num": stream.id,
                "name": stream.name,
                "series_id": stream.id,
                "cover": stream.logo or "",
                "category_id": str(stream.category_id)
            }
            for stream in streams.all()
        ]

    elif action == "get_series_info":
        if not series_id:
            raise HTTPException(status_code=400, detail="series_id required")

        stream = db.query(Stream).filter(Stream.id == int(series_id)).first()
        if not stream:
            raise HTTPException(status_code=404, detail="Series not found")

        return {
            "seasons": [],
            "info": {
                "name": stream.name,
                "cover": stream.logo or "",
                "plot": "",
                "cast": "",
                "director": "",
                "genre": "",
                "releaseDate": "",
                "rating": "5.0",
                "category_id": str(stream.category_id)
            },
            "episodes": {}
        }

    return {"error": "Unknown action"}

# Stream proxy endpoints
@app.get("/movie/{username}/{password}/{stream_id}.{extension}")
async def get_movie_stream(
    username: str,
    password: str,
    stream_id: int,
    extension: str,
    db: Session = Depends(get_db)
):
    """Proxy movie/VOD stream"""
    if not authenticate_xtream(username, password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    stream = db.query(Stream).filter(Stream.id == stream_id, Stream.type == "vod").first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")

    # Redirect to the actual stream URL
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=stream.stream_url, status_code=302)

@app.get("/live/{username}/{password}/{stream_id}.{extension}")
async def get_live_stream(
    username: str,
    password: str,
    stream_id: int,
    extension: str,
    db: Session = Depends(get_db)
):
    """Proxy live stream"""
    if not authenticate_xtream(username, password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    stream = db.query(Stream).filter(Stream.id == stream_id, Stream.type == "live").first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")

    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=stream.stream_url, status_code=302)

@app.get("/series/{username}/{password}/{stream_id}.{extension}")
async def get_series_stream(
    username: str,
    password: str,
    stream_id: int,
    extension: str,
    db: Session = Depends(get_db)
):
    """Proxy series/episode stream"""
    if not authenticate_xtream(username, password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    stream = db.query(Stream).filter(Stream.id == stream_id, Stream.type == "series").first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")

    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=stream.stream_url, status_code=302)

# Admin API Routes
@app.post("/api/v1/sources")
async def create_source(source: SourceCreate, db: Session = Depends(get_db)):
    """Create new M3U source"""
    db_source = M3USource(**source.dict())
    db.add(db_source)
    db.commit()
    db.refresh(db_source)
    return db_source

@app.get("/api/v1/sources")
async def list_sources(db: Session = Depends(get_db)):
    """List all M3U sources"""
    sources = db.query(M3USource).all()
    return sources

@app.get("/api/v1/sources/{source_id}")
async def get_source(source_id: int, db: Session = Depends(get_db)):
    """Get source details"""
    source = db.query(M3USource).filter(M3USource.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    return source

@app.put("/api/v1/sources/{source_id}")
async def update_source(source_id: int, source_update: SourceUpdate, db: Session = Depends(get_db)):
    """Update M3U source"""
    source = db.query(M3USource).filter(M3USource.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    for key, value in source_update.dict(exclude_unset=True).items():
        setattr(source, key, value)

    db.commit()
    db.refresh(source)
    return source

@app.delete("/api/v1/sources/{source_id}")
async def delete_source(source_id: int, db: Session = Depends(get_db)):
    """Delete M3U source"""
    source = db.query(M3USource).filter(M3USource.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    db.delete(source)
    db.commit()
    return {"message": "Source deleted"}

@app.post("/api/v1/sources/{source_id}/sync")
async def sync_source_endpoint(source_id: int, db: Session = Depends(get_db)):
    """Manually sync M3U source"""
    source = db.query(M3USource).filter(M3USource.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    count = sync_source(source, db)
    return {"message": f"Synced {count} streams", "count": count}

@app.get("/api/v1/streams")
async def list_streams(
    source_id: Optional[int] = None,
    category_id: Optional[int] = None,
    type: Optional[str] = None,
    enabled: Optional[bool] = None,
    search: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    """List streams with filtering"""
    query = db.query(Stream)

    if source_id:
        query = query.filter(Stream.source_id == source_id)
    if category_id:
        query = query.filter(Stream.category_id == category_id)
    if type:
        query = query.filter(Stream.type == type)
    if enabled is not None:
        query = query.filter(Stream.enabled == enabled)
    if search:
        query = query.filter(Stream.name.contains(search))

    total = query.count()
    streams = query.offset(skip).limit(limit).all()

    return {"total": total, "streams": streams}

@app.put("/api/v1/streams/{stream_id}")
async def update_stream(stream_id: int, stream_update: StreamUpdate, db: Session = Depends(get_db)):
    """Update stream"""
    stream = db.query(Stream).filter(Stream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")

    for key, value in stream_update.dict(exclude_unset=True).items():
        setattr(stream, key, value)

    db.commit()
    db.refresh(stream)
    return stream

@app.delete("/api/v1/streams/{stream_id}")
async def delete_stream(stream_id: int, db: Session = Depends(get_db)):
    """Delete stream"""
    stream = db.query(Stream).filter(Stream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")

    db.delete(stream)
    db.commit()
    return {"message": "Stream deleted"}

@app.get("/api/v1/categories")
async def list_categories(type: Optional[str] = None, db: Session = Depends(get_db)):
    """List categories"""
    query = db.query(Category)
    if type:
        query = query.filter(Category.type == type)
    return query.all()

@app.post("/api/v1/categories")
async def create_category(category: CategoryCreate, db: Session = Depends(get_db)):
    """Create category"""
    db_category = Category(**category.dict())
    db.add(db_category)
    db.commit()
    db.refresh(db_category)
    return db_category

@app.get("/api/v1/stats")
async def get_stats(db: Session = Depends(get_db)):
    """Get system statistics"""
    return {
        "sources": db.query(M3USource).count(),
        "categories": db.query(Category).count(),
        "streams": db.query(Stream).count(),
        "enabled_streams": db.query(Stream).filter(Stream.enabled == True).count(),
        "vod_streams": db.query(Stream).filter(Stream.type == "vod").count(),
        "live_streams": db.query(Stream).filter(Stream.type == "live").count(),
        "series_streams": db.query(Stream).filter(Stream.type == "series").count()
    }

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}

@app.get("/")
async def root():
    """Root endpoint with basic HTML interface"""
    return HTMLResponse(content="""
    <!DOCTYPE html>
    <html>
    <head>
        <title>M3U2X Proxy</title>
        <style>
            body { font-family: Arial, sans-serif; max-width: 1200px; margin: 50px auto; padding: 20px; }
            h1 { color: #333; }
            .section { margin: 20px 0; padding: 20px; background: #f5f5f5; border-radius: 5px; }
            .button { padding: 10px 20px; background: #007bff; color: white; border: none; border-radius: 3px; cursor: pointer; }
            .button:hover { background: #0056b3; }
            input, select { padding: 8px; margin: 5px; width: 300px; }
            table { width: 100%; border-collapse: collapse; margin: 20px 0; }
            th, td { padding: 10px; text-align: left; border-bottom: 1px solid #ddd; }
            th { background: #007bff; color: white; }
        </style>
    </head>
    <body>
        <h1>M3U2X Proxy - Management Interface</h1>

        <div class="section">
            <h2>System Status</h2>
            <p id="stats">Loading...</p>
        </div>

        <div class="section">
            <h2>Add M3U Source</h2>
            <input type="text" id="sourceName" placeholder="Source Name">
            <input type="text" id="sourceUrl" placeholder="M3U URL">
            <select id="sourceType">
                <option value="mixed">Mixed</option>
                <option value="vod">VOD/Movies</option>
                <option value="series">TV Series</option>
                <option value="live">Live TV</option>
            </select>
            <button class="button" onclick="addSource()">Add Source</button>
        </div>

        <div class="section">
            <h2>Sources</h2>
            <div id="sources">Loading...</div>
        </div>

        <div class="section">
            <h2>Xtream API Info</h2>
            <p><strong>Server URL:</strong> <code id="serverUrl">http://localhost:8000</code></p>
            <p><strong>Username:</strong> admin</p>
            <p><strong>Password:</strong> admin</p>
            <p><strong>API Endpoint:</strong> <code>/player_api.php</code></p>
        </div>

        <script>
            async function loadStats() {
                const res = await fetch('/api/v1/stats');
                const stats = await res.json();
                document.getElementById('stats').innerHTML = `
                    <strong>Sources:</strong> ${stats.sources} |
                    <strong>Streams:</strong> ${stats.streams} (${stats.enabled_streams} enabled) |
                    <strong>VOD:</strong> ${stats.vod_streams} |
                    <strong>Live:</strong> ${stats.live_streams} |
                    <strong>Series:</strong> ${stats.series_streams}
                `;
            }

            async function loadSources() {
                const res = await fetch('/api/v1/sources');
                const sources = await res.json();
                const html = sources.length ? `
                    <table>
                        <tr><th>Name</th><th>Type</th><th>Status</th><th>Last Sync</th><th>Actions</th></tr>
                        ${sources.map(s => `
                            <tr>
                                <td>${s.name}</td>
                                <td>${s.type}</td>
                                <td>${s.enabled ? 'Enabled' : 'Disabled'}</td>
                                <td>${s.last_sync || 'Never'}</td>
                                <td>
                                    <button class="button" onclick="syncSource(${s.id})">Sync</button>
                                    <button class="button" onclick="deleteSource(${s.id})">Delete</button>
                                </td>
                            </tr>
                        `).join('')}
                    </table>
                ` : '<p>No sources added yet.</p>';
                document.getElementById('sources').innerHTML = html;
            }

            async function addSource() {
                const name = document.getElementById('sourceName').value;
                const url = document.getElementById('sourceUrl').value;
                const type = document.getElementById('sourceType').value;

                if (!name || !url) {
                    alert('Please enter name and URL');
                    return;
                }

                await fetch('/api/v1/sources', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name, url, type})
                });

                document.getElementById('sourceName').value = '';
                document.getElementById('sourceUrl').value = '';
                loadSources();
                loadStats();
            }

            async function syncSource(id) {
                await fetch(`/api/v1/sources/${id}/sync`, {method: 'POST'});
                alert('Sync started!');
                loadSources();
                loadStats();
            }

            async function deleteSource(id) {
                if (confirm('Delete this source?')) {
                    await fetch(`/api/v1/sources/${id}`, {method: 'DELETE'});
                    loadSources();
                    loadStats();
                }
            }

            // Set server URL
            document.getElementById('serverUrl').textContent = window.location.origin;

            // Load initial data
            loadStats();
            loadSources();
            setInterval(loadStats, 5000);
        </script>
    </body>
    </html>
    """)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
