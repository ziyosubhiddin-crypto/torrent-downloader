import asyncio
import os
import shutil
import time
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Form, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from downloader import download_torrent
from telegram_uploader import upload_file_to_telegram, reload_telegram_client

app = FastAPI(title="Torrent Telegram Downloader Web App")

# Global in-memory storage for tasks
# task_id -> dict
tasks = {}

class ConfigUpdate(BaseModel):
    api_id: str
    api_hash: str
    bot_token: str
    channel_username: str

def clean_up_task(task_dir: Optional[Path], temp_torrent_path: Optional[Path]):
    """
    Cleans up local files after successful upload or failure.
    """
    try:
        if task_dir and task_dir.exists():
            print(f"Cleaning up task directory: {task_dir}")
            shutil.rmtree(task_dir)
        if temp_torrent_path and temp_torrent_path.exists():
            print(f"Cleaning up temp torrent file: {temp_torrent_path}")
            os.remove(temp_torrent_path)
    except Exception as e:
        print(f"Error during cleanup: {e}")

async def run_download_and_upload(task_id: str, torrent_source: str, temp_torrent_path: Optional[Path] = None):
    """
    Background worker that runs the aria2c download and Telegram upload.
    """
    task = tasks.get(task_id)
    if not task:
        return

    task_dir = None
    downloaded_files = []
    
    try:
        # 1. Download phase
        async for progress in download_torrent(torrent_source):
            if progress["status"] == "metadata":
                task.update({
                    "status": "metadata",
                    "speed": progress["speed"],
                    "downloaded": "0 B",
                    "total": "0 B",
                    "percent": 0,
                    "eta": "N/A"
                })
            elif progress["status"] == "downloading":
                task.update({
                    "status": "downloading",
                    "percent": progress["percent"],
                    "speed": progress["speed"],
                    "downloaded": progress["downloaded"],
                    "total": progress["total"],
                    "eta": progress["eta"]
                })
            elif progress["status"] == "finished":
                task_dir = progress["task_dir"]
                downloaded_files = progress["files"]
                task.update({
                    "status": "processing",
                    "percent": 100,
                    "speed": "0 B/s",
                    "eta": "N/A"
                })
            elif progress["status"] == "failed":
                task.update({
                    "status": "failed",
                    "error": progress["error"]
                })
                clean_up_task(task_dir, temp_torrent_path)
                return

        # 2. Upload phase
        if not downloaded_files:
            task.update({
                "status": "failed",
                "error": "Yuklab olingan fayllar topilmadi."
            })
            clean_up_task(task_dir, temp_torrent_path)
            return

        total_files = len(downloaded_files)
        for index, file_path in enumerate(downloaded_files, start=1):
            file_name = file_path.name
            file_size = os.path.getsize(file_path)
            file_size_gb = file_size / (1024 * 1024 * 1024)
            
            # Update current file state
            task.update({
                "status": "uploading",
                "current_file": f"({index}/{total_files}) {file_name}",
                "percent": 0,
                "speed": "0 B/s",
                "downloaded": "0 MB",
                "total": f"{file_size / (1024*1024):.1f} MB",
                "eta": "N/A"
            })
            
            if file_size_gb > 2.0:
                print(f"Skipping {file_name} because it exceeds the 2GB limit.")
                # We record this warning in the task but keep going for other files
                task.update({
                    "status": "warning",
                    "error": f"Fayl hajmi 2GB dan katta: {file_name} (Yuklab bo'lmadi)"
                })
                await asyncio.sleep(3) # Wait a bit so user can read warning
                continue
                
            start_time = time.time()
            last_update = 0
            
            async def upload_progress(current, total):
                nonlocal last_update
                now = time.time()
                # Throttle state updates
                if now - last_update < 1:
                    return
                last_update = now
                
                percent = (current * 100) / total
                elapsed = now - start_time
                speed = current / elapsed if elapsed > 0 else 0
                
                if speed < 1024:
                    speed_str = f"{speed:.1f} B/s"
                elif speed < 1024 * 1024:
                    speed_str = f"{speed / 1024:.1f} KB/s"
                else:
                    speed_str = f"{speed / (1024 * 1024):.1f} MB/s"
                    
                task.update({
                    "percent": round(percent, 1),
                    "speed": speed_str,
                    "downloaded": f"{current / (1024 * 1024):.1f} MB",
                })
            
            try:
                await upload_file_to_telegram(file_path, upload_progress)
            except Exception as e:
                task.update({
                    "status": "failed",
                    "error": f"Telegramga yuklashda xato ({file_name}): {str(e)}"
                })
                clean_up_task(task_dir, temp_torrent_path)
                return

        # 3. Completion
        task.update({
            "status": "completed",
            "percent": 100,
            "speed": "0 B/s",
            "eta": "Tayyor"
        })
        clean_up_task(task_dir, temp_torrent_path)

    except Exception as e:
        task.update({
            "status": "failed",
            "error": f"Kutilmagan xatolik yuz berdi: {str(e)}"
        })
        clean_up_task(task_dir, temp_torrent_path)

