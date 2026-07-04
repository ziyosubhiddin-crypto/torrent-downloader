import asyncio
import re
import os
from pathlib import Path
import uuid
from config import DOWNLOAD_PATH

# Regex to parse aria2c progress output:
# E.g. [#482b1d 1.1MiB/15MiB(7%) CN:3 SPD:1.1MiB ETA:12s]
PROGRESS_REGEX = re.compile(
    r'\[#\w+\s+(?P<downloaded>[^/]+)/(?P<total>[^\(]+)\((?P<percent>\d+)%\)\s+CN:\d+\s+SPD:(?P<speed>[^\s]+)\s+ETA:(?P<eta>[^\]\s]+)'
)

# Regex to parse metadata download output (no percentage, total is 0B/0B or similar)
# E.g. [#482b1d 0B/0B CN:1 SPD:5.2KiB]
METADATA_REGEX = re.compile(
    r'\[#\w+\s+0B/0B\s+CN:\d+\s+SPD:(?P<speed>[^\s\]]+)'
)

async def download_torrent(torrent_source: str):
    """
    Downloads a torrent file or magnet link asynchronously using aria2c in a separate folder.
    Yields progress dicts.
    On success, the last yielded dict contains status='finished', the task directory, and files list.
    On failure, the last yielded dict contains status='failed' and an error message.
    """
    # Create an isolated task directory
    task_id = str(uuid.uuid4())[:8]
    task_dir = DOWNLOAD_PATH / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "aria2c",
        "--seed-time=0",
        "--summary-interval=1",
        f"--dir={task_dir}",
        "--console-log-level=notice",
        torrent_source
    ]

    print(f"Starting aria2c download in: {task_dir}")
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )

    try:
        while True:
            line_bytes = await process.stdout.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", errors="ignore").strip()
            
            # Check for regular download progress
            match = PROGRESS_REGEX.search(line)
            if match:
                yield {
                    "status": "downloading",
                    "percent": int(match.group("percent")),
                    "speed": match.group("speed") + "/s",
                    "downloaded": match.group("downloaded"),
                    "total": match.group("total"),
                    "eta": match.group("eta")
                }
                continue
                
            # Check for metadata downloading (common in magnet links at start)
            meta_match = METADATA_REGEX.search(line)
            if meta_match:
                yield {
                    "status": "metadata",
                    "percent": 0,
                    "speed": meta_match.group("speed") + "/s",
                    "downloaded": "0B",
                    "total": "0B",
                    "eta": "N/A"
                }
                continue

            # Check if there is an error in the output
            if "download failed" in line.lower() or "exception caught" in line.lower():
                yield {
                    "status": "failed",
                    "error": line
                }

        return_code = await process.wait()
        
        if return_code == 0:
            # Find all files recursively in task_dir
            downloaded_files = []
            for root, _, files in os.walk(task_dir):
                for file in files:
                    # Ignore aria2 control files (.aria2)
                    if not file.endswith(".aria2"):
                        downloaded_files.append(Path(root) / file)
            
            yield {
                "status": "finished",
                "task_dir": task_dir,
                "files": downloaded_files
            }
        else:
            yield {
                "status": "failed",
                "error": f"aria2c exited with code {return_code}"
            }

    except Exception as e:
        try:
            process.kill()
        except:
            pass
        yield {
            "status": "failed",
            "error": str(e)
        }
