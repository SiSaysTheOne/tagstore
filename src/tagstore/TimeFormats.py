import re
from datetime import datetime as dt

# Important that this is an ordered dict to parse for formats with the most datetime information first
time_formats = {
    "YYYYMMDD_HHMMSS": "%Y%m%d_%H%M%S",
    "YYYY-MM-DD_HH-MM-SS": "%Y-%m-%d_%H-%M-%S",
    "YYYY-MM-DD HHMMSS": "%Y-%m-%d %H%M%S",
    "YYYYMMDD-HHMMSS": "%Y%m%d-%H%M%S",
    "YYYY-MM-DD-HH-MM-SS": "%Y-%m-%d-%H-%M-%S",
    "YYYYMMDDHHMMSS": "%Y%m%d%H%M%S",
    "YYYY-MM-DD HH:MM:SS": "%Y-%m-%d %H:%M:%S",
    "YYYY-MM-DD_at_HH.MM.SS_AMPM": "%Y-%m-%d_at_%I.%M.%S_%p",
    "YYYYMMDD:": "%Y%m%d",
    "YYMMDD:": "%y%m%d",
}

def get_unix_timestamp(stem: str) -> dt | None:
    for match in re.finditer(r"\d+", stem):
        try:
            return dt.fromtimestamp(int(match.group()))
        except (ValueError, OSError, OverflowError):
            continue

    return None

def get_time(stem: str) -> dt | None:
    ampm_suffix = ""
    ampm_match = re.search(r"[^0-9A-Za-z]?(AM|PM)$", stem, re.IGNORECASE)
    if ampm_match:
        ampm_suffix = stem[ampm_match.start():]
        stem = stem[:ampm_match.start()]

    digits = re.sub(r"^\D+|\D+$", "", stem)
    timestamp = digits + ampm_suffix

    for tf_filename, tf_str in time_formats.items():
        rendered_length = len(dt(2000, 1, 1).strftime(tf_str))
        # strftime always zero-pads, so rendered_length is the max width. %I is the
        # one field in our formats that real filenames may render unpadded (single
        # digit hour), so also allow one character short for formats that use it.
        candidate_lengths = (rendered_length - 1, rendered_length) if "%I" in tf_str else (rendered_length,)

        for candidate_length in candidate_lengths:
            if len(timestamp) < candidate_length:
                continue
            try:
                return dt.strptime(timestamp[:candidate_length], tf_str)
            except ValueError:
                continue

    unix_time = get_unix_timestamp(timestamp)
    if unix_time is not None:
        return unix_time

    return None

def time_format(entry) -> str | None:
    pass