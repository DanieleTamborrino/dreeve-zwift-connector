import json
import os
import shutil
import struct
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import requests

from app.fit_validator import (
    IncompleteActivityError,
    UINT32_INVALID,
    validate_fit_file,
)
from app.sync import (
    cleanup_incomplete_downloads,
    is_activity_incomplete_metadata,
    perform_sync,
)
from app.zwift_client import ZwiftClient


def build_synthetic_fit(
    num_records: int = 40,
    include_session: bool = True,
    start_time: int = 1162067496,
    total_timer_time: int = 40000,
    total_distance: int = 100000,
    big_endian: bool = False,
    use_compressed_ts: bool = False,
    include_dev_data: bool = False,
    pad_to_size: int = 4200,
) -> bytes:
    endian = ">" if big_endian else "<"
    arch = 1 if big_endian else 0
    body = bytearray()

    # Definition for record (local_mesg=0, global_mesg=20)
    # Field 253 (timestamp, 4B, uint32), Field 0 (position_lat, 4B, sint32)
    def_hdr_0 = 0x40 | (0x20 if include_dev_data else 0x00) | 0x00
    body.extend(struct.pack(f"{endian}BBBHB", def_hdr_0, 0, arch, 20, 2))
    body.extend(bytes([253, 4, 134, 0, 4, 133]))
    if include_dev_data:
        body.append(1)  # 1 dev field
        body.extend(bytes([0, 2, 0]))  # 2 bytes dev data

    for i in range(num_records):
        if use_compressed_ts:
            # Compressed timestamp header for local_mesg=0: bit 7 = 1, bits 6..5 = 00
            data_hdr = 0x80 | (i & 0x1F)
        else:
            data_hdr = 0x00
        body.append(data_hdr)
        body.extend(struct.pack(f"{endian}II", (start_time + i) & 0xFFFFFFFF, 1000 + i))
        if include_dev_data:
            body.extend(b"\x01\x02")

    if include_session:
        # Definition for session (local_mesg=1, global_mesg=18)
        # Fields: 2 (start_time, 4B), 8 (total_timer_time, 4B), 9 (total_distance, 4B)
        body.extend(struct.pack(f"{endian}BBBHB", 0x41, 0, arch, 18, 3))
        body.extend(bytes([2, 4, 134, 8, 4, 134, 9, 4, 134]))
        body.append(0x01)
        body.extend(
            struct.pack(
                f"{endian}III", start_time, total_timer_time, total_distance
            )
        )

    # Pad with dummy record definitions if needed to reach pad_to_size
    while 14 + len(body) + 2 < pad_to_size:
        # Definition for pad message (local_mesg=2, global_mesg=250) with 64-byte field
        body.extend(struct.pack(f"{endian}BBBHB", 0x42, 0, arch, 250, 1))
        body.extend(bytes([0, 64, 7]))
        body.append(0x02)
        body.extend(b"\x00" * 64)

    header = struct.pack("<BBHI4sH", 14, 0x20, 2100, len(body), b".FIT", 0)
    return header + bytes(body) + b"\x00\x00"


