import json
import re
from urllib.parse import urljoin, urlparse

from src.utils import clean_website_url, get_place_stable_id


def extract_app_state(html: str) -> list | None:
    for p in [
        r'window\.APP_INITIALIZATION_STATE\s*=\s*(\[[\s\S]*?\]);\s*</script>',
        r'window\.APP_INITIALIZATION_STATE\s*=\s*(\[[\s\S]*?\]);',
        r'window\.APP_INITIALIZATION_STATE\s*=\s*(\[[\s\S]*?\])\s*\n',
    ]:
        m = re.search(p, html)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
    return None


def extract_place_from_array(arr: list) -> dict | None:
    if not isinstance(arr, list) or len(arr) < 3:
        return None

    result = {}
    arr_str = json.dumps(arr)

    r = {}
    r["title"] = _extract_title(arr, arr_str)
    r["address"] = _extract_address(arr, arr_str)
    r["phone"] = _extract_phone(arr_str)
    r["website"] = _extract_website(arr, arr_str)
    r["totalScore"] = _extract_rating(arr)
    r["reviewsCount"] = _extract_reviews(arr)
    r["categoryName"] = _extract_category(arr_str)
    r["location"] = _extract_coords(arr, arr_str)
    r["placeId"] = _extract_place_id(arr, arr_str)
    r["url"] = _extract_url(arr, arr_str)
    r["isClaimed"] = _extract_is_claimed(arr, arr_str)

    if r["website"]:
        r["website"] = clean_website_url(r["website"])

    if r.get("title"):
        return r
    return None


def _extract_title(arr: list, arr_str: str) -> str | None:
    for p in [r'"title":\s*"([^"]+)"', r'"name":\s*"([^"]+)"']:
        m = re.search(p, arr_str)
        if m:
            return m.group(1)

    for i, item in enumerate(arr):
        if isinstance(item, str) and len(item) > 3 and not item.startswith("http"):
            if re.match(r"^[\w\s&'.\-,/#()]+$", item):
                if i > 0 and isinstance(arr[i - 1], (int, float)):
                    continue
                if i > 0 and isinstance(arr[i - 1], str) and arr[i - 1].startswith("CID"):
                    continue
                if i > 0 and isinstance(arr[i - 1], str) and re.match(r"^0x", arr[i - 1]):
                    continue
                return item
    return None


def _extract_address(arr: list, arr_str: str) -> str | None:
    m = re.search(r'"address":\s*"([^"]+)"', arr_str)
    if m:
        return m.group(1)

    addr_candidates = []
    for i, item in enumerate(arr):
        if isinstance(item, str) and re.match(r"^[\d\w]", item):
            if "," in item and len(item) > 10:
                if i > 0 and isinstance(arr[i - 1], str) and "CID" in arr[i - 1]:
                    continue
                addr_candidates.append(item)

    state_keys = {"usa": "USA", "united states": "USA", "uk": "UK",
                  "canada": "Canada", "australia": "Australia"}

    for c in addr_candidates:
        parts = [p.strip() for p in c.split(",")]
        if len(parts) >= 2:
            for p in parts:
                for k, v in state_keys.items():
                    if p.lower().strip() == k:
                        return c
                    if len(parts) >= 3:
                        return c

    if addr_candidates:
        return addr_candidates[-1]

    return None


def _extract_phone(arr_str: str) -> str | None:
    m = re.search(r'"phone":\s*"([^"]+)"', arr_str)
    if m:
        return m.group(1)

    phone_pats = [
        r'"\+?1?\s*\(?\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4}"',
        r'"\d{2,3}[\s.-]\d{3}[\s.-]\d{4}"',
    ]

    vals = re.findall(r':\s*"([^"]+)"', arr_str)
    for v in vals:
        digits = re.sub(r"\D", "", v)
        if 10 <= len(digits) <= 15 and len(v) <= 20:
            return v
    return None


