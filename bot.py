import asyncio
import os
import shutil
import time
from pathlib import Path
from pyrogram import Client, filters
from pyrogram.types import Message
import config
from downloader import download_torrent

app = Client(
    "torrent_bot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN
)

# Video extensions we want to upload as videos
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv"}

async def upload_progress_handler(current, total, client, status_msg, file_name, start_time):
    """
    Handles editing the status message with the upload progress.
    """
    now = time.time()
    # Throttling to edit message at most once every 4 seconds
    if not hasattr(upload_progress_handler, "last_update"):
        upload_progress_handler.last_update = 0
    
    if now - upload_progress_handler.last_update < 4:
        return
        
    upload_progress_handler.last_update = now
    
    percent = (current * 100) / total
    elapsed = now - start_time
    speed = current / elapsed if elapsed > 0 else 0
    
    # Format speed
    if speed < 1024:
        speed_str = f"{speed:.1f} B/s"
    elif speed < 1024 * 1024:
        speed_str = f"{speed / 1024:.1f} KB/s"
    else:
        speed_str = f"{speed / (1024 * 1024):.1f} MB/s"
        
    uploaded_mb = current / (1024 * 1024)
    total_mb = total / (1024 * 1024)
    
    try:
        await status_msg.edit_text(
            f"📤 **Kanalga yuklanmoqda...**\n"
            f"📁 Fayl: `{file_name}`\n"
            f"📊 Jarayon: `{percent:.1f}%`\n"
            f"⚡ Tezlik: `{speed_str}`\n"
            f"📦 Yuklandi: `{uploaded_mb:.1f} MB / {total_mb:.1f} MB`"
        )
    except Exception as e:
        print(f"Upload progress update error: {e}")