class TestFitValidator(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _write_tmp(self, name: str, data: bytes) -> str:
        path = os.path.join(self.tmp_dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_real_sample_files_in_downloads(self):
        downloads_dir = "/home/daniele/Downloads"
        defective = os.path.join(
            downloads_dir, "2026-10-27_activity_2258415478529654800.fit"
        )
        valid_files = [
            "2026-10-27_activity_2258418450361794592.fit",
            "2026-07-23_activity_2188838904419860528.fit",
            "2026-10-08_activity_2244490604534874144.fit",
            "2026-07-25_activity_2190182450351587328.fit",
            "2026-10-12_activity_2247404910066843696.fit",
        ]

        if os.path.exists(defective):
            ok, reason = validate_fit_file(defective)
            self.assertFalse(ok)
            self.assertIn("below minimum threshold", reason)

            # Even if size threshold is bypassed, internal records/session check must reject it
            ok_no_size, reason_no_size = validate_fit_file(
                defective, min_records=30, min_size_bytes=100
            )
            self.assertFalse(ok_no_size)
            self.assertIn("insufficient data records", reason_no_size)

        for vf in valid_files:
            vpath = os.path.join(downloads_dir, vf)
            if os.path.exists(vpath):
                ok, reason = validate_fit_file(vpath)
                self.assertTrue(ok, f"Expected {vf} to be valid, got: {reason}")

    def test_synthetic_valid_and_variants(self):
        for big_endian in (False, True):
            for compressed in (False, True):
                for dev_data in (False, True):
                    data = build_synthetic_fit(
                        num_records=35,
                        big_endian=big_endian,
                        use_compressed_ts=compressed,
                        include_dev_data=dev_data,
                    )
                    path = self._write_tmp("valid.fit", data)
                    ok, reason = validate_fit_file(path)
                    self.assertTrue(ok, reason)

    def test_invalid_conditions(self):
        # Missing file
        ok, reason = validate_fit_file(os.path.join(self.tmp_dir, "missing.fit"))
        self.assertFalse(ok)

        # Too small
        p_small = self._write_tmp("small.fit", b"0" * 1533)
        ok, reason = validate_fit_file(p_small)
        self.assertFalse(ok)
        self.assertIn("below minimum threshold", reason)

        # Bad signature
        p_sig = self._write_tmp("bad_sig.fit", b"\x0e\x20\x00\x08\x10\x00\x00\x00XXXX" + b"\x00" * 5000)
        ok, reason = validate_fit_file(p_sig)
        self.assertFalse(ok)
        self.assertIn("signature", reason)

        # Truncated payload
        header = struct.pack("<BBHI4sH", 14, 0x20, 2100, 99999, b".FIT", 0)
        p_trunc = self._write_tmp("trunc.fit", header + b"\x00" * 5000)
        ok, reason = validate_fit_file(p_trunc)
        self.assertFalse(ok)
        self.assertIn("truncated FIT payload", reason)

        # Insufficient records
        p_few = self._write_tmp("few.fit", build_synthetic_fit(num_records=5))
        ok, reason = validate_fit_file(p_few)
        self.assertFalse(ok)
        self.assertIn("insufficient data records", reason)

        # Missing session
        p_nosess = self._write_tmp("nosess.fit", build_synthetic_fit(include_session=False))
        ok, reason = validate_fit_file(p_nosess)
        self.assertFalse(ok)
        self.assertIn("missing FIT session", reason)

        # Invalid session start_time (0xFFFFFFFF)
        p_bad_start = self._write_tmp(
            "bad_start.fit", build_synthetic_fit(start_time=UINT32_INVALID)
        )
        ok, reason = validate_fit_file(p_bad_start)
        self.assertFalse(ok)
        self.assertIn("start_time is invalid", reason)

        # Invalid session total_timer_time (0)
        p_zero_timer = self._write_tmp(
            "zero_timer.fit", build_synthetic_fit(total_timer_time=0)
        )
        ok, reason = validate_fit_file(p_zero_timer)
        self.assertFalse(ok)
        self.assertIn("invalid session total_timer_time", reason)

        # Session missing start_time field (session_start_time is None)
        body_no_start = bytearray(build_synthetic_fit(include_session=False)[14:-2])
        body_no_start.extend(struct.pack("<BBBHB", 0x41, 0, 0, 18, 1))
        body_no_start.extend(bytes([8, 4, 134]))  # only total_timer_time, no field 2
        body_no_start.append(0x01)
        body_no_start.extend(struct.pack("<I", 40000))
        hdr_no_start = struct.pack("<BBHI4sH", 14, 0x20, 2100, len(body_no_start), b".FIT", 0)
        p_none_start = self._write_tmp("none_start.fit", hdr_no_start + bytes(body_no_start) + b"\x00\x00")
        ok, reason = validate_fit_file(p_none_start)
        self.assertFalse(ok)
        self.assertIn("start_time is invalid", reason)

        # Truncated 2-byte CRC trailer (file length == header_size + data_size without 2B CRC)
        valid_bytes = build_synthetic_fit()
        p_no_crc_bytes = self._write_tmp("no_crc_bytes.fit", valid_bytes[:-1])
        ok, reason = validate_fit_file(p_no_crc_bytes)
        self.assertFalse(ok)
        self.assertIn("truncated FIT payload", reason)

        # Corrupted non-zero file CRC-16
        bad_crc_bytes = valid_bytes[:-2] + b"\x34\x12"
        p_bad_crc = self._write_tmp("bad_crc.fit", bad_crc_bytes)
        ok, reason = validate_fit_file(p_bad_crc)
        self.assertFalse(ok)
        self.assertIn("FIT file CRC mismatch", reason)


class TestSyncAndDownloadGuards(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.config_dir = os.path.join(self.tmp_dir, "config")
        self.downloads_dir = os.path.join(self.tmp_dir, "downloads")
        os.makedirs(self.config_dir, exist_ok=True)
        os.makedirs(self.downloads_dir, exist_ok=True)
        self.env_patcher = patch.dict(
            os.environ,
            {
                "DATA_DIR": self.tmp_dir,
                "STATE_DIR": self.config_dir,
                "DOWNLOADS_DIR": self.downloads_dir,
                "ZWIFT_EMAIL": "test@example.com",
                "ZWIFT_PASSWORD": "secret",
                "SYNC_TIME_WINDOW": "all_time",
            },
        )
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @patch("app.zwift_client.time.sleep")
    @patch("app.zwift_client.requests.get")
    def test_download_file_atomic_guard(self, mock_get, _mock_sleep):
        client = ZwiftClient("test@example.com", "secret")
        dest_path = os.path.join(self.downloads_dir, "2026-10-27_activity_111.fit")

        # 1. Incomplete payload must raise IncompleteActivityError and clean up .tmp
        resp_bad = MagicMock()
        resp_bad.ok = True
        resp_bad.iter_content.return_value = [b"\x00" * 1533]
        mock_get.return_value = resp_bad

        with self.assertRaises(IncompleteActivityError):
            client.download_file("bucket", "key", dest_path)

        self.assertFalse(os.path.exists(dest_path))
        self.assertFalse(os.path.exists(dest_path + ".tmp"))

        # 2. Valid FIT payload must succeed and create dest_path
        resp_good = MagicMock()
        resp_good.ok = True
        resp_good.iter_content.return_value = [build_synthetic_fit()]
        mock_get.return_value = resp_good

        self.assertTrue(client.download_file("bucket", "key", dest_path))
        self.assertTrue(os.path.exists(dest_path))
        self.assertFalse(os.path.exists(dest_path + ".tmp"))

    def test_is_activity_incomplete_metadata(self):
        self.assertTrue(is_activity_incomplete_metadata({"inProgress": True})[0])
        self.assertTrue(
            is_activity_incomplete_metadata(
                {"movingTimeInMs": 0, "distanceInMeters": 0}
            )[0]
        )
        self.assertTrue(
            is_activity_incomplete_metadata(
                {"movingTimeInMs": None, "distanceInMeters": None}
            )[0]
        )
        self.assertFalse(
            is_activity_incomplete_metadata(
                {"inProgress": False, "movingTimeInMs": 600000, "distanceInMeters": 0}
            )[0]
        )
        self.assertFalse(
            is_activity_incomplete_metadata(
                {"inProgress": False, "movingTimeInMs": 600000, "distanceInMeters": 4500.0}
            )[0]
        )

    def test_cleanup_incomplete_downloads(self):
        bad_fn = "2026-10-27_activity_2258415478529654800.fit"
        good_fn = "2026-10-27_activity_2258418450361794592.fit"
        bad_path = os.path.join(self.downloads_dir, bad_fn)
        good_path = os.path.join(self.downloads_dir, good_fn)

        with open(bad_path, "wb") as f:
            f.write(b"\x00" * 1533)
        with open(good_path, "wb") as f:
            f.write(build_synthetic_fit())

        downloaded_map = {
            "2258415478529654800": {
                "id": "2258415478529654800",
                "filename": bad_fn,
            },
            "2258418450361794592": {
                "id": "2258418450361794592",
                "filename": good_fn,
            },
        }

        removed = cleanup_incomplete_downloads(self.downloads_dir, downloaded_map)
        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(bad_path))
        self.assertTrue(os.path.exists(good_path))
        self.assertNotIn("2258415478529654800", downloaded_map)
        self.assertIn("2258418450361794592", downloaded_map)

    @patch("app.sync.ZwiftClient")
    def test_perform_sync_end_to_end_guards_and_recovery(self, mock_client_cls):
        # Seed a valid token
        with open(os.path.join(self.config_dir, "tokens.json"), "w") as f:
            json.dump(
                {
                    "access_token": "tok",
                    "refresh_token": "ref",
                    "profile_id": "999",
                    "expires_at": 9999999999,
                },
                f,
            )

        # Seed a previously downloaded defective file on disk + in history
        stale_id = "2258415478529654800"
        stale_fn = f"2026-10-27_activity_{stale_id}.fit"
        stale_path = os.path.join(self.downloads_dir, stale_fn)
        with open(stale_path, "wb") as f:
            f.write(b"\x00" * 1533)

        with open(os.path.join(self.config_dir, "sync_history.json"), "w") as f:
            json.dump(
                {
                    "downloaded": {
                        stale_id: {
                            "id": stale_id,
                            "starts": "2026-10-27T08:00:00Z",
                            "filename": stale_fn,
                        }
                    },
                    "last_sync": None,
                },
                f,
            )

        client = MagicMock()
        mock_client_cls.return_value = client

        # First sync run:
        # - Activity 101: incomplete metadata (inProgress=True) -> skipped
        # - Activity 102: 404 from details -> skipped
        # - Activity stale_id: passes metadata, but download_file raises IncompleteActivityError -> skipped, not marked downloaded
        # - Activity 103: valid activity -> downloaded and recorded
        client.fetch_activities.return_value = [
            {
                "id_str": "101",
                "startDate": "2026-10-27T09:00:00Z",
                "inProgress": True,
                "movingTimeInMs": 0,
                "distanceInMeters": 0,
            },
            {
                "id_str": "102",
                "startDate": "2026-10-27T08:30:00Z",
                "inProgress": False,
                "movingTimeInMs": 600000,
                "distanceInMeters": 3000,
            },
            {
                "id_str": stale_id,
                "startDate": "2026-10-27T08:00:00Z",
                "inProgress": False,
                "movingTimeInMs": 600000,
                "distanceInMeters": 3000,
            },
            {
                "id_str": "103",
                "startDate": "2026-10-26T08:00:00Z",
                "inProgress": False,
                "movingTimeInMs": 1200000,
                "distanceInMeters": 8000,
            },
        ]

        resp_404 = MagicMock()
        resp_404.status_code = 404
        err_404 = requests.HTTPError("404 Not Found")
        err_404.response = resp_404

        def fake_details(_token, act_id):
            if act_id == "102":
                raise err_404
            return {
                "fitFileBucket": "bucket",
                "fitFileKey": f"key/{act_id}.fit",
                "inProgress": False,
                "movingTimeInMs": 600000,
                "distanceInMeters": 3000,
            }

        client.fetch_activity_details.side_effect = fake_details

        def fake_download_run1(_bucket, key, dest_path):
            if stale_id in key:
                raise IncompleteActivityError("Incomplete FIT file (0 records)")
            with open(dest_path, "wb") as f:
                f.write(build_synthetic_fit())
            return True

        client.download_file.side_effect = fake_download_run1

        res1 = perform_sync("all_time")
        self.assertEqual(res1["status"], "success")
        self.assertEqual(res1["new_downloads"], 1)
        self.assertEqual(res1["skipped"], 3)
        self.assertEqual(res1["errors"], [])

        with open(os.path.join(self.config_dir, "sync_history.json"), "r") as f:
            hist1 = json.load(f)
        self.assertNotIn(stale_id, hist1["downloaded"])
        self.assertNotIn("101", hist1["downloaded"])
        self.assertNotIn("102", hist1["downloaded"])
        self.assertIn("103", hist1["downloaded"])

        # Second sync run: stale_id is now complete on Zwift!
        def fake_download_run2(_bucket, _key, dest_path):
            with open(dest_path, "wb") as f:
                f.write(build_synthetic_fit())
            return True

        client.download_file.side_effect = fake_download_run2
        res2 = perform_sync("all_time")
        self.assertEqual(res2["status"], "success")
        self.assertEqual(res2["new_downloads"], 1)

        with open(os.path.join(self.config_dir, "sync_history.json"), "r") as f:
            hist2 = json.load(f)
        self.assertIn(stale_id, hist2["downloaded"])
        self.assertIn("103", hist2["downloaded"])


if __name__ == "__main__":
    unittest.main()
