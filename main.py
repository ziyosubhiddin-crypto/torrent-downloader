import asyncio
import os
import shutil
import time
import json
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

# Global storage for tasks
tasks = {}
active_tasks = {}
TASKS_FILE = Path("tasks.json")

def save_tasks_to_file():
    try:
        with open(TASKS_FILE, "w", encoding="utf-8") as f:
            json.dump(tasks, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Error saving tasks to file: {e}")

def load_tasks_from_file():
    global tasks
    try:
        if TASKS_FILE.exists():
            with open(TASKS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                # Cleanup states that were active before restart
                for tid, t in saved.items():
                    if t.get("status") in ["pending", "metadata", "downloading", "processing", "uploading"]:
                        t["status"] = "failed"
                        t["error"] = "Server qayta ishga tushishi sababli to'xtatildi."
                tasks = saved
        else:
            tasks = {}
    except Exception as e:
        print(f"Error loading tasks: {e}")
        tasks = {}

def update_task_state(task_id: str, updates: dict):
    if task_id in tasks:
        tasks[task_id].update(updates)
        save_tasks_to_file()

# Load initial tasks list
load_tasks_from_file()

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
            if progress["status"] == "started":
                task_dir = progress["task_dir"]
                update_task_state(task_id, {"task_dir": str(task_dir)}, save_to_disk=True)
                continue
            elif progress["status"] == "metadata":
                update_task_state(task_id, {
                    "status": "metadata",
                    "speed": progress["speed"],
                    "downloaded": "0 B",
                    "total": "0 B",
                    "percent": 0,
                    "eta": "N/A"
                }, save_to_disk=True)
            elif progress["status"] == "downloading":
                update_task_state(task_id, {
                    "status": "downloading",
                    "percent": progress["percent"],
                    "speed": progress["speed"],
                    "downloaded": progress["downloaded"],
                    "total": progress["total"],
                    "eta": progress["eta"]
                }, save_to_disk=False) # Don't write to disk for every 1-second progress bar update
            elif progress["status"] == "finished":
                task_dir = progress["task_dir"]
                downloaded_files = progress["files"]
                update_task_state(task_id, {
                    "status": "processing",
                    "percent": 100,
                    "speed": "0 B/s",
                    "eta": "N/A"
                }, save_to_disk=True)
            elif progress["status"] == "failed":
                update_task_state(task_id, {
                    "status": "failed",
                    "error": progress["error"]
                }, save_to_disk=True)
                clean_up_task(task_dir, temp_torrent_path)
                return

        # 2. Upload phase
        if not downloaded_files:
            update_task_state(task_id, {
                "status": "failed",
                "error": "Yuklab olingan fayllar topilmadi."
            }, save_to_disk=True)
            clean_up_task(task_dir, temp_torrent_path)
            return

        total_files = len(downloaded_files)
        for index, file_path in enumerate(downloaded_files, start=1):
            file_name = file_path.name
            file_size = os.path.getsize(file_path)
            file_size_gb = file_size / (1024 * 1024 * 1024)
            
            # Update current file state
            update_task_state(task_id, {
                "status": "uploading",
                "current_file": f"({index}/{total_files}) {file_name}",
                "percent": 0,
                "speed": "0 B/s",
                "downloaded": "0 MB",
                "total": f"{file_size / (1024*1024):.1f} MB",
                "eta": "N/A"
            }, save_to_disk=True)
            
            if file_size_gb > 2.0:
                print(f"Skipping {file_name} because it exceeds the 2GB limit.")
                # We record this warning in the task but keep going for other files
                update_task_state(task_id, {
                    "status": "warning",
                    "error": f"Fayl hajmi 2GB dan katta: {file_name} (Yuklab bo'lmadi)"
                }, save_to_disk=True)
                await asyncio.sleep(3) # Wait a bit so user can read warning
                continue
                
            start_time = time.time()
            last_update = 0
            
            async def upload_progress(current, total):
                nonlocal last_update
                now = time.time()
                # Throttle state updates
                if now - last_update < 1.5:
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
                    
                update_task_state(task_id, {
                    "percent": round(percent, 1),
                    "speed": speed_str,
                    "downloaded": f"{current / (1024 * 1024):.1f} MB",
                }, save_to_disk=False) # Don't write to disk for every upload percent change
            
            try:
                await upload_file_to_telegram(file_path, upload_progress)
            except Exception as e:
                update_task_state(task_id, {
                    "status": "failed",
                    "error": f"Telegramga yuklashda xato ({file_name}): {str(e)}"
                }, save_to_disk=True)
                clean_up_task(task_dir, temp_torrent_path)
                return

        # 3. Completion
        update_task_state(task_id, {
            "status": "completed",
            "percent": 100,
            "speed": "0 B/s",
            "eta": "Tayyor"
        }, save_to_disk=True)
        clean_up_task(task_dir, temp_torrent_path)

    except asyncio.CancelledError:
        print(f"Task {task_id} was cancelled.")
        update_task_state(task_id, {
            "status": "failed",
            "error": "Vazifa bekor qilindi."
        }, save_to_disk=True)
        clean_up_task(task_dir, temp_torrent_path)
    except Exception as e:
        update_task_state(task_id, {
            "status": "failed",
            "error": f"Kutilmagan xatolik yuz berdi: {str(e)}"
        }, save_to_disk=True)
        clean_up_task(task_dir, temp_torrent_path)
    finally:
        active_tasks.pop(task_id, None)

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
        "task_dir": None,
        "temp_torrent_path": str(temp_torrent_path) if temp_torrent_path else None,
        "added_at": time.time()
    }
    save_tasks_to_file()

    # Start background execution
    task_obj = asyncio.create_task(run_download_and_upload(task_id, torrent_source, temp_torrent_path))
    active_tasks[task_id] = task_obj

    return {"status": "success", "task_id": task_id, "message": "Yuklash vazifasi qo'shildi!"}

@app.get("/api/disk-usage")
def get_disk_usage():
    """
    Returns the disk usage details of the root directory.
    """
    try:
        total, used, free = shutil.disk_usage("/")
        return {
            "total": f"{total / (1024**3):.1f} GB",
            "used": f"{used / (1024**3):.1f} GB",
            "free": f"{free / (1024**3):.1f} GB",
            "percent": round((used / total) * 100, 1)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Disk holatini aniqlashda xato: {str(e)}")

@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: str):
    """
    Cancels a running task and cleans up its files.
    """
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Vazifa topilmadi.")
        
    task_info = tasks[task_id]
    
    # Cancel running async task
    if task_id in active_tasks:
        print(f"Cancelling active task {task_id}")
        active_tasks[task_id].cancel()
        
    # Manual clean up of files just in case
    task_dir_str = task_info.get("task_dir")
    temp_torrent_str = task_info.get("temp_torrent_path")
    
    task_dir = Path(task_dir_str) if task_dir_str else None
    temp_torrent = Path(temp_torrent_str) if temp_torrent_str else None
    
    clean_up_task(task_dir, temp_torrent)
    
    # Remove from lists
    tasks.pop(task_id, None)
    active_tasks.pop(task_id, None)
    save_tasks_to_file()
    
    return {"status": "success", "message": "Vazifa bekor qilindi va fayllar o'chirildi!"}

# Ensure static folder exists
static_dir = Path("static")
static_dir.mkdir(exist_ok=True)

# Mount the static folder at the root
app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    print("FastAPI server ishga tushmoqda...")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
