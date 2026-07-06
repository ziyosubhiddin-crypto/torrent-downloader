import asyncio
import re
import os
from pathlib import Path
import uuid
import config

TRACKERS_URL = "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt"

FALLBACK_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.coppersurfer.tk:6969/announce",
    "udp://tracker.leechers-paradise.org:6969/announce",
    "udp://tracker.internetwarriors.net:1337/announce",
    "udp://opentracker.i2p.rocks:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://open.demonii.com:1337/announce",
    "http://tracker.ipv6tracker.ru:80/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://explodie.org:6969/announce"
]

async def fetch_trackers():
    try:
        # Use curl which is native to Linux VPS to fetch trackers asynchronously without additional pip packages
        process = await asyncio.create_subprocess_exec(
            "curl", "-s", "--max-time", "3", TRACKERS_URL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL
        )
        stdout, _ = await process.communicate()
        if process.returncode == 0:
            text = stdout.decode("utf-8", errors="ignore")
            trackers = [line.strip() for line in text.split("\n") if line.strip()]
            if trackers:
                return trackers
    except Exception as e:
        print(f"Error fetching live trackers: {e}. Using fallback trackers.")
    return FALLBACK_TRACKERS

def parse_aria2_progress(line: str):
    """
    Robustly parses aria2c progress outputs like:
    [#482b1d 1.1MiB/15MiB(7%) CN:3 SPD:1.1MiB ETA:12s]
    [#2b7e7e 0B/1.0GiB(0%) CN:0 SD:0 DL:0B]
    """
    if not (line.startswith("[#") and "%" in line):
        return None
        
    try:
        content = line.strip("[]")
        parts = content.split()
        if len(parts) < 3:
            return None
            
        # Size info is in the second part: E.g. 1.1MiB/15MiB(7%)
        size_part = parts[1]
        if "/" not in size_part:
            return None
            
        downloaded, rest = size_part.split("/", 1)
        if "(" not in rest or ")" not in rest:
            return None
            
        total, percent_part = rest.split("(", 1)
        percent_str = percent_part.rstrip(")%")
        percent = int(percent_str) if percent_str.isdigit() else 0
        
        # Key-value parse for rest of fields
        kv = {}
        for p in parts[2:]:
            if ":" in p:
                k, v = p.split(":", 1)
                kv[k] = v
                
        # Handle speed field which can be SPD or DL
        speed = "0 B/s"
        if "SPD" in kv:
            speed = kv["SPD"]
            if not speed.lower().endswith("/s"):
                speed += "/s"
        elif "DL" in kv:
            speed = kv["DL"]
            if not speed.lower().endswith("/s"):
                speed += "/s"
                
        eta = "N/A"
        if "ETA" in kv:
            eta = kv["ETA"]
            
        return {
            "status": "downloading",
            "percent": percent,
            "speed": speed,
            "downloaded": downloaded,
            "total": total,
            "eta": eta
        }
    except Exception:
        return None

async def read_lines(stream):
    """
    Read from stream splitting on both \n and \r (for aria2c carriage-return progress lines).
    """
    buf = b""
    while True:
        chunk = await stream.read(512)
        if not chunk:
            if buf:
                yield buf.decode("utf-8", errors="ignore").strip()
            break
        buf += chunk
        # Split on both \r and \n
        parts = re.split(b'[\r\n]+', buf)
        # Last part may be incomplete — keep it in the buffer
        buf = parts[-1]
        for part in parts[:-1]:
            line = part.decode("utf-8", errors="ignore").strip()
            if line:
                yield line

async def download_metadata(torrent_source: str, task_dir: Path):
    """
    Downloads only the .torrent metadata file for a magnet link.
    Returns the Path to the downloaded .torrent file.
    If torrent_source is already a file path, returns it as Path.
    """
    if not torrent_source.startswith("magnet:"):
        return Path(torrent_source)
        
    trackers = await fetch_trackers()
    trackers_str = ",".join(trackers)
    
    cmd = [
        "aria2c",
        "--bt-metadata-only=true",
        "--bt-save-metadata=true",
        f"--dir={task_dir}",
        "--console-log-level=notice",
        "--enable-dht=true",
        "--enable-dht6=true",
        "--bt-enable-lpd=true",
        "--enable-peer-exchange=true",
        "--bt-max-peers=120",
        "--max-connection-per-server=16",
        "--listen-port=6881-6999",
        f"--bt-tracker={trackers_str}",
        torrent_source
    ]
    
    print(f"Fetching torrent metadata for magnet in: {task_dir}")
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )
    
    try:
        # We can read output line by line or wait
        async for line in read_lines(process.stdout):
            print(f"[aria2c-metadata] {line}")
            
        return_code = await process.wait()
        if return_code != 0:
            raise RuntimeError(f"Metadata download failed with exit code {return_code}")
            
        # Find downloaded .torrent file in task_dir
        torrent_files = list(task_dir.glob("*.torrent"))
        if not torrent_files:
            raise FileNotFoundError("Metadata was downloaded but no .torrent file was found in task directory.")
            
        # Return the path of the torrent file (usually there is only one)
        return torrent_files[0]
    except Exception as e:
        raise RuntimeError(f"Failed to fetch magnet metadata: {e}")
    finally:
        try:
            if process.returncode is None:
                process.kill()
        except Exception:
            pass

