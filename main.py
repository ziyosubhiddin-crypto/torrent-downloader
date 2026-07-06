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
from downloader import download_torrent, download_metadata, get_torrent_files, download_single_file
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

def update_task_state(task_id: str, updates: dict, save_to_disk: bool = True):
    if task_id in tasks:
        tasks[task_id].update(updates)
        if save_to_disk:
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
    Background worker that runs sequential download and Telegram upload.
    """
    print(f"DEBUG: run_download_and_upload started for task_id={task_id}, source={torrent_source}")
    task = tasks.get(task_id)
    if not task:
        print(f"DEBUG: task {task_id} not found in tasks dict!")
        return

    print(f"DEBUG: Task details: {task}")
    task_dir = None
    
    try:
        # Create task directory
        cfg = config.get_config()
        import uuid
        task_id_dir = str(uuid.uuid4())[:8]
        task_dir = cfg["DOWNLOAD_PATH"] / task_id_dir
        task_dir.mkdir(parents=True, exist_ok=True)
        update_task_state(task_id, {"task_dir": str(task_dir)}, save_to_disk=True)

        # 1. Download metadata phase
        update_task_state(task_id, {
            "status": "metadata",
            "percent": 0,
            "speed": "0 B/s",
            "downloaded": "0 B",
            "total": "0 B",
            "eta": "N/A"
        }, save_to_disk=True)

        try:
            torrent_path = await download_metadata(torrent_source, task_dir)
        except Exception as e:
            update_task_state(task_id, {
                "status": "failed",
                "error": f"Metama'lumotlarni yuklab olishda xatolik: {str(e)}"
            }, save_to_disk=True)
            clean_up_task(task_dir, temp_torrent_path)
            return

        # 2. Get list of files inside the torrent
        try:
            all_files = await get_torrent_files(torrent_path)
        except Exception as e:
            update_task_state(task_id, {
                "status": "failed",
                "error": f"Torrent fayllarini o'qishda xatolik: {str(e)}"
            }, save_to_disk=True)
            clean_up_task(task_dir, temp_torrent_path)
            return

        # 3. Filter files (>= 100MB or fallback to single largest file)
        large_files = [f for f in all_files if f["size_bytes"] >= 100 * 1024 * 1024]
        if large_files:
            filtered_files = large_files
        else:
            # Fallback to the single largest file
            largest_file = max(all_files, key=lambda f: f["size_bytes"])
            filtered_files = [largest_file]

        # Sort alphabetically by path to upload in sequence
        filtered_files.sort(key=lambda f: f["path"])
        
        total_files = len(filtered_files)
        if total_files == 0:
            update_task_state(task_id, {
                "status": "failed",
                "error": "Yuklab olish uchun mos fayllar topilmadi."
            }, save_to_disk=True)
            clean_up_task(task_dir, temp_torrent_path)
            return

        # 4. Sequential loop (Download -> Upload -> Delete)
        for index, file_info in enumerate(filtered_files, start=1):
            file_idx = file_info["index"]
            rel_path = file_info["path"]
            file_name = Path(rel_path).name
            
            # Start downloading this file
            update_task_state(task_id, {
                "status": "downloading",
                "current_file": f"({index}/{total_files}) {file_name} (Yuklanmoqda...)",
                "percent": 0,
                "speed": "0 B/s",
                "downloaded": "0 B",
                "total": file_info["size_str"],
                "eta": "N/A"
            }, save_to_disk=True)

            download_success = False
            async for progress in download_single_file(torrent_path, file_idx, task_dir):
                if progress.get("status") == "downloading":
                    update_task_state(task_id, {
                        "percent": progress["percent"],
                        "speed": progress["speed"],
                        "downloaded": progress["downloaded"],
                        "total": progress["total"],
                        "eta": progress["eta"]
                    }, save_to_disk=False)
                elif progress.get("status") == "failed":
                    update_task_state(task_id, {
                        "status": "failed",
                        "error": f"Faylni yuklab olishda xatolik ({file_name}): {progress['error']}"
                    }, save_to_disk=True)
                    clean_up_task(task_dir, temp_torrent_path)
                    return
                elif progress.get("status") == "finished":
                    download_success = True

            if not download_success:
                update_task_state(task_id, {
                    "status": "failed",
                    "error": f"Fayl yuklab olinmadi ({file_name})"
                }, save_to_disk=True)
                clean_up_task(task_dir, temp_torrent_path)
                return

            # File path on local storage
            local_file_path = task_dir / rel_path
            if not local_file_path.exists():
                update_task_state(task_id, {
                    "status": "failed",
                    "error": f"Yuklab olingan fayl topilmadi: {rel_path}"
                }, save_to_disk=True)
                clean_up_task(task_dir, temp_torrent_path)
                return

            # Split if large
            from splitter import split_file_if_large
            try:
                split_files = await split_file_if_large(local_file_path)
            except Exception as e:
                update_task_state(task_id, {
                    "status": "failed",
                    "error": f"Faylni bo'lishda xatolik ({file_name}): {str(e)}"
                }, save_to_disk=True)
                clean_up_task(task_dir, temp_torrent_path)
                return

            # Upload split parts
            for p_index, part_path in enumerate(split_files, start=1):
                part_name = part_path.name
                part_size = part_path.stat().st_size
                part_info = f" (Part {p_index}/{len(split_files)})" if len(split_files) > 1 else ""

                update_task_state(task_id, {
                    "status": "uploading",
                    "current_file": f"({index}/{total_files}) {file_name}{part_info}",
                    "percent": 0,
                    "speed": "0 B/s",
                    "downloaded": "0 MB",
                    "total": f"{part_size / (1024*1024):.1f} MB",
                    "eta": "N/A"
                }, save_to_disk=True)
                
                start_time = time.time()
                last_update = 0
                
                async def upload_progress(current, total):
                    nonlocal last_update
                    now = time.time()
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
                    }, save_to_disk=False)

                try:
                    await upload_file_to_telegram(part_path, upload_progress)
                except Exception as e:
                    update_task_state(task_id, {
                        "status": "failed",
                        "error": f"Telegramga yuklashda xato ({part_name}): {str(e)}"
                    }, save_to_disk=True)
                    clean_up_task(task_dir, temp_torrent_path)
                    return

                # Delete split part after successful upload to save disk space
                try:
                    if part_path.exists():
                        print(f"Deleting uploaded part file: {part_path}")
                        part_path.unlink()
                except Exception as e:
                    print(f"Error deleting part file {part_path}: {e}")

            # Just in case local_file_path still exists
            try:
                if local_file_path.exists():
                    print(f"Deleting local file after upload: {local_file_path}")
                    local_file_path.unlink()
            except Exception as e:
                print(f"Error deleting main file {local_file_path}: {e}")

        # 5. Completion
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