@app.on_channel_post(filters.chat(config.CHANNEL_USERNAME))
async def handle_new_torrent(client: Client, message: Message):
    torrent_source = None
    local_torrent_path = None
    file_display_name = "Torrent"
    
    # 1. Identify torrent file or magnet link
    if message.document and message.document.file_name.endswith(".torrent"):
        file_display_name = message.document.file_name
        status_msg = await client.send_message(
            config.CHANNEL_USERNAME,
            f"📥 **Torrent fayli aniqlandi:** `{file_display_name}`\nYuklab olishga tayyorlanmoqda...",
            reply_to_message_id=message.id
        )
        try:
            local_torrent_path = await message.download()
            torrent_source = local_torrent_path
        except Exception as e:
            await status_msg.edit_text(f"❌ Torrent faylini yuklab olishda xatolik:\n`{e}`")
            return
            
    elif message.text and message.text.strip().startswith("magnet:"):
        file_display_name = "Magnet Link"
        status_msg = await client.send_message(
            config.CHANNEL_USERNAME,
            f"📥 **Magnet havola aniqlandi.**\nYuklab olishga tayyorlanmoqda...",
            reply_to_message_id=message.id
        )
        torrent_source = message.text.strip()
    else:
        # Not a torrent or magnet link
        return

    # 2. Start the downloading process
    last_edit_time = 0
    task_dir = None
    downloaded_files = []
    
    try:
        async for progress in download_torrent(torrent_source):
            now = time.time()
            
            if progress["status"] == "metadata":
                if now - last_edit_time > 4:
                    await status_msg.edit_text(
                        f"🧲 **Magnet bog'lanishi o'rnatilmoqda...**\n"
                        f"⚡ Tezlik: `{progress['speed']}`\n"
                        f"⏳ Iltimos, kuting, metadata yuklab olinmoqda..."
                    )
                    last_edit_time = now
                    
            elif progress["status"] == "downloading":
                if now - last_edit_time > 4:
                    await status_msg.edit_text(
                        f"📥 **Kino yuklab olinmoqda...**\n"
                        f"📁 Nomi: `{file_display_name}`\n"
                        f"📊 Jarayon: `{progress['percent']}%`\n"
                        f"⚡ Tezlik: `{progress['speed']}`\n"
                        f"📦 Hajmi: `{progress['downloaded']} / {progress['total']}`\n"
                        f"⏳ Qolgan vaqt: `{progress['eta']}`"
                    )
                    last_edit_time = now
                    
            elif progress["status"] == "finished":
                task_dir = progress["task_dir"]
                downloaded_files = progress["files"]
                await status_msg.edit_text("✅ Yuklab olish yakunlandi! Kanalga yuklash boshlanmoqda...")
                
            elif progress["status"] == "failed":
                await status_msg.edit_text(f"❌ Yuklab olishda xatolik yuz berdi:\n`{progress['error']}`")
                # Clean up local torrent if downloaded
                if local_torrent_path and os.path.exists(local_torrent_path):
                    os.remove(local_torrent_path)
                return

        # 3. Upload files to Telegram
        if not downloaded_files:
            await status_msg.edit_text("❌ Yuklab olingan fayllar topilmadi!")
            if task_dir and os.path.exists(task_dir):
                shutil.rmtree(task_dir)
            if local_torrent_path and os.path.exists(local_torrent_path):
                os.remove(local_torrent_path)
            return

        for file_path in downloaded_files:
            file_name = file_path.name
            file_size = os.path.getsize(file_path)
            file_size_gb = file_size / (1024 * 1024 * 1024)
            
            # Telegram 2GB limit check
            if file_size_gb > 2.0:
                await client.send_message(
                    config.CHANNEL_USERNAME,
                    f"⚠️ Fayl hajmi 2GB dan katta (`{file_size_gb:.2f} GB`). Telegram botlar uchun 2GB dan katta fayllarni yuklash taqiqlangan.\n📁 Fayl: `{file_name}`",
                    reply_to_message_id=message.id
                )
                continue
            
            start_time = time.time()
            ext = file_path.suffix.lower()
            
            # Show initial upload state
            await status_msg.edit_text(f"📤 **Kanalga yuklanmoqda...**\n📁 Fayl: `{file_name}`")
            
            try:
                if ext in VIDEO_EXTENSIONS:
                    # Upload as video
                    await client.send_video(
                        chat_id=config.CHANNEL_USERNAME,
                        video=str(file_path),
                        caption=f"🎬 **{file_name}**\n\n@kinolarimmani8 kanali uchun maxsus yuklandi.",
                        reply_to_message_id=message.id,
                        progress=upload_progress_handler,
                        progress_args=(client, status_msg, file_name, start_time)
                    )
                else:
                    # Upload as general file document
                    await client.send_document(
                        chat_id=config.CHANNEL_USERNAME,
                        document=str(file_path),
                        caption=f"📁 **{file_name}**\n\n@kinolarimmani8 kanali uchun maxsus yuklandi.",
                        reply_to_message_id=message.id,
                        progress=upload_progress_handler,
                        progress_args=(client, status_msg, file_name, start_time)
                    )
            except Exception as e:
                await client.send_message(
                    config.CHANNEL_USERNAME,
                    f"❌ Faylni yuklashda xatolik: `{file_name}`\nXato matni: `{e}`",
                    reply_to_message_id=message.id
                )

        # 4. Final Cleanup
        await status_msg.delete()
        if task_dir and os.path.exists(task_dir):
            shutil.rmtree(task_dir)
        if local_torrent_path and os.path.exists(local_torrent_path):
            os.remove(local_torrent_path)

    except Exception as e:
        await client.send_message(
            config.CHANNEL_USERNAME,
            f"❌ Kutilmagan xatolik yuz berdi:\n`{e}`"
        )
        if task_dir and os.path.exists(task_dir):
            shutil.rmtree(task_dir)
        if local_torrent_path and os.path.exists(local_torrent_path):
            os.remove(local_torrent_path)

if __name__ == "__main__":
    print("Bot ishga tushmoqda...")
    app.run()
