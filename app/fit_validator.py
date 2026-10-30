import logging
import os
import struct

logger = logging.getLogger("zwift_connector.fit_validator")

MIN_FIT_FILE_SIZE_BYTES = int(os.getenv("MIN_FIT_FILE_SIZE_BYTES", "4096"))
MIN_FIT_RECORDS = int(os.getenv("MIN_FIT_RECORDS", "30"))

MESG_NUM_SESSION = 18
MESG_NUM_RECORD = 20

SESSION_FIELD_START_TIME = 2
SESSION_FIELD_TOTAL_TIMER_TIME = 8
SESSION_FIELD_TOTAL_DISTANCE = 9

UINT32_INVALID = 0xFFFFFFFF

_FIT_CRC_TABLE = (
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
)


def _compute_fit_crc(data: bytes | memoryview) -> int:
    crc = 0
    for b in data:
        tmp = _FIT_CRC_TABLE[crc & 0x0F]
        crc = ((crc >> 4) & 0x0FFF) ^ tmp ^ _FIT_CRC_TABLE[b & 0x0F]
        tmp = _FIT_CRC_TABLE[crc & 0x0F]
        crc = ((crc >> 4) & 0x0FFF) ^ tmp ^ _FIT_CRC_TABLE[(b >> 4) & 0x0F]
    return crc


class IncompleteActivityError(Exception):
    """Raised when a downloaded or inspected Zwift activity is incomplete."""