@app.get("/api/config")
def get_web_config():
    """
    Returns current configuration settings (excluding full bot token for safety).
    """
    cfg = config.get_config()
    return {
        "api_id": cfg["API_ID"] or "",
        "api_hash": cfg["API_HASH"] or "",
        "bot_token": f"{cfg['BOT_TOKEN'][:10]}...{cfg['BOT_TOKEN'][-5:]}" if cfg["BOT_TOKEN"] else "",
        "channel_username": cfg["CHANNEL_USERNAME"],
        "is_configured": cfg["IS_CONFIGURED"]
    }

@app.post("/api/config")
async def update_web_config(cfg_update: ConfigUpdate):
    """
    Saves configuration variables to .env and reloads Telegram Client.
    """
    try:
        config.save_config(
            api_id=cfg_update.api_id,
            api_hash=cfg_update.api_hash,
            bot_token=cfg_update.bot_token,
            channel_username=cfg_update.channel_username
        )
        
        # Reload Pyrogram
        await reload_telegram_client()
        
        return {"status": "success", "message": "Sozlamalar saqlandi!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sozlamalarni saqlashda xato: {str(e)}")

@app.get("/api/tasks")
def list_tasks():
    """
    Returns all tasks ordered by creation time (newest first).
    """
    sorted_tasks = sorted(tasks.values(), key=lambda x: x["added_at"], reverse=True)
    return sorted_tasks

@app.post("/api/download")
async def start_download(
    magnet: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None)
):
    """
    Starts download process using magnet link or uploaded .torrent file.
    """
    cfg = config.get_config()
    if not cfg["IS_CONFIGURED"]:
        raise HTTPException(
            status_code=400, 
            detail="Tizim sozlanmagan! Avval sozlamalarni kiriting."
        )

    torrent_source = None
    temp_torrent_path = None
    source_name = "Noma'lum"

    # Handle magnet link
    if magnet and magnet.strip().startswith("magnet:"):
        torrent_source = magnet.strip()
        source_name = "Magnet Link"
        # Try to parse display name from magnet if available (dn parameter)
        import urllib.parse
        parsed = urllib.parse.parse_qs(urllib.parse.urlparse(torrent_source).query)
        if "dn" in parsed:
            source_name = parsed["dn"][0]

    # Handle uploaded file
    elif file and file.filename.endswith(".torrent"):
        # Create temp folder for torrent file uploads
        temp_dir = cfg["DOWNLOAD_PATH"] / "temp_torrents"
        temp_dir.mkdir(parents=True, exist_ok=True)
        
        temp_torrent_path = temp_dir / f"{time.time()}_{file.filename}"
        with open(temp_torrent_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
        torrent_source = str(temp_torrent_path)
        source_name = file.filename

    else:
        raise HTTPException(
            status_code=400, 
            detail="Iltimos, to'g'ri magnet havola yoki .torrent faylini yuboring."
        )

    # Register task
    task_id = f"task_{int(time.time())}"
    tasks[task_id] = {
        "id": task_id,
        "name": source_name,
        "status": "pending",
        "percent": 0,
        "speed": "0 B/s",
        "downloaded": "0 B",
        "total": "0 B",
        "eta": "N/A",
        "current_file": "",
        "error": "",
        "added_at": time.time()
    }

    # Start background execution
    asyncio.create_task(run_download_and_upload(task_id, torrent_source, temp_torrent_path))

    return {"status": "success", "task_id": task_id, "message": "Yuklash vazifasi qo'shildi!"}

# Ensure static folder exists
static_dir = Path("static")
static_dir.mkdir(exist_ok=True)

# Mount the static folder at the root
app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    print("FastAPI server ishga tushmoqda...")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
