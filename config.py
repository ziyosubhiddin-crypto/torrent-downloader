import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

def get_config():
    """
    Returns a dictionary of current configuration settings.
    Doesn't raise error immediately to allow Web UI configuration setup.
    """
    # Reload environment to reflect any updates to .env file
    load_dotenv(override=True)
    
    api_id_str = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")
    bot_token = os.getenv("BOT_TOKEN")
    channel = os.getenv("CHANNEL_USERNAME", "@kinolarimmani8")
    download_dir = os.getenv("DOWNLOAD_DIR", "./downloads")
    
    is_configured = bool(api_id_str and api_hash and bot_token)
    
    # Ensure download path exists if configured
    download_path = Path(download_dir).resolve()
    if is_configured:
        download_path.mkdir(parents=True, exist_ok=True)
        
    return {
        "API_ID": int(api_id_str) if (api_id_str and api_id_str.strip().isdigit()) else None,
        "API_HASH": api_hash.strip() if api_hash else None,
        "BOT_TOKEN": bot_token.strip() if bot_token else None,
        "CHANNEL_USERNAME": channel.strip(),
        "DOWNLOAD_DIR": download_dir,
        "DOWNLOAD_PATH": download_path,
        "IS_CONFIGURED": is_configured
    }

def save_config(api_id: str, api_hash: str, bot_token: str, channel_username: str, download_dir: str = "./downloads"):
    """
    Saves new configuration values to the local .env file.
    """
    env_content = f"""# Telegram API Credentials (get from https://my.telegram.org)
API_ID={api_id.strip()}
API_HASH={api_hash.strip()}

# Telegram Bot Token (get from @BotFather)
BOT_TOKEN={bot_token.strip()}

# Telegram Channel to post to
CHANNEL_USERNAME={channel_username.strip()}

# Directory where downloaded movies will be stored temporarily
DOWNLOAD_DIR={download_dir.strip()}
"""
    
    # Write to .env
    with open(".env", "w") as f:
        f.write(env_content)
        
    # Force reload of environment variables in this python session
    load_dotenv(override=True)