async def get_torrent_files(torrent_path: Path) -> list[dict]:
    """
    Runs aria2c -S to list all files in the torrent.
    Parses the output and returns a list of dictionaries with file index, path, and size.
    """
    cmd = ["aria2c", "-S", str(torrent_path)]
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"Failed to read torrent files: {stderr.decode('utf-8', errors='ignore')}")
        
    stdout_text = stdout.decode('utf-8', errors='ignore')
    
    lines = stdout_text.splitlines()
    files = []
    current_idx = None
    current_path = None
    
    for line in lines:
        line_clean = line.rstrip()
        if not line_clean:
            continue
            
        # Match index and path: "  1|./path/to/file" or " 12|path/to/file"
        match_idx = re.match(r'^\s*(\d+)\|(.+)$', line_clean)
        if match_idx:
            current_idx = int(match_idx.group(1))
            current_path = match_idx.group(2).strip()
            if current_path.startswith("./"):
                current_path = current_path[2:]
            continue
            
        # Match size line: "   |755MiB (791,674,880)"
        match_size = re.match(r'^\s*\|([^(]+)(?:\(([\d,]+)\))?$', line_clean)
        if match_size and current_idx is not None:
            size_str = match_size.group(1).strip()
            bytes_str = match_size.group(2)
            if bytes_str:
                size_bytes = int(bytes_str.replace(",", ""))
            else:
                size_bytes = 0
                
            files.append({
                "index": current_idx,
                "path": current_path,
                "size_str": size_str,
                "size_bytes": size_bytes
            })
            current_idx = None
            current_path = None
            
    return files

async def download_single_file(torrent_path: Path, file_index: int, task_dir: Path):
    """
    Downloads only a specific file from the torrent using its index.
    Yields progress updates.
    """
    trackers = await fetch_trackers()
    trackers_str = ",".join(trackers)

    cmd = [
        "aria2c",
        "--seed-time=0",
        "--summary-interval=1",
        f"--dir={task_dir}",
        "--console-log-level=notice",
        "--enable-dht=true",
        "--enable-dht6=true",
        "--bt-enable-lpd=true",
        "--enable-peer-exchange=true",
        "--bt-max-peers=120",
        "--max-connection-per-server=16",
        "--split=16",
        "--min-split-size=1M",
        "--listen-port=6881-6999",
        "--disk-cache=64M",
        "--file-allocation=none",
        f"--bt-tracker={trackers_str}",
        f"--select-file={file_index}",
        str(torrent_path)
    ]

    print(f"Starting single file download for index {file_index} in: {task_dir}")
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )

    try:
        async for line in read_lines(process.stdout):
            print(f"[aria2c-file-{file_index}] {line}")

            progress_data = parse_aria2_progress(line)
            if progress_data:
                yield progress_data
                continue

            if "download failed" in line.lower() or "error" in line.lower():
                if "result" in line.lower() or "gid" in line.lower():
                    yield {
                        "status": "failed",
                        "error": line
                      }

        return_code = await process.wait()
        
        if return_code == 0:
            yield {
                "status": "finished"
            }
        else:
            yield {
                "status": "failed",
                "error": f"aria2c exited with code {return_code}"
            }

    except Exception as e:
        yield {
            "status": "failed",
            "error": str(e)
        }
    finally:
        try:
            if process.returncode is None:
                process.kill()
        except Exception:
            pass

async def download_torrent(torrent_source: str):
    """
    Fallback wrapper for downloading entire torrent content at once (old behavior).
    Yields progress dicts.
    """
    cfg = config.get_config()
    task_id = str(uuid.uuid4())[:8]
    task_dir = cfg["DOWNLOAD_PATH"] / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    trackers = await fetch_trackers()
    trackers_str = ",".join(trackers)

    cmd = [
        "aria2c",
        "--seed-time=0",
        "--summary-interval=1",
        f"--dir={task_dir}",
        "--console-log-level=notice",
        "--enable-dht=true",
        "--enable-dht6=true",
        "--bt-enable-lpd=true",
        "--enable-peer-exchange=true",
        "--bt-max-peers=120",
        "--max-connection-per-server=16",
        "--split=16",
        "--min-split-size=1M",
        "--listen-port=6881-6999",
        "--disk-cache=64M",
        "--file-allocation=none",
        f"--bt-tracker={trackers_str}",
        torrent_source
    ]

    print(f"Starting standard full download in: {task_dir}")
    yield {"status": "started", "task_dir": task_dir}
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )

    try:
        async for line in read_lines(process.stdout):
            progress_data = parse_aria2_progress(line)
            if progress_data:
                yield progress_data
                continue

            if "download failed" in line.lower() or "error" in line.lower():
                if "result" in line.lower() or "gid" in line.lower():
                    yield {
                        "status": "failed",
                        "error": line
                    }

        return_code = await process.wait()
        
        if return_code == 0:
            downloaded_files = []
            for root, _, files in os.walk(task_dir):
                for file in files:
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
        yield {
            "status": "failed",
            "error": str(e)
        }
    finally:
        try:
            if process.returncode is None:
                process.kill()
        except Exception:
            pass

