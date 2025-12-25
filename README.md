# M3U2X - VOD Proxy for Dispatcharr

![License](https://img.shields.io/badge/License-MIT-blue.svg)
![Docker](https://img.shields.io/badge/Docker-%E2%9C%94-2496ED.svg?logo=docker)
![Python](https://img.shields.io/badge/Python-3.11+-3776AB.svg?logo=python)

**M3U2X** is a clever "fake" Xtream Codes API server that acts as a bridge between your media collections and Dispatcharr. It allows you to easily integrate media from public directories, personal collections, or online streams into your Dispatcharr VOD library.

---

## 🎬 What is M3U2X?

M3U2X emulates a fully functional Xtream Codes API server (`player_api.php`). Think of it as a dynamic playlist manager that makes your M3U files "discoverable" within the Dispatcharr ecosystem.

### How It Works
1. **Ingest:** Accepts M3U playlists via URL or direct file upload.
2. **Parse:** Stores metadata (`#EXTINF` tags, `group-title`, logos) in an internal SQLite database.
3. **Emulate:** Dispatcharr connects to M3U2X like any other Xtream Codes source.
4. **Direct Play:** End users browse and play content directly from the original source URLs—**no video data is proxied or stored by M3U2X.**

---

## ✨ Features

* **Dual Input Methods:** Add M3U sources via URL or by uploading `.m3u`/`.m3u8` files.
* **Smart Organization:** Automatically categorizes content using the `group-title` from your M3U files.
* **Direct Play:** Streams are served as direct links—zero bandwidth is consumed by the proxy server.
* **Self-Contained:** Everything is stored within a single SQLite database for easy management and backups.
* **Modern Web UI:** Built-in administration panel for adding, syncing, and managing sources.
* **Docker First:** Easy deployment and port management via Docker Compose.
* **SSL Flexible:** Works with HTTPS sources using self-signed certificates.

---

## 🚀 Quick Start (Docker)

### 1. Clone & Configure
```bash
git clone [https://github.com/SpaceBallz2k8/m3u2x.git](https://github.com/SpaceBallz2k8/m3u2x.git)
cd m3u2x
```
Edit the `.env` file to set your desired port (default is 8000):
```env
PORT=8000
```

### 2. Build & Run
```bash
docker compose build
docker compose up -d
```

### 3. Access the Admin Panel
Open your browser and navigate to:
`http://YOUR_DOCKER_IP:YOUR_PORT`

---

## 📖 How to Use

### Step 1: Add an M3U Source
1. In the web UI, go to the **"Add M3U Source"** tab.
2. Choose **From URL** (paste link) or **From File** (upload .m3u).
3. Name your source, select the content type (VOD, Series, Live, or Mixed), and click **Add Source**.

### Step 2: Configure Dispatcharr
In your Dispatcharr settings, add a new **Xtream Codes** source:
* **Server URL:** `http://YOUR_M3U2X_IP:PORT/player_api.php`
* **Username:** `admin`
* **Password:** `admin`

### Step 3: Browse & Play
Your media will appear in Dispatcharr, organized by the categories in your M3U. When played, the client receives a direct link to the original file for seamless playback.

---

## 🐳 Docker Compose Details

The `docker-compose.yml` maps your chosen port and persists the database:

```yaml
version: '3.8'
services:
  m3u2x:
    build: .
    container_name: m3u2x
    ports:
      - "${PORT}:8000"
    volumes:
      - ./data:/app/data  # Persists the SQLite database
    environment:
      - DATABASE_URL=sqlite:///./data/m3u2x.db
    restart: unless-stopped
```

---

## 🛠️ Technical Overview

* **Backend Framework:** FastAPI (Python)
* **Database:** SQLite (stored in `./data/m3u2x.db`)
* **API:** Implements essential Xtream Codes endpoints (`get_vod_categories`, `get_vod_streams`, etc.)
* **Authentication:** Simple `admin`/`admin` credentials for API access.

---

## 🤝 Contributing & Support
Contributions, suggestions, and bug reports are welcome! Please feel free to open an issue or submit a pull request on GitHub.

Found this project useful? Consider giving it a ⭐ star on GitHub!

## 📄 License
This project is open source and available under the [MIT License](LICENSE).

---
**Happy streaming! 🍿**
