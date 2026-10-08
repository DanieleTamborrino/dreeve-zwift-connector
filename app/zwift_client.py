import logging
import os
import time
import requests
import json
import base64

logger = logging.getLogger("zwift_connector.zwift_client")

ZWIFT_AUTH_URL = "https://secure.zwift.com/auth/realms/zwift/protocol/openid-connect/token"
ZWIFT_API_BASE = "https://us-or-rly101.zwift.com/api"

class ZwiftClient:
    def __init__(self, email: str = "", password: str = ""):
        self.email = email.strip() if email else ""
        self.password = password.strip() if password else ""

    def _fetch_profile_id(self, access_token: str) -> str:
        try:
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json"
            }
            url = f"{ZWIFT_API_BASE}/profiles/me"
            response = requests.get(url, headers=headers, timeout=30)
            if response.ok:
                return str(response.json().get("id", ""))
            else:
                logger.error(f"Failed to fetch profile/me: {response.status_code} {response.text}")
        except Exception as e:
            logger.error(f"Exception fetching profile/me: {e}")
        return ""

    def authenticate(self, email: str = None, password: str = None) -> dict:
        """Authenticate with Zwift using email and password."""
        email_to_use = email or self.email
        password_to_use = password or self.password

        payload = {
            "client_id": "Zwift_Mobile_Link",
            "grant_type": "password",
            "username": email_to_use,
            "password": password_to_use
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded"
        }
        logger.info("Authenticating with Zwift...")
        response = requests.post(ZWIFT_AUTH_URL, data=payload, headers=headers, timeout=30)
        
        if not response.ok:
            logger.error(f"Authentication failed ({response.status_code}): {response.text}")
            response.raise_for_status()
            
        data = response.json()
        access_token = data.get("access_token", "")
        profile_id = self._fetch_profile_id(access_token)
        if profile_id:
            data["profile_id"] = profile_id
        return data

    def refresh_access_token(self, refresh_token: str) -> dict:
        """Refresh expired access token."""
        payload = {
            "client_id": "Zwift_Mobile_Link",
            "grant_type": "refresh_token",
            "refresh_token": refresh_token
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded"
        }
        logger.info("Refreshing Zwift access token...")
        response = requests.post(ZWIFT_AUTH_URL, data=payload, headers=headers, timeout=30)
        
        if not response.ok:
            logger.error(f"Token refresh failed ({response.status_code}): {response.text}")
            response.raise_for_status()
            
        data = response.json()
        access_token = data.get("access_token", "")
        profile_id = self._fetch_profile_id(access_token)
        if profile_id:
            data["profile_id"] = profile_id
        return data

    def fetch_activities(self, access_token: str, profile_id: str, start: int = 0, limit: int = 50) -> list:
        """Fetch activities for the user."""
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json"
        }
        params = {
            "start": start,
            "limit": limit
        }
        logger.info(f"Fetching Zwift activities (start={start}, limit={limit})...")
        time.sleep(1.0) # static backoff
        
        url = f"{ZWIFT_API_BASE}/profiles/{profile_id}/activities"
        response = requests.get(url, headers=headers, params=params, timeout=30)
        
        if not response.ok:
            logger.error(f"Activities fetch failed ({response.status_code}): {response.text}")
            response.raise_for_status()
            
        return response.json()

    def fetch_activity_details(self, access_token: str, activity_id_str: str) -> dict:
        """Fetch details for a specific activity to get the .fit file S3 URL."""
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json"
        }
        params = {
            "fetchSnapshots": "true",
            "fetchEvent": "true"
        }
        logger.info(f"Fetching details for activity {activity_id_str}...")
        time.sleep(1.0) # static backoff
        
        url = f"{ZWIFT_API_BASE}/activities/{activity_id_str}"
        response = requests.get(url, headers=headers, params=params, timeout=30)
        
        if not response.ok:
            logger.error(f"Activity details fetch failed ({response.status_code}): {response.text}")
            response.raise_for_status()
            
        return response.json()

    def download_file(self, bucket: str, key: str, dest_path: str) -> bool:
        """Download .fit file from Zwift's S3 bucket."""
        tmp_path = dest_path + ".tmp"
        file_url = f"https://{bucket}.s3.amazonaws.com/{key}"
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        logger.info(f"Downloading FIT file from S3 -> {dest_path}")
        time.sleep(1.0) # static backoff
        
        response = requests.get(file_url, headers=headers, stream=True, timeout=60)
        if not response.ok:
            logger.error(f"File download failed ({response.status_code}): {response.text}")
            response.raise_for_status()

        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        success = False
        try:
            with open(tmp_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            try:
                os.chmod(tmp_path, 0o666)
            except OSError:
                pass

            os.replace(tmp_path, dest_path)
            success = True
        finally:
            if not success and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        return True
