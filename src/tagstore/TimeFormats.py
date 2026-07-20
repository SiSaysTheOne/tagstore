from datetime import datetime as dt

time_formats = {
    "YYYYMMDD_HHMMSS": "%Y%m%d_%H%M%S",
    "YYYY-MM-DD_HH-MM-SS": "%Y-%m-%d_%H-%M-%S"
}

def get_time(entry) -> dt | None:
    stem = entry.path.stem
    for tf_filename, tf_str in time_formats.items():
        try:
            return dt.strptime(stem[:len(tf_str)], tf_str)
        except ValueError:
            continue

    return None

def time_format(entry) -> str | None:
    pass