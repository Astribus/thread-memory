"""Time zone choices built from zone.tab, labelled like "Kolkata · India (UTC+5:30)".

Legacy aliases are excluded. The IANA name is what's stored; the offset is display-only.
"""

import zoneinfo
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REGIONS = ["Africa", "America", "Antarctica", "Arctic", "Asia", "Atlantic", "Australia", "Europe", "Indian",
           "Pacific"]
REGION_NAMES = {"America": "Americas", "Indian": "Indian Ocean", "Atlantic": "Atlantic Ocean",
                "Pacific": "Pacific Ocean", "Arctic": "Arctic"}

# Legacy names some browsers still report, mapped to current IANA names.
OLD_NAMES = {
    "Asia/Calcutta": "Asia/Kolkata", "Asia/Saigon": "Asia/Ho_Chi_Minh", "Asia/Katmandu": "Asia/Kathmandu",
    "Asia/Rangoon": "Asia/Yangon", "Asia/Dacca": "Asia/Dhaka", "Asia/Thimbu": "Asia/Thimphu",
    "Asia/Ulan_Bator": "Asia/Ulaanbaatar", "Asia/Macao": "Asia/Macau", "Asia/Ashkhabad": "Asia/Ashgabat",
    "Asia/Chongqing": "Asia/Shanghai", "Asia/Harbin": "Asia/Shanghai", "Asia/Tel_Aviv": "Asia/Jerusalem",
    "Asia/Istanbul": "Europe/Istanbul", "Europe/Kiev": "Europe/Kyiv", "Atlantic/Faeroe": "Atlantic/Faroe",
    "America/Buenos_Aires": "America/Argentina/Buenos_Aires", "America/Godthab": "America/Nuuk",
    "America/Indianapolis": "America/Indiana/Indianapolis", "America/Louisville": "America/Kentucky/Louisville",
    "America/Montreal": "America/Toronto", "Pacific/Ponape": "Pacific/Pohnpei", "Pacific/Truk": "Pacific/Chuuk",
    "Pacific/Samoa": "Pacific/Pago_Pago", "Australia/Canberra": "Australia/Sydney",
    "Australia/ACT": "Australia/Sydney", "Australia/NSW": "Australia/Sydney",
    "US/Eastern": "America/New_York", "US/Central": "America/Chicago", "US/Mountain": "America/Denver",
    "US/Pacific": "America/Los_Angeles", "Etc/UTC": "UTC", "Etc/GMT": "UTC", "GMT": "UTC", "UCT": "UTC",
}


def _tables() -> tuple[Path | None, Path | None]:
    """zone.tab and iso3166.tab from the system's time zone data (macOS and the Debian server both have them)."""
    for base in [*zoneinfo.TZPATH, "/usr/share/zoneinfo", "/usr/share/lib/zoneinfo"]:
        zone = Path(base) / "zone.tab"
        if zone.exists():
            iso = Path(base) / "iso3166.tab"
            return zone, iso if iso.exists() else None
    return None, None


@lru_cache(maxsize=1)
def _places() -> dict[str, str]:
    """IANA name -> country name, for every real place zone."""
    available = zoneinfo.available_timezones()
    zone_tab, iso_tab = _tables()
    countries = {}
    if iso_tab:
        for line in iso_tab.read_text().splitlines():
            if line and not line.startswith("#"):
                code, _, name = line.partition("\t")
                countries[code] = name.strip()
    places = {}
    if zone_tab:
        for line in zone_tab.read_text().splitlines():
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 3 and parts[2] in available:
                places[parts[2]] = countries.get(parts[0], parts[0])
    else:   # no zone.tab: fall back to Region/City names
        for name in available:
            if name.split("/")[0] in REGIONS and name not in OLD_NAMES:
                places[name] = ""
    return places


def offset_text(name: str, when: datetime | None = None) -> str:
    """'UTC−4', 'UTC+5:30', 'UTC' for the zone right now (uses a real minus sign)."""
    off = (when or datetime.now(timezone.utc)).astimezone(ZoneInfo(name)).utcoffset() or timedelta(0)
    minutes = int(off.total_seconds() // 60)
    if minutes == 0:
        return "UTC"
    sign = "+" if minutes > 0 else "−"
    h, m = divmod(abs(minutes), 60)
    return f"UTC{sign}{h}" + (f":{m:02d}" if m else "")


def city(name: str) -> str:
    """'America/Argentina/Buenos_Aires' -> 'Buenos Aires'."""
    return name.split("/")[-1].replace("_", " ")


def label(name: str, when: datetime | None = None) -> str:
    """What a person sees: 'Kolkata · India (UTC+5:30)'. Unknown or empty names come back unchanged."""
    if name == "UTC":
        return "UTC, no daylight saving (UTC)"
    try:
        country = _places().get(name, "")
        return f"{city(name)}{' · ' + country if country else ''} ({offset_text(name, when)})"
    except (ZoneInfoNotFoundError, ValueError):
        return name


def canonical(name: str) -> str | None:
    """Today's name for a zone, or None if it isn't a real zone. Accepts old aliases ('Asia/Calcutta')."""
    name = (name or "").strip()
    if not name:
        return None
    name = OLD_NAMES.get(name, name)
    if name in _places() or name == "UTC":
        return name
    try:
        ZoneInfo(name)   # valid zone missing from the table
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name if "/" in name else None   # skip bare legacy names (EST, PRC, Japan)


@lru_cache(maxsize=1)
def choices() -> list[dict]:
    """[{region, zones: [{id, label}]}] for the picker, regions in a fixed order, cities A–Z."""
    groups: dict[str, list] = {r: [] for r in REGIONS}
    for name in _places():
        region = name.split("/")[0]
        if region in groups:
            groups[region].append({"id": name, "label": label(name)})
    out = [{"region": REGION_NAMES.get(r, r), "zones": sorted(z, key=lambda x: x["label"].lower())}
           for r, z in groups.items() if z]
    out.append({"region": "Other", "zones": [{"id": "UTC", "label": label("UTC")}]})
    return out


def same_clock(a: str, b: str) -> bool:
    """True if both zones have the same offset now and in six months (covers DST)."""
    try:
        now = datetime.now(timezone.utc)
        za, zb = ZoneInfo(a), ZoneInfo(b)
        return all((now + d).astimezone(za).utcoffset() == (now + d).astimezone(zb).utcoffset()
                   for d in (timedelta(0), timedelta(days=182)))
    except (ZoneInfoNotFoundError, ValueError):
        return False


def options_html(selected: str = "") -> str:
    """<optgroup>/<option> markup for server-rendered pages (the sign-up form)."""
    from html import escape
    parts = ['<option value="" disabled' + ("" if selected else " selected") + '>Choose your time zone</option>']
    for group in choices():
        parts.append(f'<optgroup label="{escape(group["region"])}">')
        for z in group["zones"]:
            sel = " selected" if z["id"] == selected else ""
            parts.append(f'<option value="{escape(z["id"])}"{sel}>{escape(z["label"])}</option>')
        parts.append("</optgroup>")
    return "".join(parts)


