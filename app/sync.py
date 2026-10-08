import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from app.zwift_client import ZwiftClient

logger = logging.getLogger("zwift_connector.sync")

def get_data_paths():
    data_dir = os.getenv("DATA_DIR", "/data")
    config_dir = os.getenv("STATE_DIR") or os.path.join(data_dir, "config")
    downloads_dir = os.getenv("WATCH_DIR") or os.getenv("DOWNLOADS_DIR") or os.path.join(data_dir, "downloads")
    
    os.makedirs(config_dir, exist_ok=True)
    os.makedirs(downloads_dir, exist_ok=True)

    return {
        "tokens": os.path.join(config_dir, "tokens.json"),
        "history": os.path.join(config_dir, "sync_history.json"),
        "downloads": downloads_dir
    }

def load_tokens() -> dict:
    paths = get_data_paths()
    if os.path.exists(paths["tokens"]):
        try:
            with open(paths["tokens"], "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error reading tokens file: {e}")
    return {}

def save_tokens(tokens: dict):
    paths = get_data_paths()
    if "expires_at" not in tokens:
        expires_in = tokens.get("expires_in")
        if expires_in is None:
            expires_in = 3600
        tokens["expires_at"] = int(time.time()) + int(expires_in)

    with open(paths["tokens"], "w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2)
    logger.info("Tokens successfully saved to disk.")

def load_history() -> dict:
    paths = get_data_paths()
    if os.path.exists(paths["history"]):
        try:
            with open(paths["history"], "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error reading sync history file: {e}")
    return {"downloaded": {}, "last_sync": None}

def save_history(history: dict):
    paths = get_data_paths()
    history["last_sync"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"
    with open(paths["history"], "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)

def get_all_activities() -> list:
    """
    Scan disk downloads folder and merge with sync_history.json
    to ensure 100% of downloaded workouts are accurately listed in the Web UI,
    even if downstream consumers (like Dreeve) move/delete files from the watch folder.
    """
    paths = get_data_paths()
    history = load_history()
    downloaded_map = history.get("downloaded", {})
    activities = []

    disk_files = {}
    if os.path.exists(paths["downloads"]):
        for fn in os.listdir(paths["downloads"]):
            if fn.endswith(".fit"):
                file_path = os.path.join(paths["downloads"], fn)
                size_bytes = os.path.getsize(file_path) if os.path.exists(file_path) else 0
                disk_files[fn] = size_bytes

    seen_ids = set()

    # Process all workouts recorded in history
    for workout_id, hist_entry in downloaded_map.items():
        str_id = str(workout_id)
        seen_ids.add(str_id)
        fn = hist_entry.get("filename") or f"activity_{str_id}.fit"
        
        if fn in disk_files:
            size_bytes = disk_files[fn]
            size_str = f"{size_bytes / 1024:.1f} KB"
        else:
            size_str = "N/A (Processed)"

        starts = hist_entry.get("starts") or "N/A"
        downloaded_at = hist_entry.get("downloaded_at")

        activities.append({
            "id": str_id,
            "starts": starts,
            "filename": fn,
            "downloaded_at": downloaded_at,
            "size_str": size_str
        })

    # Add any disk files that were not in sync_history
    for fn, size_bytes in disk_files.items():
        workout_id = fn.replace(".fit", "")
        workout_date = "N/A"
        
        if "_activity_" in fn:
            parts = fn.split("_activity_")
            workout_date = parts[0]
            workout_id = parts[1].replace(".fit", "")
        elif "_workout_" in fn:
            parts = fn.split("_workout_")
            workout_date = parts[0]
            workout_id = parts[1].replace(".fit", "")

        str_id = str(workout_id)
        if str_id in seen_ids:
            continue

        seen_ids.add(str_id)
        file_path = os.path.join(paths["downloads"], fn)
        downloaded_at = None
        if os.path.exists(file_path):
            mtime = os.path.getmtime(file_path)
            downloaded_at = datetime.fromtimestamp(mtime, timezone.utc).replace(tzinfo=None).isoformat() + "Z"

        activities.append({
            "id": str_id,
            "starts": workout_date,
            "filename": fn,
            "downloaded_at": downloaded_at,
            "size_str": f"{size_bytes / 1024:.1f} KB"
        })

    activities.sort(key=lambda x: x.get("starts", ""), reverse=True)
    return activities

def get_cutoff_datetime(time_window: str):
    """Calculate datetime cutoff based on selected time window."""
    if not time_window or time_window == "all_time":
        return None

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if time_window == "1_day":
        return now - timedelta(days=1)
    elif time_window == "1_week":
        return now - timedelta(days=7)
    elif time_window == "1_month":
        return now - timedelta(days=30)
    elif time_window == "1_year":
        return now - timedelta(days=365)

    return None

def perform_sync(time_window: str = None) -> dict:
    if not time_window:
        time_window = os.getenv("SYNC_TIME_WINDOW", "1_week")

    email = os.getenv("ZWIFT_EMAIL")
    password = os.getenv("ZWIFT_PASSWORD")

    if not email or not password:
        return {"status": "error", "message": "ZWIFT_EMAIL and ZWIFT_PASSWORD must be set in environment."}

    client = ZwiftClient(email, password)
    tokens = load_tokens()
    
    current_time = int(time.time())
    
    # Authenticate or refresh
    if not tokens or "access_token" not in tokens:
        logger.info("No tokens found. Authenticating with Zwift...")
        try:
            tokens = client.authenticate()
            save_tokens(tokens)
        except Exception as e:
            return {"status": "error", "message": f"Failed to authenticate with Zwift: {str(e)}"}
    else:
        expires_at = tokens.get("expires_at", 0)
        if current_time > (expires_at - 300):
            refresh_token = tokens.get("refresh_token")
            if not refresh_token:
                logger.info("No refresh token. Re-authenticating...")
                try:
                    tokens = client.authenticate()
                    save_tokens(tokens)
                except Exception as e:
                    return {"status": "error", "message": f"Failed to authenticate with Zwift: {str(e)}"}
            else:
                logger.info("Access token expired or expiring soon. Refreshing token...")
                try:
                    new_tokens = client.refresh_access_token(refresh_token)
                    if "refresh_token" not in new_tokens:
                        new_tokens["refresh_token"] = refresh_token
                    # Make sure we keep profile_id if not present
                    if "profile_id" not in new_tokens and "profile_id" in tokens:
                        new_tokens["profile_id"] = tokens["profile_id"]
                    save_tokens(new_tokens)
                    tokens = new_tokens
                except Exception as e:
                    logger.error(f"Failed to refresh access token: {e}")
                    # Try password auth as fallback
                    try:
                        tokens = client.authenticate()
                        save_tokens(tokens)
                    except Exception as e2:
                        return {"status": "error", "message": f"Failed to authenticate with Zwift: {str(e2)}"}

    access_token = tokens["access_token"]
    profile_id = tokens.get("profile_id")
    if not profile_id:
        # Fallback to decode it if missing
        profile_id = client._decode_jwt_profile_id(access_token)
        
    if not profile_id:
        return {"status": "error", "message": "Could not determine Zwift profile ID from token."}

    paths = get_data_paths()
    history = load_history()
    downloaded_map = history.get("downloaded", {})

    cutoff_dt = get_cutoff_datetime(time_window)
    if cutoff_dt:
        logger.info(f"Filtering sync to time window: {time_window} (Cutoff: {cutoff_dt.isoformat()}Z)")

    start = 0
    limit = 50
    total_new = 0
    total_skipped = 0
    total_processed = 0
    errors = []
    stop_sync = False

    logger.info(f"Starting Zwift activity sync (time_window={time_window})...")

    while not stop_sync:
        try:
            activities = client.fetch_activities(access_token, profile_id, start=start, limit=limit)
        except Exception as e:
            logger.error(f"Error fetching activities starting at {start}: {e}")
            errors.append(f"Offset {start} fetch error: {str(e)}")
            break

        if not activities:
            logger.info("No more activities returned from API.")
            break

        consecutive_existing_count = 0

        for activity in activities:
            total_processed += 1
            activity_id = str(activity.get("id_str", activity.get("id")))
            starts_str = activity.get("startDate") or ""
            
            starts_dt = None
            date_prefix = "activity"
            if starts_str:
                try:
                    starts_dt = datetime.fromisoformat(starts_str.replace("Z", "+00:00")).replace(tzinfo=None)
                    date_prefix = starts_dt.strftime("%Y-%m-%d")
                except Exception:
                    date_prefix = starts_str[:10]

            if cutoff_dt and starts_dt and starts_dt < cutoff_dt:
                logger.info(f"Activity {activity_id} ({starts_str}) is older than cutoff {cutoff_dt.isoformat()}Z. Time window limit reached.")
                stop_sync = True
                break

            filename = f"{date_prefix}_activity_{activity_id}.fit"
            dest_path = os.path.join(paths["downloads"], filename)

            verify_disk = os.getenv("VERIFY_FILES_ON_DISK", "false").lower() in ["true", "1", "yes"]
            is_already_downloaded = (activity_id in downloaded_map) and (not verify_disk or os.path.exists(dest_path))

            if is_already_downloaded:
                total_skipped += 1
                consecutive_existing_count += 1
                logger.debug(f"Activity {activity_id} ({filename}) already downloaded. Skipping.")
                continue

            consecutive_existing_count = 0
            
            try:
                details = client.fetch_activity_details(access_token, activity_id)
                bucket = details.get("fitFileBucket")
                key = details.get("fitFileKey")
                
                if not bucket or not key:
                    logger.warning(f"Activity {activity_id} does not have a FIT file bucket/key. Skipping.")
                    continue
                    
                client.download_file(bucket, key, dest_path)
                downloaded_map[activity_id] = {
                    "id": activity_id,
                    "starts": starts_str,
                    "filename": filename,
                    "downloaded_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"
                }
                total_new += 1
                logger.info(f"Successfully downloaded new activity {activity_id} -> {filename}")
            except Exception as e:
                logger.error(f"Failed to download FIT file for activity {activity_id}: {e}")
                errors.append(f"Activity {activity_id} download error: {str(e)}")

        if stop_sync:
            break

        if consecutive_existing_count >= len(activities) and len(activities) > 0:
            logger.info("Encountered fully synced page of existing activities. Incremental sync complete!")
            break

        if len(activities) < limit:
            break
        start += limit

    history["downloaded"] = downloaded_map
    save_history(history)

    result = {
        "status": "success" if not errors else "partial_success",
        "new_downloads": total_new,
        "skipped": total_skipped,
        "total_processed": total_processed,
        "time_window": time_window,
        "errors": errors,
        "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"
    }

    logger.info(f"Sync complete ({time_window}). New downloads: {total_new}, Skipped: {total_skipped}, Errors: {len(errors)}")
    return result
