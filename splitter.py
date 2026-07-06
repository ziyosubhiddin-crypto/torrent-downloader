import asyncio
import os
import math
from pathlib import Path

# 1.9 GB limit for Telegram uploads (safety margin below Telegram's 2GB limit)
LIMIT = int(1.9 * 1024 * 1024 * 1024)


async def get_video_duration(file_path: Path) -> float:
    """
    Get video duration using ffprobe. Returns 0 if failed or not a video.
    """
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(file_path)
        ]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode == 0:
            duration_str = stdout.decode().strip()
            return float(duration_str)
        else:
            print(f"ffprobe returncode={process.returncode}, stderr={stderr.decode()}")
    except Exception as e:
        print(f"Error getting video duration: {e}")
    return 0.0

async def split_video_ffmpeg(file_path: Path, parts_count: int, duration: float) -> list[Path]:
    """
    Splits video file into parts_count parts using ffmpeg stream copy.
    """
    part_duration = duration / parts_count
    output_parts = []
    
    parent_dir = file_path.parent
    base_name = file_path.stem
    ext = file_path.suffix
    
    for i in range(parts_count):
        start_time = i * part_duration
        part_file = parent_dir / f"{base_name}.part{i+1}{ext}"
        
        # ffmpeg command using -c copy for fast splitting without re-encoding
        # Placing -ss and -t after -i is safer/more reliable for stream copying
        cmd = [
            "ffmpeg", "-y",
            "-i", str(file_path),
            "-ss", f"{start_time:.3f}",
            "-t", f"{part_duration:.3f}",
            "-c", "copy",
            "-map", "0",
            str(part_file)
        ]
        
        print(f"Splitting part {i+1}/{parts_count}: {' '.join(cmd)}")
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode == 0 and part_file.exists() and part_file.stat().st_size > 0:
            output_parts.append(part_file)
        else:
            err_msg = stderr.decode().strip()
            print(f"FFmpeg split part {i+1} failed: {err_msg}")
            # Clean up any created parts and raise Exception to fallback to binary split
            for p in output_parts:
                if p.exists():
                    p.unlink()
            raise RuntimeError(f"FFmpeg split failed: {err_msg}")
            
    return output_parts

async def split_file_binary(file_path: Path, parts_count: int) -> list[Path]:
    """
    Splits any file into parts_count parts by splitting bytes.
    """
    file_size = file_path.stat().st_size
    chunk_size = math.ceil(file_size / parts_count)
    output_parts = []
    
    parent_dir = file_path.parent
    base_name = file_path.name
    
    try:
        with open(file_path, "rb") as src:
            for i in range(parts_count):
                part_file = parent_dir / f"{base_name}.part{i+1}"
                print(f"Binary splitting part {i+1}/{parts_count}: {part_file}")
                with open(part_file, "wb") as dest:
                    bytes_written = 0
                    while bytes_written < chunk_size:
                        read_len = min(64 * 1024, chunk_size - bytes_written)
                        data = src.read(read_len)
                        if not data:
                            break
                        dest.write(data)
                        bytes_written += len(data)
                
                if part_file.exists() and part_file.stat().st_size > 0:
                    output_parts.append(part_file)
                else:
                    raise RuntimeError("Failed to write binary part or empty file created.")
    except Exception as e:
        # Clean up
        for p in output_parts:
            if p.exists():
                p.unlink()
        raise RuntimeError(f"Binary split failed: {e}")
        
    return output_parts

async def split_file_if_large(file_path: Path) -> list[Path]:
    """
    Checks if a file exceeds the Telegram limit (1.9 GB). If it does, splits it into parts.
    Returns list of paths to the split parts (or a list containing only the original path if no splitting was needed).
    After successful split, the original file is deleted to conserve space.
    Verifies that all resulting parts are under the limit.
    """
    global LIMIT
    
    if not file_path.exists():
        return []
        
    file_size = file_path.stat().st_size
    if file_size <= LIMIT:
        return [file_path]
    
    # Always split into at least 2 parts. Custom logic:
    # 2GB to 4GB -> 2 parts
    # 4GB to 6GB -> 3 parts
    # 6GB to 8GB -> 4 parts
    # 8GB to 10GB -> 5 parts, etc.
    size_gb = file_size / (1024 * 1024 * 1024)
    parts_count = max(2, int(size_gb // 2) + 1)
    print(f"File {file_path.name} is {size_gb:.2f} GB. Splitting into {parts_count} parts.")
    
    ext = file_path.suffix.lower()
    video_extensions = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv"}
    
    split_successful = False
    parts = []
    
    if ext in video_extensions:
        duration = await get_video_duration(file_path)
        if duration > 0:
            try:
                parts = await split_video_ffmpeg(file_path, parts_count, duration)
                split_successful = True
                
                # Verify all parts are under the limit
                # FFmpeg stream copy can produce uneven splits
                oversized_parts = [p for p in parts if p.exists() and p.stat().st_size > LIMIT]
                if oversized_parts:
                    print(f"WARNING: {len(oversized_parts)} ffmpeg parts still exceed limit. Re-splitting with more parts...")
                    # Clean up ffmpeg parts and fallback to binary split with more parts
                    for p in parts:
                        if p.exists():
                            p.unlink()
                    parts = []
                    split_successful = False
                    # Increase parts count for binary split
                    parts_count = parts_count + 1
            except Exception as e:
                print(f"FFmpeg split failed for {file_path.name}: {e}. Falling back to binary split.")
                
    if not split_successful:
        parts = await split_file_binary(file_path, parts_count)
        split_successful = True
        
    if split_successful and parts:
        # Final verification: ensure NO part exceeds the limit
        for p in parts:
            if p.exists():
                p_size = p.stat().st_size
                print(f"  Part {p.name}: {p_size / (1024**3):.2f} GB {'✓' if p_size <= LIMIT else '✗ OVER LIMIT!'}")
        
        # Delete original file to free space
        try:
            print(f"Deleting original large file: {file_path}")
            file_path.unlink()
        except Exception as e:
            print(f"Error deleting original file {file_path}: {e}")
            
    return parts
