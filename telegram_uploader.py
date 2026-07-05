import asyncio
from pathlib import Path
from pyrogram import Client
from pyrogram.errors import FloodWait
import config

# Global client cache
_client = None

def get_telegram_client():
    """
    Returns the singleton Pyrogram Client instance.
    Returns None if not fully configured.
    """
    global _client
    cfg = config.get_config()
    if not cfg["IS_CONFIGURED"]:
        return None
        
    if _client is None:
        print("Initializing Pyrogram client...")
        _client = Client(
            "torrent_web_app",
            api_id=cfg["API_ID"],
            api_hash=cfg["API_HASH"],
            bot_token=cfg["BOT_TOKEN"]
        )
    return _client

async def reload_telegram_client():
    """
    Stops the existing Pyrogram client (if connected) and resets it,
    forcing it to re-initialize with new credentials next time.
    """
    global _client
    if _client is not None:
        print("Reloading Pyrogram client with new config...")
        try:
            if _client.is_connected:
                await _client.stop()
        except Exception as e:
            print(f"Error stopping Telegram client: {e}")
        _client = None
    
    # Try to initialize again with new credentials
    get_telegram_client()

async def upload_file_to_telegram(file_path: Path, progress_callback=None):
    """
    Uploads a file (as video or general document) to the configured Telegram channel.
    Calls progress_callback(current_bytes, total_bytes) periodically if provided.
    Includes auto-retry loop for Pyrogram FloodWait rate limits.
    """
    # Pre-upload size check (Telegram bot limit is 2GB)
    MAX_TELEGRAM_SIZE = int(2 * 1024 * 1024 * 1024)  # 2 GB
    file_size = file_path.stat().st_size
    if file_size >= MAX_TELEGRAM_SIZE:
        raise ValueError(
            f"Fayl hajmi {file_size / (1024**3):.2f} GB - Telegram limiti 2GB. "
            f"Fayl to'g'ri bo'linmagan bo'lishi mumkin."
        )
    
    client = get_telegram_client()
    if not client:
        raise ValueError("Telegram bot sozlamalari noto'g'ri yoki kiritilmagan!")
        
    # Start client if not already running
    if not client.is_connected:
        print("Connecting Pyrogram client...")
        await client.start()
        
    cfg = config.get_config()
    channel = cfg["CHANNEL_USERNAME"]
    
    file_name = file_path.name
    ext = file_path.suffix.lower()
    video_extensions = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv"}
    
    # Wrapper for progress callback (Pyrogram expects standard args)
    async def pyrogram_progress_wrapper(current, total, *args):
        if progress_callback:
            if asyncio.iscoroutinefunction(progress_callback):
                await progress_callback(current, total)
            else:
                progress_callback(current, total)

    attempts = 5
    for attempt in range(attempts):
        try:
            if ext in video_extensions:
                print(f"Uploading {file_name} as Video (Attempt {attempt+1}/{attempts})...")
                await client.send_video(
                    chat_id=channel,
                    video=str(file_path),
                    caption=f"🎬 **{file_name}**\n\n{channel} kanali uchun maxsus yuklandi.",
                    progress=pyrogram_progress_wrapper
                )
            else:
                print(f"Uploading {file_name} as Document (Attempt {attempt+1}/{attempts})...")
                await client.send_document(
                    chat_id=channel,
                    document=str(file_path),
                    caption=f"📁 **{file_name}**\n\n{channel} kanali uchun maxsus yuklandi.",
                    progress=pyrogram_progress_wrapper
                )
            print(f"Successfully uploaded {file_name}")
            break # Success, exit retry loop
        except FloodWait as e:
            # e.value contains the number of seconds required to wait
            wait_time = e.value + 3
            print(f"Telegram rate limit hit (FloodWait). Waiting for {wait_time} seconds before retrying...")
            await asyncio.sleep(wait_time)
            if attempt == attempts - 1:
                # If it was the last attempt, raise the exception
                raise
