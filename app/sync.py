import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
import requests
from app.fit_validator import IncompleteActivityError, validate_fit_file
from app.zwift_client import ZwiftClient

logger = logging.getLogger("zwift_connector.sync")
_history_lock = threading.RLock()

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

    tmp_path = paths["tokens"] + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2)
    os.replace(tmp_path, paths["tokens"])
    logger.info("Tokens successfully saved to disk.")

def load_history() -> dict:
    paths = get_data_paths()
    if os.path.exists(paths["history"]):
        try:
            with _history_lock:
                with open(paths["history"], "r", encoding="utf-8") as f:
                    data = json.load(f)
                    data.setdefault("downloaded", {})
                    data.setdefault("pending", {})
                    return data
        except Exception as e:
            logger.error(f"Error reading sync history file: {e}")
    return {"downloaded": {}, "pending": {}, "last_sync": None}

def save_history(history: dict):
    paths = get_data_paths()
    history.setdefault("downloaded", {})
    history.setdefault("pending", {})
    history["last_sync"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    tmp_path = paths["history"] + ".tmp"
    with _history_lock:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
        os.replace(tmp_path, paths["history"])

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

    now = datetime.now(timezone.utc)
    if time_window == "1_day":
        return now - timedelta(days=1)
    elif time_window == "1_week":
        return now - timedelta(days=7)
    elif time_window == "1_month":
        return now - timedelta(days=30)
    elif time_window == "1_year":
        return now - timedelta(days=365)

    return None


def _parse_iso_datetime(dt_str: str) -> datetime | None:
    if not dt_str or not isinstance(dt_str, str):
        return None
    cleaned = dt_str.strip()
    dt = None
    try:
        dt = datetime.fromisoformat(cleaned)
    except Exception:
        pass
    if dt is None and cleaned.endswith("Z"):
        cleaned_no_z = cleaned[:-1]
        try:
            dt = datetime.fromisoformat(cleaned_no_z + "+00:00")
        except Exception:
            pass
    if dt is None:
        try:
            if len(cleaned) >= 5 and cleaned[-5] in ("+", "-") and cleaned[-3] != ":":
                cleaned = cleaned[:-2] + ":" + cleaned[-2:]
            dt = datetime.fromisoformat(cleaned)
        except Exception:
            return None

    if dt is not None:
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc)
        return dt.replace(tzinfo=timezone.utc)
    return None


def is_activity_incomplete_metadata(data: dict) -> tuple[bool, str]:
    """Check Zwift API activity/details payload for incomplete stub activity indicators."""
    if not isinstance(data, dict):
        return True, "invalid activity metadata"
    if data.get("inProgress") is True:
        return True, "activity is still inProgress"
    end_date = data.get("endDate")
    if not end_date:
        return True, "activity has no endDate (still in progress)"
    end_dt = _parse_iso_datetime(end_date)
    if end_dt is not None:
        now = datetime.now(timezone.utc)
        if end_dt > now + timedelta(seconds=60):
            return True, f"activity endDate {end_date} is in the future"
    moving_ms = data.get("movingTimeInMs")
    dist_m = data.get("distanceInMeters")
    if (moving_ms is None or moving_ms <= 0) and (dist_m is None or dist_m <= 0):
        return True, f"movingTimeInMs={moving_ms}, distanceInMeters={dist_m}"
    return False, ""


def _extract_activity_id_from_filename(filename: str) -> str:
    workout_id = filename[:-4] if filename.endswith(".fit") else filename
    if "_activity_" in filename:
        workout_id = filename.split("_activity_", 1)[1].replace(".fit", "")
    elif "_workout_" in filename:
        workout_id = filename.split("_workout_", 1)[1].replace(".fit", "")
    return str(workout_id)


def cleanup_incomplete_downloads(
    downloads_dir: str | dict | None = None,
    downloaded_map: dict | None = None,
    pending_map: dict | None = None,
) -> int:
    """
    Scan the downloads directory for incomplete or corrupt .fit files,
    delete them from disk, and purge their entries from sync history.
    Returns the number of defective files removed.
    """
    auto_save_history = False
    history_obj = None

    if isinstance(downloads_dir, dict):
        downloads_dir = downloads_dir.get("downloads")

    if downloads_dir is None:
        paths = get_data_paths()
        downloads_dir = paths["downloads"]

    if downloaded_map is None:
        history_obj = load_history()
        downloaded_map = history_obj.setdefault("downloaded", {})
        pending_map = history_obj.setdefault("pending", {})
        auto_save_history = True
    elif (
        "downloaded" in downloaded_map
        and "last_sync" in downloaded_map
        and isinstance(downloaded_map.get("downloaded"), dict)
    ):
        history_obj = downloaded_map
        pending_map = history_obj.setdefault("pending", {})
        downloaded_map = history_obj["downloaded"]

    if not downloads_dir or not os.path.exists(downloads_dir):
        return 0

    removed_count = 0
    for fn in list(os.listdir(downloads_dir)):
        if not fn.endswith(".fit"):
            continue
        file_path = os.path.join(downloads_dir, fn)
        if not os.path.isfile(file_path):
            continue

        is_valid, reason = validate_fit_file(file_path)
        if not is_valid:
            logger.warning(f"Removing incomplete FIT file '{fn}' from disk: {reason}")
            try:
                os.remove(file_path)
            except OSError as e:
                logger.error(f"Failed to remove incomplete FIT file '{fn}': {e}")
                continue

            removed_count += 1
            extracted_id = _extract_activity_id_from_filename(fn)
            keys_to_remove = [
                k
                for k, v in downloaded_map.items()
                if str(k) == extracted_id
                or (isinstance(v, dict) and (v.get("filename") == fn or str(v.get("id")) == extracted_id))
            ]
            for k in keys_to_remove:
                downloaded_map.pop(k, None)
                logger.info(f"Removed incomplete activity {k} from sync history.")

            if pending_map is not None:
                pending_map.pop(extracted_id, None)

    if removed_count > 0 and auto_save_history and history_obj is not None:
        save_history(history_obj)

    return removed_count