def validate_fit_file(
    file_path: str,
    min_records: int | None = None,
    min_size_bytes: int | None = None,
    expected_timer_ms: int | float | None = None,
    expected_distance_m: int | float | None = None,
) -> tuple[bool, str]:
    """
    Validate a binary .fit file to ensure it represents a complete cycling activity.
    Optionally cross-validates session metrics against expected API metrics.
    Returns (is_valid, reason).
    """
    if min_records is None:
        min_records = int(os.getenv("MIN_FIT_RECORDS", str(MIN_FIT_RECORDS)))
    if min_size_bytes is None:
        min_size_bytes = int(os.getenv("MIN_FIT_FILE_SIZE_BYTES", str(MIN_FIT_FILE_SIZE_BYTES)))

    try:
        if not os.path.exists(file_path):
            return False, "file does not exist"

        file_size = os.path.getsize(file_path)
        if file_size < min_size_bytes:
            return (
                False,
                f"file size {file_size} B is below minimum threshold ({min_size_bytes} B)",
            )

        with open(file_path, "rb") as f:
            data = f.read()

        if len(data) < 14:
            return False, f"file too short for FIT header ({len(data)} B)"

        header_size = data[0]
        if header_size not in (12, 14) or len(data) < header_size:
            return False, f"invalid FIT header size ({header_size})"

        if data[8:12] != b".FIT":
            return False, "missing .FIT header signature"

        if header_size == 14:
            hdr_crc = struct.unpack_from("<H", data, 12)[0]
            if hdr_crc != 0 and _compute_fit_crc(memoryview(data)[:12]) != hdr_crc:
                return False, "FIT header CRC mismatch"

        data_size = struct.unpack_from("<I", data, 4)[0]
        if data_size <= 0:
            return False, f"invalid FIT data_size ({data_size})"

        end_offset = header_size + data_size
        if len(data) < end_offset + 2:
            return (
                False,
                f"truncated FIT payload (expected {end_offset + 2} B, got {len(data)} B)",
            )

        expected_crc = struct.unpack_from("<H", data, end_offset)[0]
        if expected_crc != 0:
            crc_start = 14 if (header_size == 14 and struct.unpack_from("<H", data, 12)[0] != 0) else 0
            if _compute_fit_crc(memoryview(data)[crc_start:end_offset]) != expected_crc:
                return False, "FIT file CRC mismatch"

        offset = header_size
        definitions: dict[int, dict] = {}
        record_count = 0
        session_found = False
        session_start_time = None
        session_total_timer_time = None
        session_total_distance = None

        while offset < end_offset:
            hdr = data[offset]
            offset += 1

            if (hdr & 0x80) != 0:
                # Compressed timestamp header
                local_mesg_num = (hdr >> 5) & 0x03
                is_definition = False
                has_dev_data = False
            else:
                is_definition = (hdr & 0x40) != 0
                has_dev_data = (hdr & 0x20) != 0
                local_mesg_num = hdr & 0x0F

            if is_definition:
                if offset + 5 > end_offset:
                    return False, "truncated FIT definition header"

                arch = data[offset + 1]
                endian = ">" if arch == 1 else "<"
                global_mesg_num = struct.unpack_from(f"{endian}H", data, offset + 2)[0]
                num_fields = data[offset + 4]
                offset += 5

                if offset + (num_fields * 3) > end_offset:
                    return False, "truncated FIT field definitions"

                fields = []
                total_size = 0
                for _ in range(num_fields):
                    f_num = data[offset]
                    f_size = data[offset + 1]
                    f_type = data[offset + 2]
                    fields.append((f_num, f_size, f_type))
                    total_size += f_size
                    offset += 3

                if has_dev_data:
                    if offset + 1 > end_offset:
                        return False, "truncated FIT developer data count"
                    num_dev_fields = data[offset]
                    offset += 1
                    if offset + (num_dev_fields * 3) > end_offset:
                        return False, "truncated FIT developer field definitions"
                    for _ in range(num_dev_fields):
                        dev_size = data[offset + 1]
                        total_size += dev_size
                        offset += 3

                definitions[local_mesg_num] = {
                    "endian": endian,
                    "global_mesg_num": global_mesg_num,
                    "fields": fields,
                    "total_size": total_size,
                }
                continue

            defn = definitions.get(local_mesg_num)
            if defn is None:
                return False, f"missing definition for local message {local_mesg_num}"

            msg_size = defn["total_size"]
            if offset + msg_size > end_offset:
                return False, "truncated FIT data message"

            global_mesg_num = defn["global_mesg_num"]
            if global_mesg_num == MESG_NUM_RECORD:
                record_count += 1
            elif global_mesg_num == MESG_NUM_SESSION:
                session_found = True
                endian = defn["endian"]
                f_offset = offset
                for f_num, f_size, _ in defn["fields"]:
                    if f_num in (
                        SESSION_FIELD_START_TIME,
                        SESSION_FIELD_TOTAL_TIMER_TIME,
                        SESSION_FIELD_TOTAL_DISTANCE,
                    ) and f_size == 4:
                        val = struct.unpack_from(f"{endian}I", data, f_offset)[0]
                        if f_num == SESSION_FIELD_START_TIME:
                            session_start_time = val
                        elif f_num == SESSION_FIELD_TOTAL_TIMER_TIME:
                            session_total_timer_time = val
                        elif f_num == SESSION_FIELD_TOTAL_DISTANCE:
                            session_total_distance = val
                    f_offset += f_size

            offset += msg_size

        if record_count < min_records:
            return (
                False,
                f"insufficient data records ({record_count} < {min_records})",
            )

        if not session_found:
            return False, "missing FIT session summary message"

        if session_start_time is None or session_start_time == UINT32_INVALID:
            return False, f"session start_time is invalid ({session_start_time})"

        if (
            session_total_timer_time is None
            or session_total_timer_time == UINT32_INVALID
            or session_total_timer_time <= 0
        ):
            return (
                False,
                f"invalid session total_timer_time ({session_total_timer_time})",
            )

        if session_total_distance == UINT32_INVALID:
            session_total_distance = None

        if expected_timer_ms is not None:
            try:
                exp_timer = float(expected_timer_ms)
                if exp_timer > 60000:
                    min_allowed_timer = exp_timer * 0.95
                    if session_total_timer_time < min_allowed_timer:
                        return (
                            False,
                            f"FIT session timer ({session_total_timer_time} ms) is below 95% of API moving time ({int(exp_timer)} ms, min {int(min_allowed_timer)} ms)",
                        )
            except (ValueError, TypeError):
                pass

        if expected_distance_m is not None:
            try:
                exp_dist = float(expected_distance_m)
                if exp_dist > 500:
                    exp_dist_cm = exp_dist * 100.0
                    min_allowed_dist = exp_dist_cm * 0.95
                    if (
                        session_total_distance is None
                        or session_total_distance < min_allowed_dist
                    ):
                        return (
                            False,
                            f"FIT session distance ({session_total_distance} cm) is below 95% of API distance ({int(exp_dist)} m, min {int(min_allowed_dist)} cm)",
                        )
            except (ValueError, TypeError):
                pass

        return (
            True,
            f"valid FIT file ({file_size} B, {record_count} records, timer={session_total_timer_time} ms, dist={session_total_distance})",
        )

    except (OSError, struct.error, IndexError, KeyError, ValueError) as e:
        return False, f"corrupt FIT file ({e})"