def _extract_website(arr: list, arr_str: str) -> str | None:
    m = re.search(r'"website":\s*"([^"]+)"', arr_str)
    if m:
        return m.group(1)

    for url in re.findall(r'"https?://[^"]+"', arr_str):
        u = url.strip('"')
        if "google.com/maps" not in u and "google.com/search" not in u:
            return u

    for i, item in enumerate(arr):
        if isinstance(item, str) and "google.com" not in item:
            if re.match(r"^https?://", item):
                return item
    return None


def _extract_rating(arr: list) -> float | None:
    for _, item in enumerate(arr):
        if isinstance(item, (int, float)) and 1.0 <= item <= 5.0:
            return float(item)
    return None


def _extract_reviews(arr: list) -> int | None:
    for _, item in enumerate(arr):
        if isinstance(item, int) and 5 < item < 100000:
            return item
    return None


def _extract_category(arr_str: str) -> str | None:
    m = re.search(r'"category":\s*"([^"]+)"', arr_str)
    if m:
        return m.group(1)

    m = re.search(r'"categoryName":\s*"([^"]+)"', arr_str)
    if m:
        return m.group(1)
    return None


def _extract_coords(arr: list, arr_str: str) -> dict | None:
    for p in [r'"lat":\s*([\d.-]+),\s*"lng":\s*([\d.-]+)',
              r'"latitude":\s*([\d.-]+),\s*"longitude":\s*([\d.-]+)']:
        m = re.search(p, arr_str)
        if m:
            lat, lng = float(m.group(1)), float(m.group(2))
            if -90 <= lat <= 90 and -180 <= lng <= 180:
                return {"lat": lat, "lng": lng}

    def walk_coords(obj):
        if isinstance(obj, list):
            if len(obj) == 2 and all(isinstance(x, (int, float)) for x in obj):
                if -90 <= obj[0] <= 90 and -180 <= obj[1] <= 180:
                    return obj
            for item in obj:
                r = walk_coords(item)
                if r:
                    return r
        return None

    found = walk_coords(arr)
    if found:
        return {"lat": found[0], "lng": found[1]}
    return None


def _extract_place_id(arr: list, arr_str: str) -> str | None:
    m = re.search(r'"place_id":\s*"([^"]+)"', arr_str)
    if m:
        return m.group(1)

    m = re.search(r'"placeId":\s*"([^"]+)"', arr_str)
    if m:
        return m.group(1)

    for item in arr:
        if isinstance(item, str) and item.startswith("0x") and len(item) > 10:
            return item

    return None


def _extract_url(arr: list, arr_str: str) -> str | None:
    for item in arr:
        if isinstance(item, str) and "google.com/maps/place" in item:
            return item
    return None


def parse_hours(html: str) -> list[dict] | None:
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    day_re = re.compile(r"({})\s*:\s*(\d{{1,2}}:\d{{2}}\s*(?:AM|PM|am|pm)?)\s*(?:[–\-to])\s*(\d{{1,2}}:\d{{2}}\s*(?:AM|PM|am|pm)?)"
                        .format("|".join(days[:3] + [d[:3] for d in days])), re.IGNORECASE)
    matches = day_re.findall(html)
    if matches:
        result = []
        for m in matches:
            day_name = m[0].capitalize()
            if len(day_name) == 3:
                full = {d[:3].lower(): d for d in days}
                day_name = full.get(day_name.lower(), day_name)
            result.append({
                "day": day_name,
                "hours": f"{m[1]} - {m[2]}",
            })
        return result

    alt = re.findall(r'(\w+day)\s+(\d{1,2}:\d{2}[APM\s]*)\s*[-–]\s*(\d{1,2}:\d{2}[APM\s]*)',
                     html, re.IGNORECASE)
    if alt:
        return [{"day": m[0].capitalize(), "hours": f"{m[1]} - {m[2]}"} for m in alt]

    return None


def _extract_is_claimed(arr: list, arr_str: str) -> bool | None:
    for p in [r'"isClaimed":\s*(true|false)', r'"claimed":\s*(true|false)']:
        m = re.search(p, arr_str)
        if m:
            return m.group(1) == 'true'

    for item in arr:
        if item is True:
            return True
        if item is False:
            return False

    return None