def delete_activity_record(activity_id: str) -> bool:
    """
    Delete an activity from sync_history (both downloaded and pending) and remove
    matching FIT files from disk, allowing the activity to be re-downloaded.
    """
    if not activity_id or not str(activity_id).strip():
        return False

    paths = get_data_paths()
    str_id = str(activity_id).strip()

    with _history_lock:
        history = load_history()
        downloaded_map = history.setdefault("downloaded", {})
        pending_map = history.setdefault("pending", {})

        files_to_delete = set()
        matched_keys_downloaded = []
        if str_id in downloaded_map:
            matched_keys_downloaded.append(str_id)
            entry = downloaded_map[str_id]
            if isinstance(entry, dict) and entry.get("filename"):
                files_to_delete.add(entry["filename"])

        for k, v in list(downloaded_map.items()):
            k_str = str(k)
            if (
                k_str == str_id
                or _extract_activity_id_from_filename(k_str) == str_id
                or (
                    isinstance(v, dict)
                    and (
                        str(v.get("id")) == str_id
                        or _extract_activity_id_from_filename(v.get("filename", "")) == str_id
                    )
                )
            ):
                matched_keys_downloaded.append(k)
                if isinstance(v, dict) and v.get("filename"):
                    files_to_delete.add(v["filename"])

        if os.path.exists(paths["downloads"]):
            for fn in os.listdir(paths["downloads"]):
                clean_fn = fn[:-4] if fn.endswith(".tmp") else fn
                if clean_fn.endswith(".fit") and _extract_activity_id_from_filename(clean_fn) == str_id:
                    files_to_delete.add(fn)

        deletion_failed = False
        for fn in files_to_delete:
            safe_fn = os.path.basename(fn)
            fp = os.path.join(paths["downloads"], safe_fn)
            if os.path.exists(fp):
                try:
                    os.remove(fp)
                    logger.info(f"Deleted FIT file on disk: {fp}")
                except OSError as e:
                    logger.error(f"Failed to delete {fp}: {e}")
                    deletion_failed = True
            tmp_fp = fp + ".tmp"
            if os.path.exists(tmp_fp):
                try:
                    os.remove(tmp_fp)
                except OSError:
                    pass

        if deletion_failed:
            logger.error(f"Aborting sync history purge for activity {str_id} due to disk deletion failure.")
            return False

        for k in matched_keys_downloaded:
            downloaded_map.pop(k, None)

        pending_map.pop(str_id, None)
        for k in list(pending_map.keys()):
            if str(k) == str_id or _extract_activity_id_from_filename(str(k)) == str_id:
                pending_map.pop(k, None)

        history["downloaded"] = downloaded_map
        history["pending"] = pending_map
        save_history(history)
        return True


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
        profile_id = client._fetch_profile_id(access_token)
        if profile_id:
            tokens["profile_id"] = profile_id
            save_tokens(tokens)
        
    if not profile_id:
        return {"status": "error", "message": "Could not determine Zwift profile ID from token."}

    paths = get_data_paths()
    history = load_history()
    downloaded_map = history.get("downloaded", {})
    pending_map = history.get("pending", {})

    removed_files = cleanup_incomplete_downloads(paths["downloads"], downloaded_map, pending_map)
    if removed_files > 0:
        history["downloaded"] = downloaded_map
        history["pending"] = pending_map
        save_history(history)

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
            starts_dt = _parse_iso_datetime(starts_str)
            date_prefix = starts_dt.strftime("%Y-%m-%d") if starts_dt else (starts_str[:10] if starts_str else "activity")

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

            if any(k in activity for k in ("inProgress", "movingTimeInMs", "distanceInMeters", "endDate")):
                incomplete_meta, meta_reason = is_activity_incomplete_metadata(activity)
                if incomplete_meta:
                    logger.warning(
                        f"Skipping incomplete activity {activity_id} from activity list ({meta_reason})."
                    )
                    total_skipped += 1
                    continue
            
            try:
                details = {}
                try:
                    details = client.fetch_activity_details(access_token, activity_id) or {}
                except requests.HTTPError as http_err:
                    status_code = getattr(http_err.response, "status_code", None)
                    if status_code == 404:
                        if not (activity.get("fitFileBucket") and activity.get("fitFileKey")):
                            logger.warning(
                                f"Activity {activity_id} details returned 404 and no fallback S3 key is present. Skipping."
                            )
                            total_skipped += 1
                            continue
                        incomplete_act, act_reason = is_activity_incomplete_metadata(activity)
                        if incomplete_act:
                            logger.warning(
                                f"Activity {activity_id} details 404 and activity metadata incomplete: {act_reason}"
                            )
                            total_skipped += 1
                            continue
                    else:
                        raise

                if any(k in details for k in ("inProgress", "movingTimeInMs", "distanceInMeters", "endDate")):
                    incomplete_details, details_reason = is_activity_incomplete_metadata(details)
                    if incomplete_details:
                        logger.warning(
                            f"Skipping incomplete activity {activity_id} from activity details ({details_reason})."
                        )
                        total_skipped += 1
                        continue

                bucket = details.get("fitFileBucket") or activity.get("fitFileBucket")
                key = details.get("fitFileKey") or activity.get("fitFileKey")
                
                if not bucket or not key:
                    logger.warning(f"Activity {activity_id} does not have a FIT file bucket/key. Skipping.")
                    total_skipped += 1
                    continue

                moving_ms = details.get("movingTimeInMs") if details.get("movingTimeInMs") is not None else activity.get("movingTimeInMs")
                dist_m = details.get("distanceInMeters") if details.get("distanceInMeters") is not None else activity.get("distanceInMeters")

                # 2-cycle stabilization for recently completed activities (< 2 hours old)
                end_date_str = details.get("endDate") or activity.get("endDate") or starts_str
                end_dt = _parse_iso_datetime(end_date_str) if end_date_str else None
                now_utc = datetime.now(timezone.utc)
                is_recent = False
                if end_dt:
                    if (now_utc - end_dt) < timedelta(hours=2):
                        is_recent = True

                if is_recent:
                    s3_info = None
                    if hasattr(client, "get_file_info"):
                        try:
                            raw_info = client.get_file_info(bucket, key)
                            if isinstance(raw_info, dict):
                                s3_info = raw_info
                        except Exception:
                            s3_info = None

                    s3_etag = s3_info.get("etag") if s3_info else None
                    s3_size = s3_info.get("content_length") if s3_info else None

                    if activity_id not in pending_map:
                        pending_map[activity_id] = {
                            "movingTimeInMs": moving_ms,
                            "distanceInMeters": dist_m,
                            "fitFileKey": key,
                            "s3_etag": s3_etag,
                            "s3_size": s3_size,
                            "first_seen": now_utc.replace(tzinfo=None).isoformat() + "Z",
                        }
                        logger.info(
                            f"Activity {activity_id} completed recently (<2h). Snapshot recorded in pending (s3_size={s3_size}, etag={s3_etag}); skipping download for stabilization."
                        )
                        total_skipped += 1
                        continue
                    else:
                        prev = pending_map[activity_id]
                        prev_etag = prev.get("s3_etag")
                        prev_size = prev.get("s3_size")

                        s3_changed = False
                        if prev_etag and s3_etag and prev_etag != s3_etag:
                            s3_changed = True
                        if prev_size and s3_size and prev_size != s3_size:
                            s3_changed = True

                        metrics_changed = (
                            prev.get("movingTimeInMs") != moving_ms
                            or prev.get("distanceInMeters") != dist_m
                            or prev.get("fitFileKey") != key
                        )

                        if s3_changed or metrics_changed:
                            logger.info(
                                f"Activity {activity_id} file or metrics changed since last sync snapshot (s3_changed={s3_changed}, metrics_changed={metrics_changed}). Updating snapshot in pending; skipping download."
                            )
                            pending_map[activity_id]["movingTimeInMs"] = moving_ms
                            pending_map[activity_id]["distanceInMeters"] = dist_m
                            pending_map[activity_id]["fitFileKey"] = key
                            if s3_etag is not None:
                                pending_map[activity_id]["s3_etag"] = s3_etag
                            if s3_size is not None:
                                pending_map[activity_id]["s3_size"] = s3_size
                            pending_map[activity_id]["updated_at"] = now_utc.replace(tzinfo=None).isoformat() + "Z"
                            total_skipped += 1
                            continue
                        logger.info(
                            f"Activity {activity_id} stabilized across consecutive sync checks (file & metrics unchanged). Proceeding to download."
                        )

                client.download_file(
                    bucket,
                    key,
                    dest_path,
                    expected_timer_ms=moving_ms,
                    expected_distance_m=dist_m,
                )
                downloaded_map[activity_id] = {
                    "id": activity_id,
                    "starts": starts_str,
                    "filename": filename,
                    "downloaded_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"
                }
                pending_map.pop(activity_id, None)
                total_new += 1
                logger.info(f"Successfully downloaded new activity {activity_id} -> {filename}")
            except IncompleteActivityError as e:
                logger.warning(f"Skipping incomplete activity {activity_id}: {e}")
                total_skipped += 1
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
    history["pending"] = pending_map
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
