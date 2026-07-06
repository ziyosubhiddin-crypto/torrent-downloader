import asyncio
import os
import shutil
import time
from pathlib import Path
from pyrogram import Client, filters
from pyrogram.types import Message
import config
from downloader import download_torrent, download_metadata, get_torrent_files, download_single_file

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
    
    try:
        # Create unique task folder
        import uuid
        task_id = str(uuid.uuid4())[:8]
        task_dir = config.get_config()["DOWNLOAD_PATH"] / task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        # Download metadata first
        await status_msg.edit_text("🧲 **Magnet bog'lanishi o'rnatilmoqda...**\n⏳ Iltimos, kuting, metadata yuklab olinmoqda...")
        try:
            torrent_path = await download_metadata(torrent_source, task_dir)
        except Exception as e:
            await status_msg.edit_text(f"❌ Metama'lumotlarni yuklab olishda xatolik:\n`{e}`")
            if local_torrent_path and os.path.exists(local_torrent_path):
                os.remove(local_torrent_path)
            if task_dir and os.path.exists(task_dir):
                shutil.rmtree(task_dir)
            return

        # Get list of files in torrent
        try:
            all_files = await get_torrent_files(torrent_path)
        except Exception as e:
            await status_msg.edit_text(f"❌ Torrent fayllarini o'qishda xatolik:\n`{e}`")
            if local_torrent_path and os.path.exists(local_torrent_path):
                os.remove(local_torrent_path)
            if task_dir and os.path.exists(task_dir):
                shutil.rmtree(task_dir)
            return

        # Filter files (>= 100MB or largest)
        large_files = [f for f in all_files if f["size_bytes"] >= 100 * 1024 * 1024]
        if large_files:
            filtered_files = large_files
        else:
            largest_file = max(all_files, key=lambda f: f["size_bytes"])
            filtered_files = [largest_file]

        # Sort alphabetically
        filtered_files.sort(key=lambda f: f["path"])
        
        total_files = len(filtered_files)
        if total_files == 0:
            await status_msg.edit_text("❌ Yuklab olish uchun mos fayllar topilmadi!")
            if local_torrent_path and os.path.exists(local_torrent_path):
                os.remove(local_torrent_path)
            if task_dir and os.path.exists(task_dir):
                shutil.rmtree(task_dir)
            return

        # Sequential Loop (Download -> Upload -> Delete)
        for index, file_info in enumerate(filtered_files, start=1):
            file_idx = file_info["index"]
            rel_path = file_info["path"]
            file_name = Path(rel_path).name
            
            # Start downloading single file
            last_edit_time = 0
            download_success = False
            
            async for progress in download_single_file(torrent_path, file_idx, task_dir):
                now = time.time()
                if progress.get("status") == "downloading":
                    if now - last_edit_time > 4:
                        await status_msg.edit_text(
                            f"📥 **Kino yuklab olinmoqda ({index}/{total_files})...**\n"
                            f"📁 Fayl: `{file_name}`\n"
                            f"📊 Jarayon: `{progress['percent']}%`\n"
                            f"⚡ Tezlik: `{progress['speed']}`\n"
                            f"📦 Hajmi: `{progress['downloaded']} / {progress['total']}`\n"
                            f"⏳ Qolgan vaqt: `{progress['eta']}`"
                        )
                        last_edit_time = now
                elif progress.get("status") == "failed":
                    await status_msg.edit_text(f"❌ Faylni yuklab olishda xatolik yuz berdi ({file_name}):\n`{progress['error']}`")
                    if local_torrent_path and os.path.exists(local_torrent_path):
                        os.remove(local_torrent_path)
                    if task_dir and os.path.exists(task_dir):
                        shutil.rmtree(task_dir)
                    return
                elif progress.get("status") == "finished":
                    download_success = True

            if not download_success:
                await status_msg.edit_text(f"❌ Fayl yuklab olinmadi ({file_name})")
                if local_torrent_path and os.path.exists(local_torrent_path):
                    os.remove(local_torrent_path)
                if task_dir and os.path.exists(task_dir):
                    shutil.rmtree(task_dir)
                return

            local_file_path = task_dir / rel_path
            if not local_file_path.exists():
                await status_msg.edit_text(f"❌ Yuklab olingan fayl topilmadi: `{rel_path}`")
                if local_torrent_path and os.path.exists(local_torrent_path):
                    os.remove(local_torrent_path)
                if task_dir and os.path.exists(task_dir):
                    shutil.rmtree(task_dir)
                return

            # Split if large
            from splitter import split_file_if_large
            try:
                if local_file_path.stat().st_size > int(1.9 * 1024 * 1024 * 1024):
                    await status_msg.edit_text(f"✂️ **Katta fayl aniqlandi.** Bo'laklash jarayoni boshlanmoqda ({index}/{total_files})...")
                split_files = await split_file_if_large(local_file_path)
            except Exception as e:
                await client.send_message(
                    config.CHANNEL_USERNAME,
                    f"❌ Faylni bo'lishda xatolik yuz berdi: `{file_name}`\nXato matni: `{e}`",
                    reply_to_message_id=message.id
                )
                continue

            # Upload parts
            for p_index, part_path in enumerate(split_files, start=1):
                part_name = part_path.name
                part_info = f" (Bo'lak {p_index}/{len(split_files)})" if len(split_files) > 1 else ""
                
                start_time = time.time()
                ext = part_path.suffix.lower()
                
                # Show initial upload state
                await status_msg.edit_text(f"📤 **Kanalga yuklanmoqda ({index}/{total_files})...**\n📁 Fayl: `{file_name}{part_info}`")
                
                try:
                    if ext in VIDEO_EXTENSIONS:
                        await client.send_video(
                            chat_id=config.CHANNEL_USERNAME,
                            video=str(part_path),
                            caption=f"🎬 **{file_name}{part_info}**\n\n@kinolarimmani8 kanali uchun maxsus yuklandi.",
                            reply_to_message_id=message.id,
                            progress=upload_progress_handler,
                            progress_args=(client, status_msg, part_name, start_time)
                        )
                    else:
                        await client.send_document(
                            chat_id=config.CHANNEL_USERNAME,
                            document=str(part_path),
                            caption=f"📁 **{file_name}{part_info}**\n\n@kinolarimmani8 kanali uchun maxsus yuklandi.",
                            reply_to_message_id=message.id,
                            progress=upload_progress_handler,
                            progress_args=(client, status_msg, part_name, start_time)
                        )
                except Exception as e:
                    await client.send_message(
                        config.CHANNEL_USERNAME,
                        f"❌ Faylni yuklashda xatolik: `{part_name}`\nXato matni: `{e}`",
                        reply_to_message_id=message.id
                    )

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
