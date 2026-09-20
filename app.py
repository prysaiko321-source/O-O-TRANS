import os
import json
import math
import hmac
import secrets
import base64
import time
import threading
from datetime import datetime, timezone, timedelta
from html import escape
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    Response,
    request,
    redirect,
    url_for,
    session,
    jsonify
)
import requests

from company_logo import COMPANY_LOGO_BASE64
from branding import (
    get_company_branding,
    get_company_logo,
    register_branding_routes
)
from i18n import (
    LANGUAGES,
    translate,
    translate_title
)

app = Flask(__name__)

SESSION_SECRET = os.environ.get("SESSION_SECRET", "").strip()

if not SESSION_SECRET:
    # Безпечний тимчасовий ключ. Після перезапуску Render сесії
    # завершаться, але застосунок не працюватиме зі стандартним паролем.
    SESSION_SECRET = secrets.token_hex(32)

app.secret_key = SESSION_SECRET

NAVIREC_API = "https://api.navirec.com"
NAVIREC_TOKEN = os.environ.get("NAVIREC_TOKEN", "")
NAVIREC_ACCOUNT_ID = os.environ.get(
    "NAVIREC_ACCOUNT_ID",
    "5c980074-7a71-4c9b-b5a8-a7c45163adf5"
)
GOOGLE_MAPS_API_KEY = os.environ.get(
    "GOOGLE_MAPS_API_KEY",
    ""
).strip()

COMPANY_NAME = os.environ.get("COMPANY_NAME", "O&O TRANS")
COMPANY_ID = os.environ.get("COMPANY_ID", "O&O-TRANS")
PLATFORM_NAME = "TRANVIQ"
PLATFORM_TAGLINE = "Transport Intelligence Platform"
DEFAULT_LANGUAGE = os.environ.get(
    "DEFAULT_LANGUAGE",
    "uk"
).strip().lower()

if DEFAULT_LANGUAGE not in LANGUAGES:
    DEFAULT_LANGUAGE = "uk"
ADMIN_USER = os.environ.get("ADMIN_USER", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
DISPATCHER_USER = os.environ.get("DISPATCHER_USER", "")
DISPATCHER_PASSWORD = os.environ.get("DISPATCHER_PASSWORD", "")
DRIVER_USER = os.environ.get("DRIVER_USER", "")
DRIVER_PASSWORD = os.environ.get("DRIVER_PASSWORD", "")
POLAND_TZ = ZoneInfo("Europe/Warsaw")

ROLE_LABELS = {
    "director": "Директор",
    "dispatcher": "Логіст",
    "driver": "Водій"
}


def current_language():
    language = session.get(
        "language",
        DEFAULT_LANGUAGE
    )
    if language not in LANGUAGES:
        return DEFAULT_LANGUAGE
    return language


def t(key):
    return translate(current_language(), key)

ROLE_HOME_ENDPOINTS = {
    "director": "home",
    "dispatcher": "dispatcher_dashboard",
    "driver": "driver_dashboard"
}

ROLE_ENDPOINTS = {
    "dispatcher": {
        "dispatcher_dashboard",
        "vehicles",
        "vehicle_page",
        "gps",
        "geocode_search",
        "route_calculate",
        "history",
        "fuel",
        "tachograph",
        "road_payments"
    },
    "driver": {
        "driver_dashboard",
        "road_payments"
    }
}

GEOCODE_CACHE_TTL = 3600
GEOCODE_CACHE = {}
GEOCODE_LOCK = threading.Lock()
GEOCODE_LAST_REQUEST_AT = 0.0
ROUTE_CACHE_TTL = 900
ROUTE_CACHE = {}
VEHICLE_CONSUMPTION_CACHE_TTL = 1800
VEHICLE_CONSUMPTION_CACHE = {}

VEHICLES = [
    {
        "id": "aaaa9acd-5bb5-467e-8241-81444292bbfe",
        "name": "Renault Master SH 9203G"
    },
    {
        "id": "cbb121b6-34dd-41c6-974b-5b7aa3d9a1cb",
        "name": "Renault Master DX 9034F"
    },
    {
        "id": "f016af91-dee6-4e72-9f86-4b2e27a253c1",
        "name": "Renault Master DX 5405A"
    }
]


def is_logged_in():
    return bool(session.get("logged_in"))


def current_role():
    if not is_logged_in():
        return ""

    role = session.get("role")

    if role in ROLE_LABELS:
        return role

    # Сумісність зі старими сесіями директора.
    return "director"


def role_home_url(role=None):
    selected_role = role or current_role() or "director"
    endpoint = ROLE_HOME_ENDPOINTS.get(
        selected_role,
        "home"
    )
    return url_for(endpoint)


def vehicle_by_id(vehicle_id):
    normalized = normalize_vehicle_id(vehicle_id)
    for vehicle in VEHICLES:
        if vehicle["id"] == normalized:
            return vehicle
    return None


def normalize_vehicle_id(value):
    if not value:
        return ""
    value = str(value).strip()
    if "/vehicles/" in value:
        value = value.rstrip("/").split("/vehicles/")[-1]
    return value.rstrip("/")


def normalize_api_id(value):
    if not value:
        return ""

    return str(value).strip().rstrip("/").split("/")[-1]


def extract_coordinates(location):
    if not location:
        return None, None

    if isinstance(location, dict):
        coordinates = location.get("coordinates")

        if isinstance(coordinates, (list, tuple)) and len(coordinates) >= 2:
            try:
                longitude = float(coordinates[0])
                latitude = float(coordinates[1])
                return latitude, longitude
            except (TypeError, ValueError):
                pass

        latitude = location.get("latitude")
        longitude = location.get("longitude")

        try:
            if latitude is not None and longitude is not None:
                return float(latitude), float(longitude)
        except (TypeError, ValueError):
            pass

    if isinstance(location, (list, tuple)) and len(location) >= 2:
        try:
            longitude = float(location[0])
            latitude = float(location[1])
            return latitude, longitude
        except (TypeError, ValueError):
            pass

    return None, None


def navirec_headers():
    return {
        "Authorization": "Token " + NAVIREC_TOKEN,
        "Accept": "application/json; version=1.52.1",
        "Content-Type": "application/json"
    }


def get_activity(item):
    activity = item.get("activity")

    if isinstance(activity, dict):
        return activity.get("name") or activity.get("state") or ""

    return activity or ""


def parse_time(value):
    if not value:
        return None

    text = str(value)

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_time(value):
    dt = parse_time(value)

    if dt is None:
        if not value:
            return "—"
        text = str(value)
        return text.replace("T", " ")[:19]

    if dt.tzinfo is not None:
        dt = dt.astimezone(POLAND_TZ)

    return dt.strftime("%d.%m.%Y %H:%M:%S")


def format_duration(seconds):
    try:
        total_seconds = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        return "—"

    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def format_duration_short(seconds):
    try:
        total_minutes = max(
            0,
            int(round(float(seconds) / 60))
        )
    except (TypeError, ValueError):
        return "—"

    hours, minutes = divmod(total_minutes, 60)
    return f"{hours} год {minutes:02d} хв"


def format_date(value):
    if not value:
        return "—"

    text = str(value)

    try:
        return datetime.fromisoformat(
            text.replace("Z", "+00:00")
        ).strftime("%d.%m.%Y")
    except ValueError:
        return text[:10]


def html_text(value, default="—"):
    if value is None or value == "":
        return default

    return escape(str(value))


def format_number(value, decimals=1):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"

    if decimals == 0:
        return f"{number:,.0f}".replace(",", " ")

    return f"{number:,.{decimals}f}".replace(",", " ")


def safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def haversine_km(first, second):
    lat1 = safe_float(first.get("latitude"))
    lon1 = safe_float(first.get("longitude"))
    lat2 = safe_float(second.get("latitude"))
    lon2 = safe_float(second.get("longitude"))

    if None in (lat1, lon1, lat2, lon2):
        return 0.0

    radius_km = 6371.0088
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    value = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1)
        * math.cos(phi2)
        * math.sin(delta_lambda / 2) ** 2
    )

    return 2 * radius_km * math.atan2(
        math.sqrt(value),
        math.sqrt(max(0.0, 1 - value))
    )


def calculate_history_metrics(points):
    metrics = {
        "distance_km": 0.0,
        "driving_seconds": 0.0,
        "parking_seconds": 0.0,
        "idling_seconds": 0.0
    }

    for index in range(len(points) - 1):
        point = points[index]
        next_point = points[index + 1]

        current_time = parse_time(point.get("time"))
        next_time = parse_time(next_point.get("time"))

        if current_time is None or next_time is None:
            continue

        seconds = (next_time - current_time).total_seconds()

        if seconds <= 0 or seconds > 86400:
            continue

        activity = str(
            point.get("activity") or ""
        ).strip().lower()

        speed = safe_float(point.get("speed")) or 0.0
        next_speed = safe_float(
            next_point.get("speed")
        ) or 0.0

        is_idling = (
            "idling" in activity
            or "idle" in activity
        )
        is_driving = (
            "driving" in activity
            or "moving" in activity
            or speed > 2
        )

        if is_idling:
            metrics["idling_seconds"] += seconds
        elif is_driving:
            metrics["driving_seconds"] += seconds
        else:
            metrics["parking_seconds"] += seconds

        segment_km = haversine_km(point, next_point)

        if (
            segment_km <= 50
            and (
                is_driving
                or speed > 2
                or next_speed > 2
            )
        ):
            metrics["distance_km"] += segment_km

    return metrics


def state_for_vehicle(vehicle_id, states=None):
    normalized = normalize_vehicle_id(vehicle_id)

    if states is None:
        states = get_vehicle_states()

    for state in states:
        candidate = normalize_vehicle_id(
            state.get("vehicle")
            or state.get("vehicle_id")
            or state.get("vehicle_url")
        )

        if candidate == normalized:
            return state

    return None


def state_map_by_vehicle(states):
    result = {}

    for state in states:
        candidate = normalize_vehicle_id(
            state.get("vehicle")
            or state.get("vehicle_id")
            or state.get("vehicle_url")
        )

        if candidate:
            result[candidate] = state

    return result


def get_vehicle_states():
    if not NAVIREC_TOKEN:
        return []

    try:
        url = f"{NAVIREC_API}/last_vehicle_states/"
        params = {"account": NAVIREC_ACCOUNT_ID}

        response = requests.get(
            url,
            headers=navirec_headers(),
            params=params,
            timeout=20
        )

        if response.status_code != 200:
            return []

        data = response.json()

        if isinstance(data, list):
            return data

        return []

    except Exception:
        return []


def navirec_list(endpoint, params=None, timeout=25):
    if not NAVIREC_TOKEN:
        return {
            "ok": False,
            "items": [],
            "error": "NAVIREC_TOKEN не налаштований."
        }

    try:
        response = requests.get(
            f"{NAVIREC_API}/{endpoint.strip('/')}/",
            headers=navirec_headers(),
            params=params or {},
            timeout=timeout
        )

        if response.status_code != 200:
            return {
                "ok": False,
                "items": [],
                "error": (
                    f"Navirec HTTP {response.status_code}: "
                    f"{response.text[:300]}"
                )
            }

        data = response.json()

        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = (
                data.get("results")
                or data.get("items")
                or data.get("data")
                or []
            )
        else:
            items = []

        return {
            "ok": True,
            "items": [
                item
                for item in items
                if isinstance(item, dict)
            ],
            "error": None
        }

    except Exception as exc:
        return {
            "ok": False,
            "items": [],
            "error": f"Помилка Navirec: {exc}"
        }


def get_driver_states_result():
    return navirec_list(
        "last_driver_states",
        {"account": NAVIREC_ACCOUNT_ID}
    )


def get_drivers_result():
    return navirec_list(
        "drivers",
        {
            "account": NAVIREC_ACCOUNT_ID,
            "active": "true",
            "page_size": 500
        }
    )


def get_tachograph_cards_result():
    return navirec_list(
        "tachograph_cards",
        {
            "account": NAVIREC_ACCOUNT_ID,
            "active": "true",
            "page_size": 500
        }
    )


def working_state_label(value):
    states = {
        0: "Відпочинок",
        1: "Готовність",
        2: "Інша робота",
        3: "Керування"
    }

    try:
        code = int(value)
    except (TypeError, ValueError):
        return "Немає даних"

    return states.get(code, f"Невідомий стан ({code})")


def working_state_class(value):
    try:
        code = int(value)
    except (TypeError, ValueError):
        return "badge-muted"

    return {
        0: "badge-rest",
        1: "badge-ready",
        2: "badge-work",
        3: "badge-drive"
    }.get(code, "badge-warning")


def tachograph_time_state_label(value):
    try:
        code = int(value)
    except (TypeError, ValueError):
        return "Немає даних"

    if code == 0:
        return "Без попереджень"

    return f"Попередження тахографа (код {code})"


def get_first_value(data, names):
    if not isinstance(data, dict):
        return None

    for name in names:
        value = data.get(name)

        if value is not None and value != "":
            return value

    return None


def get_duration_value(data, names):
    return safe_float(get_first_value(data, names))


def merge_tachograph_state(vehicle_state, driver_state):
    merged = {}

    if isinstance(vehicle_state, dict):
        merged.update(vehicle_state)

    if isinstance(driver_state, dict):
        for key, value in driver_state.items():
            if value is not None:
                merged[key] = value

    return merged


def state_age_seconds(value):
    dt = parse_time(value)

    if dt is None:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return max(
        0,
        (datetime.now(timezone.utc) - dt).total_seconds()
    )


def get_vehicle_history(vehicle_id, date_string):
    if not NAVIREC_TOKEN:
        return {
            "ok": False,
            "error": "NAVIREC_TOKEN не налаштований.",
            "points": []
        }

    try:
        selected_date = datetime.strptime(
            date_string,
            "%Y-%m-%d"
        ).date()
        start_time = datetime.combine(
            selected_date,
            datetime.min.time(),
            tzinfo=POLAND_TZ
        ).isoformat()
        end_time = datetime.combine(
            selected_date,
            datetime.max.time().replace(microsecond=0),
            tzinfo=POLAND_TZ
        ).isoformat()

        url = f"{NAVIREC_API}/vehicle_history/"

        params = {
            "vehicle": vehicle_id,
            "start_time": start_time,
            "end_time": end_time,
            "format": "json"
        }

        response = requests.get(
            url,
            headers=navirec_headers(),
            params=params,
            timeout=45
        )

        if response.status_code != 200:
            return {
                "ok": False,
                "error": (
                    f"Navirec HTTP {response.status_code}: "
                    f"{response.text[:300]}"
                ),
                "points": []
            }

        data = response.json()

        if not isinstance(data, list):
            return {
                "ok": False,
                "error": "Navirec повернув не список GPS-точок.",
                "points": []
            }

        points = []

        for item in data:
            if not isinstance(item, dict):
                continue

            latitude, longitude = extract_coordinates(
                item.get("location")
            )

            if latitude is None or longitude is None:
                continue

            points.append({
                "id": item.get("id"),
                "time": item.get("time"),
                "latitude": latitude,
                "longitude": longitude,
                "activity": get_activity(item),
                "speed": item.get("speed"),
                "heading": item.get("heading"),
                "fuel_level": item.get("fuel_level"),
                "altitude": item.get("altitude"),
                "engine_speed": item.get("engine_speed"),
                "ignition": item.get("ignition"),
                "driver_name": item.get("driver_name"),
                "driver_surname": item.get("driver_surname"),
                "total_distance": item.get("total_distance"),
                "accumulated_distance": item.get(
                    "accumulated_distance"
                ),
                "accumulated_driving_distance": item.get(
                    "accumulated_driving_distance"
                ),
                "total_engine_time": item.get(
                    "total_engine_time"
                ),
                "satellites": item.get("satellites")
            })

        points.sort(
            key=lambda item: item.get("time") or ""
        )

        return {
            "ok": True,
            "error": None,
            "points": points
        }

    except Exception as exc:
        return {
            "ok": False,
            "error": f"Помилка отримання історії: {exc}",
            "points": []
        }


def get_vehicle_timeline_totals(vehicle_id, date_string):
    if not NAVIREC_TOKEN:
        return None

    try:
        selected_date = datetime.strptime(
            date_string,
            "%Y-%m-%d"
        ).date()
        start_time = datetime.combine(
            selected_date,
            datetime.min.time(),
            tzinfo=POLAND_TZ
        ).isoformat()
        end_time = datetime.combine(
            selected_date,
            datetime.max.time().replace(microsecond=0),
            tzinfo=POLAND_TZ
        ).isoformat()

        url = f"{NAVIREC_API}/vehicle_timeline/totals/"

        params = {
            "vehicle": vehicle_id,
            "start_time": start_time,
            "end_time": end_time
        }

        response = requests.get(
            url,
            headers=navirec_headers(),
            params=params,
            timeout=30
        )

        if response.status_code != 200:
            return None

        return response.json()

    except Exception:
        return None


def get_vehicle_average_consumption(vehicle_id, days=14):
    cached = VEHICLE_CONSUMPTION_CACHE.get(vehicle_id)
    now_monotonic = time.monotonic()

    if (
        cached
        and now_monotonic - cached[0]
        < VEHICLE_CONSUMPTION_CACHE_TTL
    ):
        return cached[1]

    if not NAVIREC_TOKEN:
        return None

    try:
        end_time = datetime.now(POLAND_TZ)
        start_time = end_time - timedelta(days=days)
        response = requests.get(
            f"{NAVIREC_API}/vehicle_timeline/totals/",
            headers=navirec_headers(),
            params={
                "vehicle": vehicle_id,
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat()
            },
            timeout=30
        )

        if response.status_code != 200:
            return None

        totals = response.json()
        consumption = get_total_number(
            totals,
            [
                "fuel_per_100_km",
                "fuelPer100Km",
                "fuel_consumption"
            ]
        )

        if consumption is not None:
            consumption = float(consumption)

        if (
            consumption is None
            or consumption <= 0
            or consumption > 100
        ):
            consumption = None

        VEHICLE_CONSUMPTION_CACHE[vehicle_id] = (
            time.monotonic(),
            consumption
        )
        return consumption
    except Exception:
        return None


def find_value(data, names):
    if not isinstance(data, dict):
        return None

    for name in names:
        if name in data and data[name] is not None:
            return data[name]

    return None


def get_total_number(data, names):
    value = find_value(data, names)

    if value is None:
        return None

    if isinstance(value, dict):
        for key in (
            "value",
            "distance",
            "seconds",
            "hours"
        ):
            if key in value:
                return safe_float(value[key])

    return safe_float(value)


def page(title, body, active=""):
    role = current_role()
    language = current_language()
    visible_title = translate_title(language, title)
    page_class = "page-gps" if active == "gps" else ""
    branding = get_company_branding(
        COMPANY_ID,
        COMPANY_NAME
    )
    company_display_name = escape(
        branding["company_name"]
    )

    if role == "driver":
        nav_items = [
            ("driver", "/driver", t("my_trips")),
            (
                "road_payments",
                "/road-payments",
                "🛣️ Оплата доріг"
            )
        ]
    elif role == "dispatcher":
        nav_items = [
            ("dispatcher", "/dispatcher", t("work_panel")),
            ("vehicles", "/vehicles", t("vehicles")),
            ("gps", "/gps", t("gps")),
            ("history", "/history", t("history")),
            ("fuel", "/fuel", t("fuel")),
            ("tachograph", "/tachograph", t("tachograph")),
            (
                "road_payments",
                "/road-payments",
                "🛣️ Оплата доріг"
            )
        ]
    elif role == "director":
        nav_items = [
            ("home", "/", t("home")),
            ("vehicles", "/vehicles", t("vehicles")),
            ("gps", "/gps", t("gps")),
            ("history", "/history", t("history")),
            ("fuel", "/fuel", t("fuel")),
            ("tachograph", "/tachograph", t("tachograph")),
            ("finance", "/finance", t("finance")),
            (
                "road_payments",
                "/road-payments",
                "🛣️ Оплата доріг"
            ),
            ("branding", "/settings/branding", t("branding")),
            ("health", "/health", t("health"))
        ]
    else:
        nav_items = []

    nav_links = []

    for item_active, href, label in nav_items:
        css_class = "active" if active == item_active else ""
        nav_links.append(
            '<a href="{}" class="{}">{}</a>'.format(
                href,
                css_class,
                label
            )
        )

    if role:
        nav_links.append(
            '<a href="/logout">{}</a>'.format(
                escape(t("logout"))
            )
        )

    nav = '<nav class="nav">{}</nav>'.format(
        "".join(nav_links)
    )

    role_badge = ""

    if role:
        role_badge = (
            '<div class="small" style="margin-top:5px;color:#dfe6e9">'
            '{}: <strong>{}</strong>'
            '</div>'
        ).format(
            escape(t("role")),
            escape(t(role))
        )

    language_options = []
    for language_code, language_name in LANGUAGES.items():
        selected = " selected" if language_code == language else ""
        language_options.append(
            '<option value="{}"{}>{}</option>'.format(
                language_code,
                selected,
                escape(language_name)
            )
        )

    language_picker = """
    <form class="language-picker" method="post" action="/language">
        <input type="hidden" name="next" value="{next_url}">
        <label for="language-select">{language_label}</label>
        <select
            id="language-select"
            name="language"
            onchange="this.form.submit()"
        >
            {language_options}
        </select>
    </form>
    """.format(
        next_url=escape(request.full_path.rstrip("?")),
        language_label=escape(t("language")),
        language_options="".join(language_options)
    )

    return """
<!doctype html>
<html lang="{language}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — {platform} · {company}</title>

<style>
* {{ box-sizing: border-box; }}

body {{
    margin: 0;
    font-family: Arial, sans-serif;
    background: #f1f3f5;
    color: #17202a;
}}

.topbar {{
    background:
        linear-gradient(135deg, #0b1724 0%, #12283c 100%);
    color: white;
    padding: 15px 22px 14px;
    box-shadow: 0 3px 14px rgba(6, 18, 31, .22);
}}

.brand-row {{
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 16px;
}}

.platform-brand-block {{
    min-width: 190px;
}}

.platform-brand {{
    display: inline-flex;
    align-items: center;
    color: white;
    text-decoration: none;
    font-size: 28px;
    font-weight: 900;
    letter-spacing: 1.1px;
    line-height: 1;
}}

.platform-iq {{
    display: inline-block;
    margin-left: 3px;
    padding: 4px 7px 5px;
    border-radius: 8px;
    color: #07131f;
    background: linear-gradient(135deg, #56e6ff, #71ee9f);
    box-shadow: 0 0 18px rgba(86, 230, 255, .38);
    letter-spacing: .5px;
}}

.platform-tagline {{
    margin-top: 5px;
    color: #a9c4d7;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: .7px;
    text-transform: uppercase;
}}

.brand-divider {{
    width: 1px;
    height: 57px;
    background: rgba(255, 255, 255, .18);
}}

.company-brand {{
    display: flex;
    align-items: center;
    gap: 11px;
}}

.company-logo {{
    width: 62px;
    height: 62px;
    object-fit: contain;
    border-radius: 10px;
    padding: 3px;
    background: #f8f5ef;
    box-shadow: 0 2px 9px rgba(0, 0, 0, .24);
}}

.company-caption {{
    color: #91acbf;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: .7px;
    text-transform: uppercase;
}}

.company-name {{
    margin-top: 3px;
    color: white;
    font-size: 17px;
    font-weight: 800;
}}

.language-picker {{
    margin-left: auto;
    min-width: 170px;
}}

.language-picker label {{
    display: block;
    margin-bottom: 4px;
    color: #91acbf;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: .6px;
    text-transform: uppercase;
}}

.language-picker select {{
    min-width: 170px;
    padding: 8px 31px 8px 10px;
    border: 1px solid rgba(255, 255, 255, .24);
    border-radius: 8px;
    background: #18364e;
    color: white;
    cursor: pointer;
}}

.nav {{
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 12px;
}}

.nav a {{
    color: #dfe6e9;
    text-decoration: none;
    padding: 8px 11px;
    border-radius: 7px;
}}

.nav a:hover,
.nav a.active {{
    background: #34495e;
    color: white;
}}

.wrap {{
    position: relative;
    z-index: 1;
    max-width: 1600px;
    margin: 0 auto;
    padding: 22px;
}}

.powered-by {{
    position: relative;
    z-index: 1;
    max-width: 1600px;
    margin: 0 auto;
    padding: 0 22px 22px;
    color: #7b8790;
    font-size: 12px;
    text-align: right;
}}

.powered-by strong {{
    color: #173c55;
    letter-spacing: .5px;
}}

.page-watermark {{
    position: fixed;
    z-index: 0;
    top: 145px;
    right: 0;
    bottom: 0;
    left: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    pointer-events: none;
}}

.page-watermark img {{
    width: min(72vw, 820px);
    max-height: 72vh;
    object-fit: contain;
    opacity: .13;
    mix-blend-mode: multiply;
    filter:
        saturate(1.08)
        contrast(1.02)
        drop-shadow(0 0 32px rgba(31, 148, 180, .22));
}}

.login-choice-grid {{
    position: relative;
    z-index: 1;
}}

.login-choice-grid .card {{
    background: rgba(255, 255, 255, .91);
    backdrop-filter: blur(2px);
}}

h1 {{
    margin-top: 0;
}}

.card {{
    background: rgba(255, 255, 255, .94);
    border-radius: 12px;
    padding: 18px;
    margin-bottom: 18px;
    box-shadow: 0 2px 8px rgba(0,0,0,.06);
}}

.grid {{
    display: grid;
    grid-template-columns: repeat(
        auto-fit,
        minmax(230px, 1fr)
    );
    gap: 14px;
}}

.stat {{
    background: rgba(255, 255, 255, .94);
    border-radius: 12px;
    padding: 16px;
    box-shadow: 0 2px 8px rgba(0,0,0,.06);
}}

.stat .label {{
    color: #687078;
    font-size: 13px;
}}

.stat .value {{
    font-size: 25px;
    font-weight: 700;
    margin-top: 7px;
}}

table {{
    width: 100%;
    border-collapse: collapse;
}}

th,
td {{
    padding: 9px 8px;
    border-bottom: 1px solid #e6e9eb;
    text-align: left;
    vertical-align: top;
}}

th {{
    background: #f7f8f9;
}}

input,
select,
button {{
    font: inherit;
}}

input,
select {{
    width: 100%;
    padding: 10px;
    border: 1px solid #ccd1d5;
    border-radius: 7px;
    background: white;
}}

button,
.button {{
    display: inline-block;
    padding: 10px 14px;
    border: 0;
    border-radius: 7px;
    background: #17202a;
    color: white;
    text-decoration: none;
    cursor: pointer;
}}

.invoice-list {{
    display: grid;
    gap: 14px;
}}

.invoice-card {{
    border: 1px solid #dfe4e8;
    border-radius: 11px;
    padding: 14px;
    background: rgba(251, 252, 253, .94);
}}

.invoice-facts {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(155px, 1fr));
    gap: 12px;
}}

.invoice-facts > div {{
    min-width: 0;
    overflow-wrap: anywhere;
}}

.invoice-label {{
    display: block;
    margin-bottom: 4px;
    color: #687078;
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
}}

.invoice-facts .small {{
    display: block;
    margin-top: 3px;
    overflow-wrap: anywhere;
}}

.invoice-description {{
    margin-top: 12px;
    padding-top: 10px;
    border-top: 1px solid #e6e9eb;
}}

.invoice-actions {{
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
    margin-top: 12px;
}}

.invoice-actions form {{
    display: inline-flex !important;
    margin: 0;
}}

.invoice-actions .button,
.invoice-actions button {{
    padding: 8px 11px;
}}

.invoice-empty {{
    padding: 18px;
    border: 1px dashed #ccd1d5;
    border-radius: 9px;
    color: #687078;
}}

.form-grid {{
    display: grid;
    grid-template-columns:
        repeat(auto-fit, minmax(220px, 1fr));
    gap: 12px;
    align-items: end;
}}

.small {{
    color: #687078;
    font-size: 13px;
}}

.ok {{
    color: #147a42;
    font-weight: 700;
}}

.error {{
    color: #b42318;
    font-weight: 700;
}}

.section-title {{
    margin: 0 0 12px;
}}

.vehicle-header {{
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    gap: 10px;
    align-items: center;
    margin-bottom: 14px;
}}

.vehicle-header h2 {{
    margin: 0;
    font-size: 21px;
}}

.badge {{
    display: inline-block;
    padding: 6px 10px;
    border-radius: 999px;
    font-size: 13px;
    font-weight: 700;
}}

.badge-rest {{ background: #e8f5e9; color: #17652c; }}
.badge-ready {{ background: #e3f2fd; color: #145a86; }}
.badge-work {{ background: #fff3cd; color: #7a5500; }}
.badge-drive {{ background: #e8eaf6; color: #303f9f; }}
.badge-warning {{ background: #fdecec; color: #a61b1b; }}
.badge-muted {{ background: #eceff1; color: #5f6b72; }}

.detail-grid {{
    display: grid;
    grid-template-columns:
        repeat(auto-fit, minmax(185px, 1fr));
    gap: 10px;
}}

.detail {{
    padding: 12px;
    border: 1px solid #e3e7ea;
    border-radius: 9px;
    background: #fafbfc;
}}

.detail .label {{
    color: #687078;
    font-size: 12px;
}}

.detail .value {{
    margin-top: 5px;
    font-size: 17px;
    font-weight: 700;
}}

.alert {{
    padding: 11px 13px;
    border-radius: 8px;
    margin-top: 12px;
}}

.alert-warning {{
    background: #fff4e5;
    color: #7a4300;
    border: 1px solid #ffd49a;
}}

.alert-error {{
    background: #fdecec;
    color: #8d1717;
    border: 1px solid #f3b8b8;
}}

.alert-ok {{
    background: #eaf7ef;
    color: #17652c;
    border: 1px solid #b9e2c7;
}}

.vehicle-link {{
    display: block;
    color: #17202a;
    text-decoration: none;
    font-weight: 700;
}}

.vehicle-link:hover {{
    text-decoration: underline;
}}

#map {{
    height: 560px;
    border-radius: 10px;
}}

body.page-gps {{
    overflow: hidden;
}}

body.page-gps .wrap {{
    max-width: none;
    margin: 0;
    padding: 0;
}}

body.page-gps .wrap > h1,
body.page-gps .powered-by {{
    display: none;
}}

.gps-screen {{
    position: relative;
    width: 100%;
    min-height: 520px;
    background: #dce5e9;
}}

.gps-screen #map {{
    width: 100%;
    min-height: 520px;
    border-radius: 0;
}}

.gps-map-toolbar {{
    position: absolute;
    z-index: 800;
    top: 12px;
    left: 56px;
    width: min(420px, calc(100vw - 75px));
    padding: 12px;
    border: 1px solid rgba(16, 42, 59, .16);
    border-radius: 11px;
    background: rgba(255, 255, 255, .94);
    box-shadow: 0 4px 18px rgba(15, 37, 51, .18);
    backdrop-filter: blur(5px);
    max-height: calc(100vh - 95px);
    overflow-y: auto;
}}

.gps-map-toolbar.collapsed {{
    width: min(315px, calc(100vw - 75px));
    padding: 7px;
    overflow: hidden;
}}

.gps-toolbar-toggle {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    width: 100%;
    padding: 8px 10px;
    border: 0;
    border-radius: 8px;
    background: #163e55;
    color: #ffffff;
    font-size: 14px;
    font-weight: 800;
    text-align: left;
}}

.gps-toolbar-toggle:hover {{
    background: #0f5265;
}}

.gps-toolbar-toggle-icon {{
    margin-left: 10px;
    font-size: 15px;
}}

.gps-toolbar-content {{
    margin-top: 11px;
}}

.gps-toolbar-content[hidden] {{
    display: none;
}}

.gps-route-planner label {{
    display: block;
    margin: 0 0 5px;
    color: #21313c;
    font-size: 12px;
    font-weight: 800;
}}

.gps-route-planner select,
.gps-route-planner input {{
    width: 100%;
    min-width: 0;
    margin: 0 0 9px;
}}

.gps-address-row {{
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 7px;
}}

.gps-address-row input {{
    margin-bottom: 0;
}}

.gps-address-row button {{
    padding: 9px 12px;
}}

.gps-address-results {{
    display: grid;
    gap: 5px;
    max-height: 190px;
    margin-top: 7px;
    overflow-y: auto;
}}

.gps-address-results[hidden] {{
    display: none;
}}

.gps-address-result {{
    width: 100%;
    padding: 8px 10px;
    border: 1px solid #cbd8df;
    border-radius: 7px;
    background: #f7fafb;
    color: #1d3342;
    text-align: left;
    font-size: 13px;
    line-height: 1.3;
}}

.gps-address-result:hover {{
    border-color: #087f8c;
    background: #e9f7f8;
}}

.gps-build-route {{
    width: 100%;
    margin-top: 9px;
}}

.gps-route-options {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 7px;
    margin: 0 0 9px;
}}

.gps-route-option {{
    display: flex !important;
    align-items: flex-start;
    gap: 7px;
    margin: 0 !important;
    padding: 8px;
    border: 1px solid #cbd8df;
    border-radius: 8px;
    background: #f7fafb;
    cursor: pointer;
}}

.gps-route-option:has(input:checked) {{
    border-color: #087f8c;
    background: #e9f7f8;
}}

.gps-route-option input {{
    width: auto;
    margin: 2px 0 0;
}}

.gps-route-option span {{
    font-size: 12px;
    line-height: 1.25;
}}

.gps-route-option strong {{
    display: block;
    color: #18384b;
}}

.gps-fuel-fields {{
    display: grid;
    grid-template-columns: 1fr 1fr 82px;
    gap: 7px;
    margin-bottom: 9px;
}}

.gps-fuel-fields label {{
    margin: 0;
}}

.gps-fuel-fields input,
.gps-fuel-fields select {{
    margin: 5px 0 0;
}}

.gps-toll-note {{
    margin-top: 9px;
    padding: 9px;
    border-radius: 8px;
    background: #fff7df;
    color: #654d0b;
    font-size: 12px;
    line-height: 1.35;
}}

.gps-toolbar-divider {{
    height: 1px;
    margin: 12px 0;
    background: #dce5e9;
}}

.gps-map-actions {{
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
}}

.gps-map-actions button {{
    padding: 9px 12px;
}}

.gps-map-actions button.active {{
    background: #087f8c;
}}

.gps-measure-result {{
    margin-top: 9px;
    color: #21313c;
    font-size: 14px;
    line-height: 1.35;
}}

.gps-map-brand {{
    position: absolute;
    z-index: 700;
    right: 14px;
    bottom: 25px;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 7px 10px;
    border-radius: 10px;
    background: rgba(255, 255, 255, .72);
    color: #16344b;
    font-size: 12px;
    font-weight: 800;
    pointer-events: none;
}}

.gps-map-brand img {{
    width: 46px;
    height: 46px;
    object-fit: contain;
    opacity: .72;
}}

.toll-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
    gap: 12px;
}}

.toll-card {{
    padding: 15px;
    border: 1px solid #dce5e9;
    border-radius: 12px;
    background: rgba(255, 255, 255, .94);
}}

.toll-card h3 {{
    margin: 0 0 7px;
}}

.toll-card p {{
    margin: 5px 0;
}}

.toll-buy-link {{
    display: inline-block;
    margin-top: 8px;
    padding: 8px 11px;
    border-radius: 8px;
    background: #087f8c;
    color: #ffffff !important;
    text-decoration: none;
    font-weight: 800;
}}

@media (max-width: 700px) {{
    .topbar {{
        padding: 14px;
    }}

    .brand-row {{
        align-items: flex-start;
        gap: 12px;
    }}

    .brand-divider {{
        display: none;
    }}

    .platform-brand-block {{
        width: 100%;
    }}

    .language-picker {{
        width: 100%;
        margin-left: 0;
    }}

    .language-picker select {{
        width: 100%;
    }}

    .company-logo {{
        width: 52px;
        height: 52px;
    }}

    .page-watermark {{
        top: 190px;
    }}

    .page-watermark img {{
        width: 94vw;
        opacity: .115;
    }}

    .wrap {{
        padding: 14px;
    }}

    th,
    td {{
        font-size: 13px;
    }}

    #map {{
        height: 420px;
    }}

    .gps-screen,
    .gps-screen #map {{
        min-height: 440px;
    }}

    .gps-map-toolbar {{
        top: 10px;
        left: 48px;
        width: calc(100vw - 60px);
        padding: 10px;
    }}

    .gps-map-toolbar.collapsed {{
        width: min(280px, calc(100vw - 60px));
        padding: 6px;
    }}

    .gps-map-brand {{
        right: 8px;
        bottom: 21px;
    }}

    .gps-fuel-fields {{
        grid-template-columns: 1fr 1fr;
    }}
}}
</style>
{extra_head}
</head>

<body class="{page_class}">

<div class="topbar">
    <div class="brand-row">
        <div class="platform-brand-block">
            <a class="platform-brand" href="/">
                TRANV<span class="platform-iq">IQ</span>
            </a>
            <div class="platform-tagline">{platform_tagline}</div>
        </div>

        <div class="brand-divider"></div>

        <div class="company-brand">
            <img
                class="company-logo"
                src="/assets/company-logo.jpg"
                alt="{company}"
            >
            <div>
                <div class="company-caption">{company_label}</div>
                <div class="company-name">{company}</div>
                {role_badge}
            </div>
        </div>

        {language_picker}
    </div>
    {nav}
</div>

<div class="page-watermark" aria-hidden="true">
    <img
        src="/assets/company-logo.jpg"
        alt=""
    >
</div>

<div class="wrap">
    <h1>{title}</h1>
    {body}
</div>

<div class="powered-by">
    Powered by <strong>TRANVIQ</strong>
</div>

</body>
</html>
""".format(
        title=visible_title,
        language=language,
        page_class=page_class,
        company=company_display_name,
        company_label=escape(t("company")),
        platform=PLATFORM_NAME,
        platform_tagline=PLATFORM_TAGLINE,
        language_picker=language_picker,
        nav=nav,
        role_badge=role_badge,
        body=body,
        extra_head=""
    )


@app.route("/assets/company-logo.jpg")
def company_logo_asset():
    logo_bytes, logo_mime_type = get_company_logo(
        COMPANY_ID
    )

    if logo_bytes:
        response = Response(
            logo_bytes,
            mimetype=logo_mime_type
        )
        response.headers["Cache-Control"] = (
            "public, max-age=300"
        )
        return response

    try:
        logo_bytes = base64.b64decode(
            COMPANY_LOGO_BASE64,
            validate=True
        )
    except (ValueError, TypeError):
        return Response(status=404)

    response = Response(
        logo_bytes,
        mimetype="image/jpeg"
    )
    response.headers["Cache-Control"] = (
        "public, max-age=86400"
    )
    return response


@app.route("/language", methods=["POST"])
def change_language():
    language = request.form.get("language", "")
    if language in LANGUAGES:
        session["language"] = language

    next_url = request.form.get("next", "/login")
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/login"
    return redirect(next_url)


register_branding_routes(
    app,
    page,
    COMPANY_ID,
    COMPANY_NAME,
    t
)


try:
    from finance import register_finance_routes

    register_finance_routes(
        app,
        page,
        VEHICLES,
        html_text
    )
    FINANCE_MODULE_ERROR = ""
except Exception as finance_exc:
    FINANCE_MODULE_ERROR = str(finance_exc)


@app.route(
    "/login",
    defaults={"role": None},
    methods=["GET", "POST"]
)
@app.route("/login/<role>", methods=["GET", "POST"])
def login(role):
    if role is None:
        body = """
        <div class="grid login-choice-grid">
            <div class="card">
                <h2>{director}</h2>
                <p>{director_desc}</p>
                <a class="button" href="/login/director">{sign_in}</a>
            </div>
            <div class="card">
                <h2>{dispatcher}</h2>
                <p>{dispatcher_desc}</p>
                <a class="button" href="/login/dispatcher">{sign_in}</a>
            </div>
            <div class="card">
                <h2>{driver}</h2>
                <p>{driver_desc}</p>
                <a class="button" href="/login/driver">{sign_in}</a>
            </div>
        </div>
        """.format(
            director=escape(t("director")),
            director_desc=escape(t("director_desc")),
            dispatcher=escape(t("dispatcher")),
            dispatcher_desc=escape(t("dispatcher_desc")),
            driver=escape(t("driver")),
            driver_desc=escape(t("driver_desc")),
            sign_in=escape(t("sign_in"))
        )
        return page(t("choose_login"), body)

    if role not in ROLE_LABELS:
        return redirect(url_for("login"))

    credentials = {
        "director": (ADMIN_USER, ADMIN_PASSWORD),
        "dispatcher": (
            DISPATCHER_USER,
            DISPATCHER_PASSWORD
        ),
        "driver": (DRIVER_USER, DRIVER_PASSWORD)
    }

    expected_user, expected_password = credentials[role]

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        credentials_configured = bool(
            expected_user and expected_password
        )

        credentials_valid = (
            credentials_configured
            and hmac.compare_digest(username, expected_user)
            and hmac.compare_digest(password, expected_password)
        )

        if credentials_valid:
            session["logged_in"] = True
            session["role"] = role
            session["username"] = username
            return redirect(role_home_url(role))

        if not credentials_configured:
            error = (
                "<p class='error'>"
                + escape(t("login_not_configured"))
                + "</p>"
            )
        else:
            error = (
                "<p class='error'>"
                + escape(t("wrong_credentials"))
                + "</p>"
            )
    else:
        error = ""

    body = """
    <div class="card" style="max-width:420px">
        {error}

        <form method="post">

            <p>
                <label>{login_label}</label>
                <input
                    name="username"
                    autocomplete="username"
                >
            </p>

            <p>
                <label>{password_label}</label>
                <input
                    name="password"
                    type="password"
                    autocomplete="current-password"
                >
            </p>

            <button type="submit">{sign_in}</button>

        </form>
    </div>
    """.format(
        error=error,
        login_label=escape(t("login")),
        password_label=escape(t("password")),
        sign_in=escape(t("sign_in"))
    )

    return page(
        t("login_title") + ": " + t(role),
        body
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.before_request
def require_login():
    if (
        request.path == "/health"
        or request.path == "/login"
        or request.path.startswith("/login/")
        or request.path == "/assets/company-logo.jpg"
        or request.path == "/language"
        or request.path == "/api/finance/email-invoices/import"
    ):
        return None

    if not is_logged_in():
        return redirect(url_for("login"))

    role = current_role()

    if role == "director":
        return None

    allowed_endpoints = ROLE_ENDPOINTS.get(role, set())

    if (
        request.endpoint in allowed_endpoints
        or request.endpoint == "logout"
    ):
        return None

    return redirect(role_home_url(role))


@app.route("/director")
def director_dashboard():
    return redirect(url_for("home"))


@app.route("/dispatcher")
def dispatcher_dashboard():
    body = """
    <div class="card">
        <h2>Робоча панель логіста</h2>
        <p>
            Тут будуть замовлення, призначення автомобілів і водіїв,
            статуси рейсів та транспортні документи.
        </p>
        <p class="small">
            Фінанси директора й особистий кабінет водія недоступні.
        </p>
    </div>
    """
    return page(
        "Кабінет логіста",
        body,
        "dispatcher"
    )


@app.route("/driver")
def driver_dashboard():
    body = """
    <div class="card">
        <h2>Мої рейси</h2>
        <p>
            Тут водій отримуватиме роботу, змінюватиме статус рейсу
            та завантажуватиме CMR, фотографії й скани документів.
        </p>
        <p class="small">
            Інформація інших водіїв, логіста та директора недоступна.
        </p>
    </div>
    """
    return page(
        "Кабінет водія",
        body,
        "driver"
    )


@app.route("/road-payments")
def road_payments():
    countries = [
        (
            "🇵🇱 Польща",
            "До 3,5 т: окремі платні автомагістралі. "
            "Понад 3,5 т: система e-TOLL.",
            "https://etoll.gov.pl/en/",
            "Відкрити e-TOLL"
        ),
        (
            "🇩🇪 Німеччина",
            "До 3,5 т: загальної віньєтки немає. "
            "Понад 3,5 т: вантажний дорожній збір Toll Collect.",
            "https://www.toll-collect.de/en/",
            "Відкрити Toll Collect"
        ),
        (
            "🇦🇹 Австрія",
            "До 3,5 т: електронна віньєтка. "
            "Понад 3,5 т: GO-Box і кілометрова оплата.",
            "https://shop.asfinag.at/en/",
            "Купити в ASFINAG"
        ),
        (
            "🇨🇿 Чехія",
            "До 3,5 т: електронна віньєтка. "
            "Понад 3,5 т: електронна система MYTO CZ.",
            "https://edalnice.cz/en/index.html",
            "Купити e-vignette"
        ),
        (
            "🇸🇰 Словаччина",
            "До 3,5 т: електронна віньєтка. "
            "Понад 3,5 т: кілометрова система eMyto.",
            "https://eznamka.sk/en",
            "Купити eZnamka"
        ),
        (
            "🇭🇺 Угорщина",
            "До 3,5 т: e-Matrica, категорія залежить від авто. "
            "Понад 3,5 т: HU-GO.",
            "https://ematrica.nemzetiutdij.hu/",
            "Купити e-Matrica"
        ),
        (
            "🇸🇮 Словенія",
            "До 3,5 т: e-vignette 2A або 2B. "
            "Понад 3,5 т: DarsGo.",
            "https://evinjeta.dars.si/en",
            "Купити e-vignette"
        ),
        (
            "🇨🇭 Швейцарія",
            "До 3,5 т: швейцарська віньєтка. "
            "Понад 3,5 т: збір для важкого транспорту.",
            "https://via.admin.ch/shop/",
            "Купити e-vignette"
        ),
        (
            "🇷🇴 Румунія",
            "Rovinieta потрібна для більшості транспортних засобів. "
            "Категорія залежить від ваги та осей.",
            "https://www.erovinieta.ro/vignettes-portal-web/",
            "Купити Rovinieta"
        ),
        (
            "🇧🇬 Болгарія",
            "До 3,5 т: електронна віньєтка. "
            "Понад 3,5 т: маршрутний або кілометровий збір.",
            "https://web.bgtoll.bg/",
            "Відкрити BG Toll"
        ),
        (
            "🇧🇪 Бельгія",
            "До 3,5 т: загальної віньєтки немає. "
            "Понад 3,5 т: кілометровий збір Viapass.",
            "https://www.viapass.be/en/",
            "Відкрити Viapass"
        ),
        (
            "🇫🇷 🇮🇹 🇪🇸 🇵🇹 Західна Європа",
            "У Франції, Італії, Іспанії та Португалії "
            "оплата часто стягується за конкретні ділянки, "
            "мости або тунелі, а не загальною віньєткою.",
            "https://www.autoroutes.fr/en/",
            "Інформація про дороги"
        )
    ]

    cards = []
    for title, description, link, link_text in countries:
        cards.append(
            """
            <article class="toll-card">
                <h3>{title}</h3>
                <p>{description}</p>
                <a
                    class="toll-buy-link"
                    href="{link}"
                    target="_blank"
                    rel="noopener noreferrer"
                >{link_text}</a>
            </article>
            """.format(
                title=title,
                description=description,
                link=link,
                link_text=link_text
            )
        )

    route_link = ""
    if current_role() != "driver":
        route_link = (
            '<p><a class="button" href="/gps">'
            'Розрахувати маршрут, паливо й оплату доріг'
            '</a></p>'
        )

    body = """
    <div class="card">
        <h2>🛣️ Оплата доріг і віньєти</h2>
        <p>
            Вибір тарифу залежить від ваги, кількості осей,
            висоти, екологічного класу й країни.
            Купуйте тільки на офіційних сторінках операторів.
        </p>
        {route_link}
    </div>
    <div class="toll-grid">{cards}</div>
    """.format(
        route_link=route_link,
        cards="".join(cards)
    )

    return page(
        "Оплата доріг",
        body,
        "road_payments"
    )


@app.route("/")
def home():
    states = get_vehicle_states()
    state_map = state_map_by_vehicle(states)

    cards = []

    for vehicle in VEHICLES:
        state = state_map.get(vehicle["id"])

        if state:
            speed = safe_float(state.get("speed"))
            fuel = safe_float(state.get("fuel_level"))

            latitude, longitude = extract_coordinates(
                state.get("location")
            )

            if speed is not None:
                speed_text = (
                    format_number(speed, 0)
                    + " км/год"
                )
            else:
                speed_text = "—"

            if fuel is not None:
                fuel_text = (
                    format_number(fuel, 1)
                    + "%"
                )
            else:
                fuel_text = "—"

            if latitude is not None and longitude is not None:
                location_text = (
                    f"{latitude:.6f}, "
                    f"{longitude:.6f}"
                )
            else:
                location_text = "Немає координат"

            status_text = "Є дані Navirec"
            status_class = "ok"

        else:
            speed_text = "—"
            fuel_text = "—"
            location_text = "Немає поточного стану"
            status_text = "Немає даних"
            status_class = "error"

        cards.append(
            """
            <div class="stat">

                <div class="label">{name}</div>

                <div class="value">{speed}</div>

                <div class="small">
                    Паливо: {fuel}
                </div>

                <div class="small">
                    GPS: {location}
                </div>

                <p class="{status_class}">
                    {status}
                </p>

                <a
                    class="button"
                    href="/vehicle/{vehicle_id}"
                >
                    Відкрити
                </a>

            </div>
            """.format(
                name=vehicle["name"],
                speed=speed_text,
                fuel=fuel_text,
                location=location_text,
                status_class=status_class,
                status=status_text,
                vehicle_id=vehicle["id"]
            )
        )

    body = """
    <div class="card">

        <p>
            Компанія:
            <strong>{company}</strong>
        </p>

        <p class="small">
            Автомобілів у системі: {count}
        </p>

    </div>

    <div class="grid">
        {cards}
    </div>
    """.format(
        company=COMPANY_NAME,
        count=len(VEHICLES),
        cards="".join(cards)
    )

    return page(
        "Панель керування",
        body,
        "home"
    )


@app.route("/vehicles")
def vehicles():
    states = get_vehicle_states()
    state_map = state_map_by_vehicle(states)

    rows = []

    for vehicle in VEHICLES:
        state = state_map.get(vehicle["id"])

        if state:
            speed = safe_float(state.get("speed"))
            fuel = safe_float(state.get("fuel_level"))

            latitude, longitude = extract_coordinates(
                state.get("location")
            )

            if speed is not None:
                speed_text = (
                    format_number(speed, 0)
                    + " км/год"
                )
            else:
                speed_text = "—"

            if fuel is not None:
                fuel_text = (
                    format_number(fuel, 1)
                    + "%"
                )
            else:
                fuel_text = "—"

            if latitude is not None and longitude is not None:
                gps_text = (
                    f"{latitude:.6f}, "
                    f"{longitude:.6f}"
                )
            else:
                gps_text = "—"

        else:
            speed_text = "—"
            fuel_text = "—"
            gps_text = "—"

        rows.append(
            """
            <tr>

                <td>
                    <a
                        class="vehicle-link"
                        href="/vehicle/{id}"
                    >
                        {name}
                    </a>
                </td>

                <td>{speed}</td>
                <td>{fuel}</td>
                <td>{gps}</td>

                <td>
                    <a
                        class="button"
                        href="/vehicle/{id}"
                    >
                        Деталі
                    </a>
                </td>

            </tr>
            """.format(
                id=vehicle["id"],
                name=vehicle["name"],
                speed=speed_text,
                fuel=fuel_text,
                gps=gps_text
            )
        )

    body = """
    <div class="card">

        <table>

            <thead>
                <tr>
                    <th>Автомобіль</th>
                    <th>Швидкість</th>
                    <th>Паливо</th>
                    <th>GPS</th>
                    <th></th>
                </tr>
            </thead>

            <tbody>
                {rows}
            </tbody>

        </table>

    </div>
    """.format(rows="".join(rows))

    return page(
        "Автомобілі",
        body,
        "vehicles"
    )


@app.route("/vehicle/<vehicle_id>")
def vehicle_page(vehicle_id):
    vehicle = vehicle_by_id(vehicle_id)

    if not vehicle:
        return (
            page(
                "Автомобіль не знайдено",
                """
                <div class="card">
                    <p class="error">
                        Невідомий автомобіль.
                    </p>
                </div>
                """,
                "vehicles"
            ),
            404
        )

    states = get_vehicle_states()
    state = state_for_vehicle(
        vehicle["id"],
        states
    )

    if state:
        latitude, longitude = extract_coordinates(
            state.get("location")
        )

        speed = safe_float(state.get("speed"))
        fuel = safe_float(state.get("fuel_level"))
        heading = safe_float(state.get("heading"))
        engine_speed = safe_float(
            state.get("engine_speed")
        )
        total_distance = safe_float(
            state.get("total_distance")
        )
        ignition = state.get("ignition")

        if speed is not None:
            speed_text = (
                format_number(speed, 0)
                + " км/год"
            )
        else:
            speed_text = "—"

        if fuel is not None:
            fuel_text = (
                format_number(fuel, 1)
                + "%"
            )
        else:
            fuel_text = "—"

        if heading is not None:
            heading_text = (
                format_number(heading, 0)
                + "°"
            )
        else:
            heading_text = "—"

        if engine_speed is not None:
            engine_text = (
                format_number(engine_speed, 0)
                + " об/хв"
            )
        else:
            engine_text = "—"

        if total_distance is not None:
            distance_text = (
                format_number(total_distance, 1)
                + " км"
            )
        else:
            distance_text = "—"

        if ignition is not None:
            ignition_text = str(ignition)
        else:
            ignition_text = "—"

        if latitude is not None and longitude is not None:
            gps_text = (
                f"{latitude:.6f}, "
                f"{longitude:.6f}"
            )
        else:
            gps_text = "Немає координат"

    else:
        latitude = None
        longitude = None
        speed_text = "—"
        fuel_text = "—"
        heading_text = "—"
        engine_text = "—"
        distance_text = "—"
        ignition_text = "—"
        gps_text = "Немає поточного стану"

    map_block = ""

    if latitude is not None and longitude is not None:
        safe_name = vehicle["name"].replace(
            "'",
            "\\'"
        )

        map_block = """
        <div class="card">
            <div id="map"></div>
        </div>

        <link
            rel="stylesheet"
            href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
        >

        <script
            src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js">
        </script>

        <script>
        const map = L.map('map').setView(
            [{lat}, {lon}],
            10
        );

        L.tileLayer(
            'https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
            {{
                maxZoom: 19,
                attribution: '&copy; OpenStreetMap'
            }}
        ).addTo(map);

        L.marker([{lat}, {lon}])
            .addTo(map)
            .bindPopup('{name}')
            .openPopup();
        </script>
        """.format(
            lat=latitude,
            lon=longitude,
            name=safe_name
        )

    body = """
    <div class="card">

        <p>
            <strong>ID:</strong>
            {id}
        </p>

        <p>
            <strong>GPS:</strong>
            {gps}
        </p>

    </div>

    <div class="grid">

        <div class="stat">
            <div class="label">Швидкість</div>
            <div class="value">{speed}</div>
        </div>

        <div class="stat">
            <div class="label">Паливо</div>
            <div class="value">{fuel}</div>
        </div>

        <div class="stat">
            <div class="label">Напрямок</div>
            <div class="value">{heading}</div>
        </div>

        <div class="stat">
            <div class="label">Оберти двигуна</div>
            <div class="value">{engine}</div>
        </div>

        <div class="stat">
            <div class="label">Загальна відстань</div>
            <div class="value">{distance}</div>
        </div>

        <div class="stat">
            <div class="label">Запалювання</div>
            <div class="value">{ignition}</div>
        </div>

    </div>

    {map_block}

    <div class="card">

        <a
            class="button"
            href="/history?vehicle={id}"
        >
            Історія маршруту
        </a>

        <a
            class="button"
            href="/gps?vehicle={id}"
        >
            GPS
        </a>

    </div>
    """.format(
        id=vehicle["id"],
        gps=gps_text,
        speed=speed_text,
        fuel=fuel_text,
        heading=heading_text,
        engine=engine_text,
        distance=distance_text,
        ignition=ignition_text,
        map_block=map_block
    )

    return page(
        vehicle["name"],
        body,
        "vehicles"
    )


@app.route("/api/geocode")
def geocode_search():
    global GEOCODE_LAST_REQUEST_AT

    query = request.args.get("q", "").strip()

    if len(query) < 3:
        return jsonify({"results": []})

    query = query[:180]
    language = current_language()
    cache_key = (language, query.casefold())
    now = time.monotonic()
    cached = GEOCODE_CACHE.get(cache_key)

    if cached and now - cached[0] < GEOCODE_CACHE_TTL:
        return jsonify({"results": cached[1]})

    try:
        with GEOCODE_LOCK:
            cached = GEOCODE_CACHE.get(cache_key)
            now = time.monotonic()

            if cached and now - cached[0] < GEOCODE_CACHE_TTL:
                return jsonify({"results": cached[1]})

            wait_seconds = 1.05 - (
                now - GEOCODE_LAST_REQUEST_AT
            )
            if wait_seconds > 0:
                time.sleep(wait_seconds)

            response = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={
                    "q": query,
                    "format": "jsonv2",
                    "limit": 5,
                    "addressdetails": 1,
                    "accept-language": language
                },
                headers={
                    "User-Agent": (
                        "TRANVIQ/1.0 "
                        "(transport route planner)"
                    )
                },
                timeout=10
            )
            GEOCODE_LAST_REQUEST_AT = time.monotonic()
            response.raise_for_status()
            raw_results = response.json()

        results = []
        for item in raw_results:
            try:
                latitude = float(item.get("lat"))
                longitude = float(item.get("lon"))
            except (TypeError, ValueError):
                continue

            display_name = str(
                item.get("display_name") or ""
            ).strip()
            if not display_name:
                continue

            results.append({
                "name": display_name,
                "latitude": latitude,
                "longitude": longitude,
                "type": str(item.get("type") or ""),
                "country_code": str(
                    (item.get("address") or {}).get(
                        "country_code"
                    ) or ""
                ).lower()
            })

        GEOCODE_CACHE[cache_key] = (
            time.monotonic(),
            results
        )
        return jsonify({"results": results})
    except (requests.RequestException, ValueError):
        return jsonify({
            "results": [],
            "error": "Пошук адреси тимчасово недоступний."
        }), 503


def decode_google_polyline(encoded):
    points = []
    index = 0
    latitude = 0
    longitude = 0

    while index < len(encoded):
        for coordinate_index in range(2):
            result = 0
            shift = 0

            while True:
                if index >= len(encoded):
                    raise ValueError("Invalid encoded polyline")

                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1f) << shift
                shift += 5

                if byte < 0x20:
                    break

            difference = ~(result >> 1) \
                if result & 1 else result >> 1

            if coordinate_index == 0:
                latitude += difference
            else:
                longitude += difference

        points.append([
            latitude / 100000,
            longitude / 100000
        ])

    return points


def parse_route_point(value):
    if not isinstance(value, dict):
        return None

    try:
        latitude = float(value.get("latitude"))
        longitude = float(value.get("longitude"))
    except (TypeError, ValueError):
        return None

    if not (-90 <= latitude <= 90):
        return None
    if not (-180 <= longitude <= 180):
        return None

    return latitude, longitude


def parse_vehicle_profile(value):
    if not isinstance(value, dict):
        value = {}

    def bounded_number(key, default, minimum, maximum):
        try:
            result = int(float(value.get(key, default)))
        except (TypeError, ValueError):
            result = default
        return max(minimum, min(maximum, result))

    emission_type = str(
        value.get("emission_type") or "DIESEL"
    ).upper()
    if emission_type not in {
        "DIESEL",
        "GASOLINE",
        "HYBRID",
        "ELECTRIC"
    }:
        emission_type = "DIESEL"

    return {
        "profile": str(value.get("profile") or "van_35")[:30],
        "weight_kg": bounded_number(
            "weight_kg", 3500, 500, 100000
        ),
        "height_mm": bounded_number(
            "height_mm", 2700, 1200, 6000
        ),
        "length_mm": bounded_number(
            "length_mm", 6500, 2000, 30000
        ),
        "width_mm": bounded_number(
            "width_mm", 2200, 1000, 4000
        ),
        "axles": bounded_number("axles", 2, 2, 10),
        "euro_class": str(
            value.get("euro_class") or "EURO_6"
        )[:20],
        "emission_type": emission_type
    }


def estimate_poland_a2_toll(route, vehicle_profile, avoid_tolls):
    """Estimate A2 toll when Google omits tollInfo.

    The official category-1 tariff valid from 2026-09-11 is 141 PLN
    for the 255 km Swiecko-Konin concession. Google route steps let us
    estimate how many Polish A2 kilometres the selected route uses.
    """
    if avoid_tolls:
        return None
    if vehicle_profile["weight_kg"] > 3500:
        return None
    if vehicle_profile["axles"] != 2:
        return None

    distance_m = 0.0
    for leg in route.get("legs") or []:
        for step in leg.get("steps") or []:
            instruction = str(
                step.get("navigationInstruction", {}).get(
                    "instructions"
                ) or ""
            ).upper()
            compact_instruction = (
                instruction.replace(" ", "")
                .replace("-", "")
            )
            if "A2" not in compact_instruction:
                continue

            locations = []
            for key in ("startLocation", "endLocation"):
                lat_lng = step.get(key, {}).get("latLng", {})
                try:
                    locations.append((
                        float(lat_lng.get("latitude")),
                        float(lat_lng.get("longitude"))
                    ))
                except (TypeError, ValueError):
                    pass

            # Do not count the German A2. The Polish A2 concession starts
            # near the border at Swiecko (longitude about 14.6 E).
            if locations:
                average_longitude = sum(
                    point[1] for point in locations
                ) / len(locations)
                if average_longitude < 14.5:
                    continue

            try:
                distance_m += float(step.get("distanceMeters") or 0)
            except (TypeError, ValueError):
                continue

    if distance_m < 1000:
        return None

    distance_km = distance_m / 1000
    rate_pln_per_km = 141 / 255
    amount = max(3, round(distance_km * rate_pln_per_km))
    return {
        "road": "A2",
        "amount": amount,
        "currency": "PLN",
        "distance_km": round(distance_km, 1),
        "method": "official_average_rate",
        "tariff_date": "2026-09-11",
        "source_url": "https://www.autostrada-a2.pl/oplaty/"
    }


def google_route(
    origin,
    destination,
    avoid_tolls,
    vehicle_profile
):
    vehicle_info = {
        "emissionType": vehicle_profile["emission_type"],
        "totalHeightMm": str(vehicle_profile["height_mm"]),
        "totalLengthMm": str(vehicle_profile["length_mm"]),
        "totalWidthMm": str(vehicle_profile["width_mm"]),
        "totalWeightKg": str(vehicle_profile["weight_kg"])
    }
    response = requests.post(
        "https://routes.googleapis.com/directions/v2:computeRoutes",
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
            "X-Goog-FieldMask": (
                "routes.distanceMeters,routes.duration,"
                "routes.polyline.encodedPolyline,"
                "routes.travelAdvisory.tollInfo,"
                "routes.legs.steps.distanceMeters,"
                "routes.legs.steps.startLocation,"
                "routes.legs.steps.endLocation,"
                "routes.legs.steps.navigationInstruction.instructions"
            )
        },
        json={
            "origin": {
                "location": {
                    "latLng": {
                        "latitude": origin[0],
                        "longitude": origin[1]
                    }
                }
            },
            "destination": {
                "location": {
                    "latLng": {
                        "latitude": destination[0],
                        "longitude": destination[1]
                    }
                }
            },
            "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_AWARE_OPTIMAL",
            "polylineQuality": "OVERVIEW",
            "computeAlternativeRoutes": False,
            "routeModifiers": {
                "avoidTolls": avoid_tolls,
                "vehicleInfo": vehicle_info
            },
            "extraComputations": ["TOLLS"],
            "languageCode": current_language(),
            "units": "METRIC"
        },
        timeout=20
    )
    response.raise_for_status()
    data = response.json()
    routes = data.get("routes") or []

    if not routes:
        raise ValueError("Route not found")

    route = routes[0]
    encoded_polyline = (
        route.get("polyline", {}).get("encodedPolyline")
        or ""
    )
    duration_text = str(route.get("duration") or "0s")
    duration_seconds = float(
        duration_text.removesuffix("s") or 0
    )
    toll_info = (
        route.get("travelAdvisory", {}).get("tollInfo")
        or {}
    )
    toll_prices = []

    for price in toll_info.get("estimatedPrice") or []:
        try:
            amount = float(price.get("units") or 0)
            amount += float(price.get("nanos") or 0) / 1000000000
        except (TypeError, ValueError):
            continue

        toll_prices.append({
            "amount": round(amount, 2),
            "currency": str(price.get("currencyCode") or "")
        })

    toll_estimate = None
    if not toll_prices:
        toll_estimate = estimate_poland_a2_toll(
            route,
            vehicle_profile,
            avoid_tolls
        )

    return {
        "provider": "google",
        "distance_m": float(route.get("distanceMeters") or 0),
        "duration_s": duration_seconds,
        "points": decode_google_polyline(encoded_polyline),
        "avoid_tolls": avoid_tolls,
        "has_tolls": bool(toll_info),
        "toll_prices": toll_prices,
        "toll_estimate": toll_estimate,
        "vehicle_profile": vehicle_profile
    }


def osrm_route(origin, destination):
    coordinates = (
        f"{origin[1]},{origin[0]};"
        f"{destination[1]},{destination[0]}"
    )
    response = requests.get(
        "https://router.project-osrm.org/route/v1/driving/"
        + coordinates,
        params={
            "overview": "full",
            "geometries": "geojson"
        },
        timeout=20
    )
    response.raise_for_status()
    data = response.json()
    routes = data.get("routes") or []

    if not routes:
        raise ValueError("Route not found")

    route = routes[0]
    route_points = []
    for coordinate in (
        route.get("geometry", {}).get("coordinates") or []
    ):
        if len(coordinate) >= 2:
            route_points.append([
                coordinate[1],
                coordinate[0]
            ])

    return {
        "provider": "osrm",
        "distance_m": float(route.get("distance") or 0),
        "duration_s": float(route.get("duration") or 0),
        "points": route_points,
        "avoid_tolls": False,
        "has_tolls": None,
        "toll_prices": []
    }


@app.route("/api/route", methods=["POST"])
def route_calculate():
    payload = request.get_json(silent=True) or {}
    origin = parse_route_point(payload.get("origin"))
    destination = parse_route_point(payload.get("destination"))
    avoid_tolls = bool(payload.get("avoid_tolls"))
    vehicle_profile = parse_vehicle_profile(
        payload.get("vehicle_profile")
    )

    if not origin or not destination:
        return jsonify({
            "error": "Неправильні координати маршруту."
        }), 400

    cache_key = (
        round(origin[0], 5),
        round(origin[1], 5),
        round(destination[0], 5),
        round(destination[1], 5),
        avoid_tolls,
        vehicle_profile["profile"],
        vehicle_profile["weight_kg"],
        vehicle_profile["height_mm"],
        vehicle_profile["length_mm"],
        vehicle_profile["width_mm"],
        vehicle_profile["axles"],
        vehicle_profile["euro_class"],
        bool(GOOGLE_MAPS_API_KEY)
    )
    cached = ROUTE_CACHE.get(cache_key)
    now = time.monotonic()

    if cached and now - cached[0] < ROUTE_CACHE_TTL:
        return jsonify(cached[1])

    if avoid_tolls and not GOOGLE_MAPS_API_KEY:
        return jsonify({
            "error": (
                "Для маршруту без платних доріг потрібно "
                "підключити Google Routes API."
            ),
            "code": "toll_service_not_configured"
        }), 503

    try:
        if GOOGLE_MAPS_API_KEY:
            route_data = google_route(
                origin,
                destination,
                avoid_tolls,
                vehicle_profile
            )
        else:
            route_data = osrm_route(origin, destination)

        if len(ROUTE_CACHE) >= 500:
            oldest_key = min(
                ROUTE_CACHE,
                key=lambda key: ROUTE_CACHE[key][0]
            )
            ROUTE_CACHE.pop(oldest_key, None)

        ROUTE_CACHE[cache_key] = (
            time.monotonic(),
            route_data
        )
        return jsonify(route_data)
    except (requests.RequestException, ValueError):
        if GOOGLE_MAPS_API_KEY and not avoid_tolls:
            try:
                route_data = osrm_route(origin, destination)
                return jsonify(route_data)
            except (requests.RequestException, ValueError):
                pass

        return jsonify({
            "error": "Маршрутний сервіс тимчасово недоступний."
        }), 503


@app.route("/gps")
def gps():
    selected_id = normalize_vehicle_id(
        request.args.get("vehicle", "")
    )

    states = get_vehicle_states()

    markers = []

    for vehicle in VEHICLES:
        state = state_for_vehicle(
            vehicle["id"],
            states
        )

        if not state:
            continue

        latitude, longitude = extract_coordinates(
            state.get("location")
        )

        if latitude is None or longitude is None:
            continue

        speed = safe_float(state.get("speed"))
        fuel = safe_float(state.get("fuel_level"))
        fuel_consumption = get_vehicle_average_consumption(
            vehicle["id"]
        )

        markers.append({
            "id": vehicle["id"],
            "name": vehicle["name"],
            "latitude": latitude,
            "longitude": longitude,
            "speed": speed,
            "fuel": fuel,
            "fuel_consumption": fuel_consumption
        })

    marker_json = json.dumps(
        markers,
        ensure_ascii=False
    )

    if markers:
        first = markers[0]
        center_lat = first["latitude"]
        center_lon = first["longitude"]
    else:
        center_lat = 51.1
        center_lon = 17.0

    body = """
    <div class="gps-screen">
        <div id="map"></div>

        <div class="gps-map-toolbar" id="gps-map-toolbar">
            <button
                type="button"
                class="gps-toolbar-toggle"
                id="gps-toolbar-toggle"
                aria-expanded="true"
                aria-controls="gps-toolbar-content"
            >
                <span>Планування маршруту</span>
                <span
                    class="gps-toolbar-toggle-icon"
                    id="gps-toolbar-toggle-icon"
                >▲</span>
            </button>

            <div
                class="gps-toolbar-content"
                id="gps-toolbar-content"
            >
            <div class="gps-route-planner">
                <label for="route-vehicle-select">
                    Початок маршруту — автомобіль
                </label>
                <select id="route-vehicle-select"></select>

                <label for="route-vehicle-profile">
                    Тип транспорту
                </label>
                <select id="route-vehicle-profile">
                    <option value="van_35">Бус до 3,5 т</option>
                    <option value="truck_75">Вантажний до 7,5 т</option>
                    <option value="truck_12">Вантажний до 12 т</option>
                    <option value="truck_18">Вантажний до 18 т</option>
                    <option value="truck_26">Вантажний до 26 т</option>
                    <option value="truck_40">Фура до 40 т</option>
                    <option value="truck_over_40">Понад 40 т</option>
                </select>

                <div class="gps-fuel-fields">
                    <label for="route-fuel-consumption">
                        Витрата, л/100 км
                        <input
                            type="number"
                            id="route-fuel-consumption"
                            min="1"
                            max="100"
                            step="0.1"
                            value="10.5"
                        >
                    </label>
                    <label for="route-fuel-price">
                        Ціна за літр
                        <input
                            type="number"
                            id="route-fuel-price"
                            min="0"
                            max="20"
                            step="0.01"
                            value="1.55"
                        >
                    </label>
                    <label for="route-fuel-currency">
                        Валюта
                        <select id="route-fuel-currency">
                            <option value="EUR">EUR</option>
                            <option value="PLN">PLN</option>
                        </select>
                    </label>
                </div>

                <label>Варіант маршруту</label>
                <div class="gps-route-options">
                    <label class="gps-route-option">
                        <input
                            type="radio"
                            name="route-mode"
                            value="fast"
                            checked
                        >
                        <span>
                            <strong>Швидкий</strong>
                            Платні дороги дозволені
                        </span>
                    </label>
                    <label class="gps-route-option">
                        <input
                            type="radio"
                            name="route-mode"
                            value="free"
                        >
                        <span>
                            <strong>Безплатний</strong>
                            Уникати платних доріг
                        </span>
                    </label>
                </div>

                <label for="destination-search">
                    Куди їдемо
                </label>
                <div class="gps-address-row">
                    <input
                        type="search"
                        id="destination-search"
                        placeholder="Місто, вулиця або повна адреса"
                        autocomplete="off"
                    >
                    <button type="button" id="address-search-button">
                        Шукати
                    </button>
                </div>
                <div
                    class="gps-address-results"
                    id="address-search-results"
                    hidden
                ></div>
                <button
                    type="button"
                    class="gps-build-route"
                    id="build-route-button"
                    disabled
                >
                    Прокласти маршрут
                </button>
                <div class="gps-toll-note" id="toll-note">
                    Вартість є орієнтовною. Вона залежить від ваги,
                    осей, екологічного класу, віньєт і способу оплати.
                </div>
            </div>

            <div class="gps-toolbar-divider"></div>

            <div class="gps-map-actions">
                <button type="button" id="measure-route-button">
                    Виміряти маршрут
                </button>
                <button type="button" id="clear-route-button">
                    Очистити карту
                </button>
            </div>
            <div class="gps-measure-result" id="measure-result">
                Натисніть «Виміряти маршрут», потім виберіть
                дві точки на карті.
            </div>
            </div>
        </div>

        <div class="gps-map-brand">
            <img src="/assets/company-logo.jpg" alt="">
            <span>Powered by TRANVIQ</span>
        </div>
    </div>

    <link
        rel="stylesheet"
        href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    >

    <script
        src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js">
    </script>

    <script>
    const vehicles = {markers};
    const selectedId = {selected};

    const map = L.map('map').setView(
        [{lat}, {lon}],
        6
    );

    L.tileLayer(
        'https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
        {{
            maxZoom: 19,
            attribution: '&copy; OpenStreetMap'
        }}
    ).addTo(map);

    const bounds = [];

    vehicles.forEach(function(vehicle) {{

        const marker = L.marker([
            vehicle.latitude,
            vehicle.longitude
        ]).addTo(map);

        const speed =
            vehicle.speed === null
            ? '—'
            : vehicle.speed.toFixed(0) + ' км/год';

        const fuel =
            vehicle.fuel === null
            ? '—'
            : vehicle.fuel.toFixed(1) + '%';

        marker.bindPopup(
            '<strong>' + vehicle.name + '</strong><br>' +
            'Швидкість: ' + speed + '<br>' +
            'Паливо: ' + fuel + '<br>' +
            vehicle.latitude.toFixed(6) +
            ', ' +
            vehicle.longitude.toFixed(6)
        );

        if (vehicle.id === selectedId) {{
            marker.openPopup();
        }}

        bounds.push([
            vehicle.latitude,
            vehicle.longitude
        ]);
    }});

    if (bounds.length > 1) {{
        map.fitBounds(
            bounds,
            {{padding: [30, 30]}}
        );
    }}

    const mapElement = document.getElementById('map');
    const toolbar = document.getElementById('gps-map-toolbar');
    const toolbarToggle = document.getElementById(
        'gps-toolbar-toggle'
    );
    const toolbarContent = document.getElementById(
        'gps-toolbar-content'
    );
    const toolbarToggleIcon = document.getElementById(
        'gps-toolbar-toggle-icon'
    );
    const measureButton = document.getElementById(
        'measure-route-button'
    );
    const clearButton = document.getElementById(
        'clear-route-button'
    );
    const measureResult = document.getElementById(
        'measure-result'
    );
    const vehicleSelect = document.getElementById(
        'route-vehicle-select'
    );
    const destinationInput = document.getElementById(
        'destination-search'
    );
    const addressSearchButton = document.getElementById(
        'address-search-button'
    );
    const addressResults = document.getElementById(
        'address-search-results'
    );
    const buildRouteButton = document.getElementById(
        'build-route-button'
    );
    const vehicleProfileSelect = document.getElementById(
        'route-vehicle-profile'
    );
    const fuelConsumptionInput = document.getElementById(
        'route-fuel-consumption'
    );
    const fuelPriceInput = document.getElementById(
        'route-fuel-price'
    );
    const fuelCurrencySelect = document.getElementById(
        'route-fuel-currency'
    );

    const vehicleProfiles = {{
        van_35: {{
            profile: 'van_35', weight_kg: 3500,
            height_mm: 2700, length_mm: 6500,
            width_mm: 2200, axles: 2,
            euro_class: 'EURO_6', emission_type: 'DIESEL',
            default_consumption: 10.5
        }},
        truck_75: {{
            profile: 'truck_75', weight_kg: 7500,
            height_mm: 3300, length_mm: 9000,
            width_mm: 2500, axles: 2,
            euro_class: 'EURO_6', emission_type: 'DIESEL',
            default_consumption: 17
        }},
        truck_12: {{
            profile: 'truck_12', weight_kg: 12000,
            height_mm: 3800, length_mm: 11000,
            width_mm: 2550, axles: 2,
            euro_class: 'EURO_6', emission_type: 'DIESEL',
            default_consumption: 22
        }},
        truck_18: {{
            profile: 'truck_18', weight_kg: 18000,
            height_mm: 4000, length_mm: 12000,
            width_mm: 2550, axles: 2,
            euro_class: 'EURO_6', emission_type: 'DIESEL',
            default_consumption: 25
        }},
        truck_26: {{
            profile: 'truck_26', weight_kg: 26000,
            height_mm: 4000, length_mm: 12000,
            width_mm: 2550, axles: 3,
            euro_class: 'EURO_6', emission_type: 'DIESEL',
            default_consumption: 29
        }},
        truck_40: {{
            profile: 'truck_40', weight_kg: 40000,
            height_mm: 4000, length_mm: 16500,
            width_mm: 2550, axles: 5,
            euro_class: 'EURO_6', emission_type: 'DIESEL',
            default_consumption: 32
        }},
        truck_over_40: {{
            profile: 'truck_over_40', weight_kg: 44000,
            height_mm: 4000, length_mm: 18500,
            width_mm: 2550, axles: 5,
            euro_class: 'EURO_6', emission_type: 'DIESEL',
            default_consumption: 36
        }}
    }};

    vehicles.forEach(function(vehicle) {{
        const option = document.createElement('option');
        option.value = vehicle.id;
        option.textContent = vehicle.name;
        if (vehicle.id === selectedId) {{
            option.selected = true;
        }}
        vehicleSelect.appendChild(option);
    }});

    if (!vehicles.length) {{
        const option = document.createElement('option');
        option.textContent = 'Немає актуальних GPS-координат';
        option.disabled = true;
        option.selected = true;
        vehicleSelect.appendChild(option);
    }}

    function setToolbarExpanded(expanded) {{
        toolbarContent.hidden = !expanded;
        toolbar.classList.toggle('collapsed', !expanded);
        toolbarToggle.setAttribute(
            'aria-expanded',
            expanded ? 'true' : 'false'
        );
        toolbarToggleIcon.textContent = expanded ? '▲' : '▼';

        try {{
            localStorage.setItem(
                'tranviq_gps_toolbar',
                expanded ? 'open' : 'closed'
            );
        }} catch (error) {{}}
    }}

    let toolbarExpanded = false;
    try {{
        toolbarExpanded = localStorage.getItem(
            'tranviq_gps_toolbar'
        ) === 'open';
    }} catch (error) {{}}

    setToolbarExpanded(toolbarExpanded);
    toolbarToggle.addEventListener('click', function() {{
        toolbarExpanded = !toolbarExpanded;
        setToolbarExpanded(toolbarExpanded);
    }});

    function updateFuelConsumption() {{
        const selectedVehicle = vehicles.find(function(item) {{
            return item.id === vehicleSelect.value;
        }});
        const profile = vehicleProfiles[vehicleProfileSelect.value]
            || vehicleProfiles.van_35;
        const navirecConsumption = selectedVehicle
            ? Number(selectedVehicle.fuel_consumption)
            : 0;

        fuelConsumptionInput.value =
            navirecConsumption > 0
            ? navirecConsumption.toFixed(1)
            : profile.default_consumption.toFixed(1);
        fuelConsumptionInput.dataset.source =
            navirecConsumption > 0 ? 'navirec' : 'profile';
        fuelConsumptionInput.title =
            navirecConsumption > 0
            ? 'Середня витрата з Navirec за останні 14 днів'
            : 'Орієнтовна витрата для цієї вагової категорії';
    }}

    try {{
        const savedFuelPrice = localStorage.getItem(
            'tranviq_fuel_price'
        );
        const savedFuelCurrency = localStorage.getItem(
            'tranviq_fuel_currency'
        );
        if (savedFuelPrice) {{
            fuelPriceInput.value = savedFuelPrice;
        }}
        if (savedFuelCurrency) {{
            fuelCurrencySelect.value = savedFuelCurrency;
        }}
    }} catch (error) {{
        // Браузер може блокувати localStorage у приватному режимі.
    }}

    updateFuelConsumption();
    vehicleSelect.addEventListener('change', updateFuelConsumption);
    vehicleProfileSelect.addEventListener(
        'change',
        updateFuelConsumption
    );
    fuelPriceInput.addEventListener('change', function() {{
        try {{
            localStorage.setItem(
                'tranviq_fuel_price',
                fuelPriceInput.value
            );
        }} catch (error) {{}}
    }});
    fuelCurrencySelect.addEventListener('change', function() {{
        try {{
            localStorage.setItem(
                'tranviq_fuel_currency',
                fuelCurrencySelect.value
            );
        }} catch (error) {{}}
    }});

    L.DomEvent.disableClickPropagation(toolbar);
    L.DomEvent.disableScrollPropagation(toolbar);

    function resizeGpsMap() {{
        const top = mapElement.getBoundingClientRect().top;
        const availableHeight = Math.max(
            440,
            window.innerHeight - top
        );
        mapElement.style.height = availableHeight + 'px';
        map.invalidateSize(false);
    }}

    window.addEventListener('resize', resizeGpsMap);
    window.requestAnimationFrame(resizeGpsMap);

    let measureMode = false;
    let measurePoints = [];
    let measureMarkers = [];
    let measureLayer = null;
    let selectedDestination = null;
    let plannedRouteLayer = null;
    let destinationMarker = null;

    function removeMeasurementLayers() {{
        measureMarkers.forEach(function(item) {{
            map.removeLayer(item);
        }});
        measureMarkers = [];
        measurePoints = [];

        if (measureLayer) {{
            map.removeLayer(measureLayer);
            measureLayer = null;
        }}
    }}

    function clearMeasurement() {{
        removeMeasurementLayers();
        measureMode = false;
        measureButton.classList.remove('active');
        measureResult.textContent =
            'Натисніть «Виміряти маршрут», потім виберіть ' +
            'дві точки на карті.';
    }}

    function removePlannedRoute() {{
        if (plannedRouteLayer) {{
            map.removeLayer(plannedRouteLayer);
            plannedRouteLayer = null;
        }}
        if (destinationMarker) {{
            map.removeLayer(destinationMarker);
            destinationMarker = null;
        }}
    }}

    function clearMapRoutes() {{
        clearMeasurement();
        removePlannedRoute();
    }}

    function formatDuration(seconds) {{
        const totalMinutes = Math.max(
            1,
            Math.round(seconds / 60)
        );
        const hours = Math.floor(totalMinutes / 60);
        const minutes = totalMinutes % 60;

        if (hours > 0) {{
            return hours + ' год ' + minutes + ' хв';
        }}
        return minutes + ' хв';
    }}

    function formatArrival(seconds) {{
        const arrival = new Date(
            Date.now() + (seconds * 1000)
        );
        return arrival.toLocaleString([], {{
            day: '2-digit',
            month: '2-digit',
            hour: '2-digit',
            minute: '2-digit'
        }});
    }}

    function selectedVehicleProfile() {{
        return vehicleProfiles[vehicleProfileSelect.value]
            || vehicleProfiles.van_35;
    }}

    function selectedRouteAvoidsTolls() {{
        const selectedMode = document.querySelector(
            'input[name="route-mode"]:checked'
        );
        return selectedMode && selectedMode.value === 'free';
    }}

    function selectRouteDestination(result) {{
        selectedDestination = result;
        destinationInput.value = result.name;
        buildRouteButton.disabled = !vehicles.length;
        addressResults.hidden = true;
        measureResult.textContent =
            'Адресу вибрано: ' + result.name +
            '. Натисніть «Прокласти маршрут».';
    }}

    function formatTollInformation(routeData) {{
        if (routeData.avoid_tolls) {{
            return 'Платні дороги: маршрут намагається їх уникати. ' +
                'Перевірте результат, бо повне уникнення не гарантується.';
        }}

        if (routeData.provider !== 'google') {{
            return 'Оплата доріг: даних про платні ділянки немає. ' +
                'Це не означає, що маршрут безплатний.';
        }}

        if (routeData.toll_prices && routeData.toll_prices.length) {{
            const prices = routeData.toll_prices.map(function(price) {{
                return Number(price.amount).toFixed(2) +
                    ' ' + price.currency;
            }});
            return 'Орієнтовна оплата доріг: ' + prices.join(' + ');
        }}

        if (routeData.toll_estimate) {{
            const estimate = routeData.toll_estimate;
            return 'Орієнтовна оплата ' + estimate.road + ': ≈ ' +
                Number(estimate.amount).toFixed(0) + ' ' +
                estimate.currency + ' (' +
                Number(estimate.distance_km).toFixed(1) +
                ' км платною дорогою; тариф від 11.09.2026).';
        }}

        if (routeData.has_tolls) {{
            return 'Є платні ділянки, але їхня ціна не визначена.';
        }}

        return 'Google не надав підтверджених даних про оплату. ' +
            'Це не означає, що платних ділянок немає.';
    }}

    function destinationRoadRule(profile) {{
        const countryCode = selectedDestination
            ? selectedDestination.country_code
            : '';
        const heavy = profile.weight_kg > 3500;
        const rules = {{
            pl: heavy
                ? 'Польща: для понад 3,5 т перевірте e-TOLL; ' +
                    'окремі ділянки A1, A2 та A4 можуть бути платними.'
                : 'Польща: окремі ділянки A1, A2 та A4 платні. ' +
                    'Якщо маршрут іде по A2, оплату треба перевірити ' +
                    'перед виїздом.',
            de: heavy
                ? 'Німеччина: для понад 3,5 т діє Toll Collect.'
                : 'Німеччина: загальної віньєтки до 3,5 т немає.',
            at: heavy
                ? 'Австрія: потрібен GO-Box.'
                : 'Австрія: потрібна електронна віньєтка.',
            cz: heavy
                ? 'Чехія: потрібна система MYTO CZ.'
                : 'Чехія: потрібна електронна віньєтка.',
            sk: heavy
                ? 'Словаччина: потрібна система eMyto.'
                : 'Словаччина: потрібна електронна віньєтка.',
            hu: heavy
                ? 'Угорщина: потрібна система HU-GO.'
                : 'Угорщина: потрібна e-Matrica.',
            si: heavy
                ? 'Словенія: потрібна система DarsGo.'
                : 'Словенія: потрібна e-vignette 2A або 2B.',
            ch: heavy
                ? 'Швейцарія: діє збір для важкого транспорту.'
                : 'Швейцарія: потрібна віньєтка.',
            ro: 'Румунія: потрібна Rovinieta відповідної категорії.',
            bg: heavy
                ? 'Болгарія: потрібен маршрутний або кілометровий збір.'
                : 'Болгарія: потрібна електронна віньєтка.',
            be: heavy
                ? 'Бельгія: для понад 3,5 т потрібен Viapass.'
                : 'Бельгія: загальної віньєтки до 3,5 т немає.',
            fr: 'Франція: оплата за окремі автостради, мости й тунелі.',
            it: 'Італія: оплата переважно за конкретні автостради.',
            es: 'Іспанія: більшість доріг безплатні, є платні ділянки.',
            pt: 'Португалія: є електронні й звичайні платні ділянки.'
        }};
        return rules[countryCode] ||
            'Перевірте правила віньєтки для країни призначення.';
    }}

    async function searchAddress() {{
        const query = destinationInput.value.trim();

        selectedDestination = null;
        buildRouteButton.disabled = true;
        addressResults.replaceChildren();

        if (query.length < 3) {{
            addressResults.hidden = false;
            addressResults.textContent =
                'Введіть щонайменше 3 символи.';
            return;
        }}

        addressSearchButton.disabled = true;
        addressSearchButton.textContent = 'Шукаю...';
        addressResults.hidden = false;
        addressResults.textContent = 'Шукаю адресу...';

        try {{
            const response = await fetch(
                '/api/geocode?q=' + encodeURIComponent(query),
                {{headers: {{'Accept': 'application/json'}}}}
            );
            const data = await response.json();

            if (!response.ok) {{
                throw new Error(
                    data.error || 'Пошук тимчасово недоступний.'
                );
            }}

            addressResults.replaceChildren();

            if (!data.results || !data.results.length) {{
                addressResults.textContent =
                    'Адресу не знайдено. Уточніть місто або вулицю.';
                return;
            }}

            if (data.results.length === 1) {{
                selectRouteDestination(data.results[0]);
                return;
            }}

            const resultsHint = document.createElement('div');
            resultsHint.className = 'small';
            resultsHint.textContent =
                'Виберіть потрібну адресу зі списку:';
            addressResults.appendChild(resultsHint);

            data.results.forEach(function(result) {{
                const resultButton = document.createElement('button');
                resultButton.type = 'button';
                resultButton.className = 'gps-address-result';
                resultButton.textContent = result.name;
                resultButton.addEventListener('click', function() {{
                    selectRouteDestination(result);
                }});
                addressResults.appendChild(resultButton);
            }});
        }} catch (error) {{
            addressResults.textContent =
                error.message || 'Пошук тимчасово недоступний.';
        }} finally {{
            addressSearchButton.disabled = false;
            addressSearchButton.textContent = 'Шукати';
        }}
    }}

    async function buildPlannedRoute() {{
        if (!selectedDestination) {{
            measureResult.textContent =
                'Спочатку знайдіть і виберіть адресу.';
            return;
        }}

        const vehicle = vehicles.find(function(item) {{
            return item.id === vehicleSelect.value;
        }});

        if (!vehicle) {{
            measureResult.textContent =
                'Для автомобіля немає актуальних GPS-координат.';
            return;
        }}

        removeMeasurementLayers();
        removePlannedRoute();
        measureMode = false;
        measureButton.classList.remove('active');
        buildRouteButton.disabled = true;
        buildRouteButton.textContent = 'Будую маршрут...';
        measureResult.textContent =
            'Будую маршрут від ' + vehicle.name + '...';

        const destinationPoint = L.latLng(
            selectedDestination.latitude,
            selectedDestination.longitude
        );
        destinationMarker = L.marker(destinationPoint)
            .addTo(map)
            .bindPopup(selectedDestination.name);

        const avoidTolls = selectedRouteAvoidsTolls();
        const vehicleProfile = selectedVehicleProfile();

        try {{
            const response = await fetch('/api/route', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{
                    origin: {{
                        latitude: vehicle.latitude,
                        longitude: vehicle.longitude
                    }},
                    destination: {{
                        latitude: selectedDestination.latitude,
                        longitude: selectedDestination.longitude
                    }},
                    avoid_tolls: avoidTolls,
                    vehicle_profile: vehicleProfile
                }})
            }});
            const routeData = await response.json();

            if (!response.ok) {{
                throw new Error(
                    routeData.error || 'Маршрут недоступний.'
                );
            }}

            if (!routeData.points || !routeData.points.length) {{
                throw new Error('Маршрут не знайдено.');
            }}

            plannedRouteLayer = L.polyline(routeData.points, {{
                color: avoidTolls ? '#16865a' : '#e4552d',
                weight: 6,
                opacity: .9
            }}).addTo(map);

            map.fitBounds(
                plannedRouteLayer.getBounds(),
                {{padding: [45, 45]}}
            );

            const distanceKm = routeData.distance_m / 1000;
            const fuelConsumption = Math.max(
                0,
                Number(fuelConsumptionInput.value) || 0
            );
            const fuelPrice = Math.max(
                0,
                Number(fuelPriceInput.value) || 0
            );
            const fuelLitres =
                distanceKm * fuelConsumption / 100;
            const fuelCost = fuelLitres * fuelPrice;
            const consumptionSource =
                fuelConsumptionInput.dataset.source === 'navirec'
                ? 'Navirec, середня за 14 днів'
                : 'норматив для категорії';

            measureResult.innerHTML =
                '<strong>' + vehicle.name + '</strong><br>' +
                'Відстань: <strong>' +
                distanceKm.toFixed(1) +
                ' км</strong><br>Час у дорозі: ' +
                formatDuration(routeData.duration_s) +
                '<br>Орієнтовне прибуття: ' +
                formatArrival(routeData.duration_s) +
                '<br>Паливо: <strong>' +
                fuelLitres.toFixed(1) + ' л</strong> × ' +
                fuelPrice.toFixed(2) + ' ' +
                fuelCurrencySelect.value +
                ' = <strong>' + fuelCost.toFixed(2) + ' ' +
                fuelCurrencySelect.value + '</strong>' +
                '<br><span class="small">Витрата: ' +
                fuelConsumption.toFixed(1) +
                ' л/100 км (' + consumptionSource + ')</span>' +
                '<br><strong>' +
                formatTollInformation(routeData) +
                '</strong><br>' +
                destinationRoadRule(vehicleProfile);
        }} catch (error) {{
            measureResult.textContent =
                error.message ||
                'Не вдалося прокласти автомобільний маршрут.';
        }} finally {{
            buildRouteButton.disabled = false;
            buildRouteButton.textContent = 'Прокласти маршрут';
        }}
    }}

    addressSearchButton.addEventListener('click', searchAddress);
    destinationInput.addEventListener('keydown', function(event) {{
        if (event.key === 'Enter') {{
            event.preventDefault();
            searchAddress();
        }}
    }});
    destinationInput.addEventListener('input', function() {{
        selectedDestination = null;
        buildRouteButton.disabled = true;
    }});
    buildRouteButton.addEventListener('click', buildPlannedRoute);

    measureButton.addEventListener('click', function() {{
        removePlannedRoute();
        removeMeasurementLayers();
        measureMode = true;
        measureButton.classList.add('active');
        measureResult.textContent = 'Клікніть першу точку на карті.';
    }});

    clearButton.addEventListener('click', clearMapRoutes);

    map.on('click', async function(event) {{
        if (!measureMode) {{
            return;
        }}

        const point = event.latlng;
        measurePoints.push(point);

        const pointLabel = measurePoints.length === 1 ? 'A' : 'B';
        const pointMarker = L.circleMarker(point, {{
            radius: 8,
            color: '#ffffff',
            weight: 3,
            fillColor: measurePoints.length === 1
                ? '#087f8c'
                : '#e4552d',
            fillOpacity: 1
        }}).addTo(map).bindTooltip(
            pointLabel,
            {{permanent: true, direction: 'top'}}
        );
        measureMarkers.push(pointMarker);

        if (measurePoints.length === 1) {{
            measureResult.textContent = 'Тепер клікніть другу точку.';
            return;
        }}

        measureMode = false;
        measureButton.classList.remove('active');
        measureResult.textContent = 'Будую автомобільний маршрут...';

        const firstPoint = measurePoints[0];
        const secondPoint = measurePoints[1];
        const straightKm = map.distance(
            firstPoint,
            secondPoint
        ) / 1000;

        const routeUrl =
            'https://router.project-osrm.org/route/v1/driving/' +
            firstPoint.lng + ',' + firstPoint.lat + ';' +
            secondPoint.lng + ',' + secondPoint.lat +
            '?overview=full&geometries=geojson';

        try {{
            const response = await fetch(routeUrl);
            if (!response.ok) {{
                throw new Error('route service error');
            }}

            const routeData = await response.json();
            if (!routeData.routes || !routeData.routes.length) {{
                throw new Error('route not found');
            }}

            const route = routeData.routes[0];
            const routePoints = route.geometry.coordinates.map(
                function(coordinate) {{
                    return [coordinate[1], coordinate[0]];
                }}
            );

            measureLayer = L.polyline(routePoints, {{
                color: '#087f8c',
                weight: 5,
                opacity: .88
            }}).addTo(map);

            map.fitBounds(
                measureLayer.getBounds(),
                {{padding: [45, 45]}}
            );

            measureResult.innerHTML =
                '<strong>Дорогами: ' +
                (route.distance / 1000).toFixed(1) +
                ' км</strong><br>Приблизний час: ' +
                formatDuration(route.duration);
        }} catch (error) {{
            measureLayer = L.polyline(
                [firstPoint, secondPoint],
                {{
                    color: '#e4552d',
                    weight: 4,
                    dashArray: '8, 8',
                    opacity: .85
                }}
            ).addTo(map);

            measureResult.innerHTML =
                '<strong>По прямій: ' +
                straightKm.toFixed(1) +
                ' км</strong><br>' +
                'Автомобільний маршрут зараз недоступний.';
        }}
    }});
    </script>
    """.format(
        markers=marker_json,
        selected=json.dumps(selected_id),
        lat=center_lat,
        lon=center_lon
    )

    return page(
        "GPS",
        body,
        "gps"
    )


@app.route("/history")
def history():
    selected_id = normalize_vehicle_id(
        request.args.get("vehicle", "")
    )

    if not selected_id:
        selected_id = VEHICLES[0]["id"]

    vehicle = vehicle_by_id(selected_id)

    if not vehicle:
        vehicle = VEHICLES[0]
        selected_id = vehicle["id"]

    date_string = request.args.get(
        "date",
        datetime.now(POLAND_TZ).strftime("%Y-%m-%d")
    )

    result = get_vehicle_history(
        selected_id,
        date_string
    )

    points = result["points"]

    totals = get_vehicle_timeline_totals(
        selected_id,
        date_string
    )

    metrics = calculate_history_metrics(points)

    if points:
        start_point = points[0]
        end_point = points[-1]

        speed_values = []

        for point in points:
            value = safe_float(
                point.get("speed")
            )

            if value is not None:
                speed_values.append(value)

        max_speed = max(
            speed_values,
            default=None
        )

        fuel_values = []

        for point in points:
            value = safe_float(
                point.get("fuel_level")
            )

            if value is not None:
                fuel_values.append(value)

        fuel_start = (
            fuel_values[0]
            if fuel_values else None
        )

        fuel_end = (
            fuel_values[-1]
            if fuel_values else None
        )

    else:
        start_point = {}
        end_point = {}
        max_speed = None
        fuel_start = None
        fuel_end = None

    distance_value = (
        metrics["distance_km"]
        if points else None
    )

    points_count_text = format_number(
        len(points),
        0
    )

    start_text = format_time(
        start_point.get("time")
    )

    end_text = format_time(
        end_point.get("time")
    )

    if distance_value is not None:
        distance_text = (
            format_number(
                distance_value,
                1
            )
            + " км"
        )
    else:
        distance_text = "—"

    if max_speed is not None:
        max_speed_text = (
            format_number(
                max_speed,
                0
            )
            + " км/год"
        )
    else:
        max_speed_text = "—"

    if fuel_start is not None:
        fuel_start_text = (
            format_number(
                fuel_start,
                1
            )
            + "%"
        )
    else:
        fuel_start_text = "—"

    if fuel_end is not None:
        fuel_end_text = (
            format_number(
                fuel_end,
                1
            )
            + "%"
        )
    else:
        fuel_end_text = "—"

    driving_distance_text = (
        format_number(distance_value, 2) + " км"
        if distance_value is not None
        else "—"
    )
    driving_time_text = (
        format_duration(metrics["driving_seconds"])
        if points else "—"
    )
    parking_time_text = (
        format_duration(metrics["parking_seconds"])
        if points else "—"
    )
    idling_time_text = (
        format_duration(metrics["idling_seconds"])
        if points else "—"
    )
    fuel_per_100_text = "—"

    if totals:
        fuel_per_100 = get_total_number(
            totals,
            [
                "fuel_per_100_km",
                "fuelPer100Km",
                "fuel_consumption"
            ]
        )

        if fuel_per_100 is not None:
            fuel_per_100_text = (
                format_number(
                    fuel_per_100,
                    2
                )
                + " л/100 км"
            )

    vehicle_options = []

    for item in VEHICLES:
        selected = (
            " selected"
            if item["id"] == selected_id
            else ""
        )

        vehicle_options.append(
            """
            <option value="{id}"{selected}>
                {name}
            </option>
            """.format(
                id=item["id"],
                selected=selected,
                name=item["name"]
            )
        )

    route_points = []

    for point in points:
        route_points.append({
            "lat": point["latitude"],
            "lon": point["longitude"],
            "time": format_time(
                point.get("time")
            ),
            "speed": safe_float(
                point.get("speed")
            ),
            "fuel": safe_float(
                point.get("fuel_level")
            ),
            "activity": point.get(
                "activity"
            ) or ""
        })

    route_json = json.dumps(
        route_points,
        ensure_ascii=False
    )

    table_rows = []

    for point in points[-100:]:
        speed = safe_float(
            point.get("speed")
        )

        fuel = safe_float(
            point.get("fuel_level")
        )

        if speed is not None:
            speed_text = (
                format_number(speed, 0)
                + " км/год"
            )
        else:
            speed_text = "—"

        if fuel is not None:
            fuel_text = (
                format_number(fuel, 1)
                + "%"
            )
        else:
            fuel_text = "—"

        table_rows.append(
            """
            <tr>

                <td>
                    {time}
                </td>

                <td>
                    {activity}
                </td>

                <td>
                    {speed}
                </td>

                <td>
                    {fuel}
                </td>

                <td>
                    {lat:.6f}, {lon:.6f}
                </td>

            </tr>
            """.format(
                time=format_time(
                    point.get("time")
                ),
                activity=(
                    point.get("activity")
                    or "—"
                ),
                speed=speed_text,
                fuel=fuel_text,
                lat=point["latitude"],
                lon=point["longitude"]
            )
        )

    error_block = ""

    if not result["ok"]:
        error_block = """
        <div class="card">
            <p class="error">
                {error}
            </p>
        </div>
        """.format(
            error=result["error"]
        )

    body = """
    <div class="card">

        <form method="get">

            <div class="form-grid">

                <div>
                    <label>Автомобіль</label>

                    <select name="vehicle">
                        {vehicle_options}
                    </select>
                </div>

                <div>
                    <label>Дата</label>

                    <input
                        type="date"
                        name="date"
                        value="{date}"
                    >
                </div>

                <div>
                    <button type="submit">
                        Показати історію
                    </button>
                </div>

            </div>

        </form>

    </div>

    {error_block}

    <div class="grid">

        <div class="stat">
            <div class="label">GPS-точок</div>
            <div class="value">{points}</div>
        </div>

        <div class="stat">
            <div class="label">Початок</div>
            <div class="value">{start}</div>
        </div>

        <div class="stat">
            <div class="label">Кінець</div>
            <div class="value">{end}</div>
        </div>

        <div class="stat">
            <div class="label">Відстань</div>
            <div class="value">{distance}</div>
        </div>

        <div class="stat">
            <div class="label">Макс. швидкість</div>
            <div class="value">{max_speed}</div>
        </div>

        <div class="stat">
            <div class="label">Паливо на початку</div>
            <div class="value">{fuel_start}</div>
        </div>

        <div class="stat">
            <div class="label">Паливо в кінці</div>
            <div class="value">{fuel_end}</div>
        </div>

        <div class="stat">
            <div class="label">Рух</div>
            <div class="value">{driving_distance}</div>
        </div>

        <div class="stat">
            <div class="label">Час руху</div>
            <div class="value">{driving_time}</div>
        </div>

        <div class="stat">
            <div class="label">Стоянка</div>
            <div class="value">{parking_time}</div>
        </div>

        <div class="stat">
            <div class="label">Холостий хід</div>
            <div class="value">{idling_time}</div>
        </div>

        <div class="stat">
            <div class="label">Витрата</div>
            <div class="value">{fuel_per_100}</div>
        </div>

    </div>

    <div class="card">
        <div id="map"></div>
    </div>

    <div class="card">

        <h3>
            Останні 100 GPS-точок
        </h3>

        <div style="overflow:auto">

            <table>

                <thead>
                    <tr>
                        <th>Час</th>
                        <th>Стан</th>
                        <th>Швидкість</th>
                        <th>Паливо</th>
                        <th>Координати</th>
                    </tr>
                </thead>

                <tbody>
                    {rows}
                </tbody>

            </table>

        </div>

    </div>

    <link
        rel="stylesheet"
        href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    >

    <script
        src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js">
    </script>

    <script>
    const points = {route_json};

    const map = L.map('map');

    L.tileLayer(
        'https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
        {{
            maxZoom: 19,
            attribution: '&copy; OpenStreetMap'
        }}
    ).addTo(map);

    if (points.length > 0) {{

        const latlngs = points.map(
            function(point) {{
                return [
                    point.lat,
                    point.lon
                ];
            }}
        );

        const line = L.polyline(
            latlngs,
            {{weight: 4}}
        ).addTo(map);

        points.forEach(
            function(point, index) {{

                if (
                    index === 0 ||
                    index === points.length - 1
                ) {{

                    L.marker([
                        point.lat,
                        point.lon
                    ])
                    .addTo(map)
                    .bindPopup(
                        point.time +
                        '<br>' +
                        'Швидкість: ' +
                        (
                            point.speed === null
                            ? '—'
                            : point.speed.toFixed(0)
                              + ' км/год'
                        ) +
                        '<br>Паливо: ' +
                        (
                            point.fuel === null
                            ? '—'
                            : point.fuel.toFixed(1)
                              + '%'
                        )
                    );
                }}
            }}
        );

        map.fitBounds(
            line.getBounds(),
            {{padding: [20, 20]}}
        );

    }} else {{

        map.setView(
            [51.1, 17.0],
            6
        );
    }}
    </script>
    """.format(
        vehicle_options="".join(
            vehicle_options
        ),
        date=date_string,
        error_block=error_block,
        points=points_count_text,
        start=start_text,
        end=end_text,
        distance=distance_text,
        max_speed=max_speed_text,
        fuel_start=fuel_start_text,
        fuel_end=fuel_end_text,
        driving_distance=driving_distance_text,
        driving_time=driving_time_text,
        parking_time=parking_time_text,
        idling_time=idling_time_text,
        fuel_per_100=fuel_per_100_text,
        rows="".join(table_rows),
        route_json=route_json
    )

    return page(
        f"Історія — {vehicle['name']}",
        body,
        "history"
    )


@app.route("/fuel")
def fuel():
    states = get_vehicle_states()
    state_map = state_map_by_vehicle(states)

    rows = []

    for vehicle in VEHICLES:
        state = state_map.get(vehicle["id"])

        if state:
            fuel = safe_float(
                state.get("fuel_level")
            )

            if fuel is not None:
                fuel_text = (
                    format_number(fuel, 1)
                    + "%"
                )
            else:
                fuel_text = "—"

        else:
            fuel_text = "—"

        rows.append(
            """
            <tr>
                <td>{name}</td>
                <td>{fuel}</td>
            </tr>
            """.format(
                name=vehicle["name"],
                fuel=fuel_text
            )
        )

    body = """
    <div class="card">

        <p class="small">
            Тут поки показується поточний рівень
            палива з Navirec.
            Збільшення рівня ще не вважаємо
            автоматично заправкою.
        </p>

        <table>

            <thead>
                <tr>
                    <th>Автомобіль</th>
                    <th>Паливо</th>
                </tr>
            </thead>

            <tbody>
                {rows}
            </tbody>

        </table>

    </div>
    """.format(
        rows="".join(rows)
    )

    return page(
        "Паливо",
        body,
        "fuel"
    )


@app.route("/tachograph")
@app.route("/tachograph-test")
def tachograph():
    vehicle_states = get_vehicle_states()
    vehicle_state_map = state_map_by_vehicle(
        vehicle_states
    )

    driver_states_result = get_driver_states_result()
    drivers_result = get_drivers_result()
    cards_result = get_tachograph_cards_result()

    driver_states = driver_states_result["items"]
    drivers = drivers_result["items"]
    cards = cards_result["items"]

    driver_states_by_vehicle = {}
    driver_states_by_driver = {}

    for state in driver_states:
        vehicle_id = normalize_api_id(
            state.get("vehicle")
        )
        driver_id = normalize_api_id(
            state.get("driver")
        )

        if vehicle_id:
            driver_states_by_vehicle[vehicle_id] = state

        if driver_id:
            driver_states_by_driver[driver_id] = state

    drivers_by_id = {
        normalize_api_id(driver.get("id") or driver.get("url")): driver
        for driver in drivers
        if normalize_api_id(
            driver.get("id") or driver.get("url")
        )
    }

    cards_by_driver = {}
    cards_by_number = {}

    for card in cards:
        driver_id = normalize_api_id(
            card.get("driver")
        )
        card_number = str(
            card.get("number") or ""
        ).strip()

        if driver_id:
            cards_by_driver[driver_id] = card

        if card_number:
            cards_by_number[card_number] = card

    api_errors = []

    for title, result in (
        ("стани водіїв", driver_states_result),
        ("список водіїв", drivers_result),
        ("картки тахографа", cards_result)
    ):
        if not result["ok"]:
            api_errors.append(
                f"Не вдалося отримати {title}: "
                f"{result['error']}"
            )

    vehicle_blocks = []
    vehicles_with_cards = 0
    warning_count = 0

    for vehicle in VEHICLES:
        vehicle_id = vehicle["id"]
        vehicle_state = vehicle_state_map.get(
            vehicle_id,
            {}
        )

        driver_id = normalize_api_id(
            vehicle_state.get("driver")
        )

        driver_state = driver_states_by_vehicle.get(
            vehicle_id
        )

        if not driver_state and driver_id:
            driver_state = driver_states_by_driver.get(
                driver_id
            )

        if driver_state:
            driver_id = normalize_api_id(
                driver_state.get("driver")
            ) or driver_id

        combined = merge_tachograph_state(
            vehicle_state,
            driver_state
        )

        driver = drivers_by_id.get(
            driver_id,
            {}
        )

        card_number = str(
            get_first_value(
                combined,
                [
                    "driver_1_card_id",
                    "driver_code"
                ]
            )
            or ""
        ).strip()

        card = cards_by_driver.get(driver_id)

        if not card and card_number:
            card = cards_by_number.get(
                card_number,
                {}
            )

        card = card or {}

        if not card_number:
            card_number = str(
                card.get("number") or ""
            ).strip()

        driver_name = str(
            driver.get("name") or ""
        ).strip()

        if not driver_name:
            first_name = str(
                combined.get("driver_name") or ""
            ).strip()
            surname = str(
                combined.get("driver_surname") or ""
            ).strip()
            driver_name = " ".join(
                part
                for part in (first_name, surname)
                if part
            )

        if not driver_name:
            driver_name = str(
                card.get("name") or ""
            ).strip()

        if not driver_name:
            driver_name = "Водія не визначено"

        working_state = get_first_value(
            combined,
            [
                "driver_working_state",
                "driver_1_working_state"
            ]
        )
        time_state = get_first_value(
            combined,
            [
                "driver_time_state",
                "driver_1_time_state"
            ]
        )
        card_present = get_first_value(
            combined,
            ["driver_1_card_present"]
        )

        if card_present is None and card_number:
            card_present = True

        if card_present:
            vehicles_with_cards += 1

        update_time = get_first_value(
            combined,
            ["time", "updated_at", "received_at"]
        )
        age_seconds = state_age_seconds(update_time)

        current_drive_remaining = get_duration_value(
            combined,
            [
                "driver_remaining_current_driving_time",
                "driver_1_remaining_current_driving_time"
            ]
        )
        daily_drive_remaining = get_duration_value(
            combined,
            [
                "driver_remaining_daily_driving_time",
                "driver_1_remaining_daily_driving_time"
            ]
        )
        shift_drive_remaining = get_duration_value(
            combined,
            [
                "driver_remaining_shift_driving_time",
                "driver_1_remaining_shift_driving_time"
            ]
        )
        weekly_drive_remaining = get_duration_value(
            combined,
            [
                "driver_remaining_weekly_driving_time",
                "driver_1_remaining_weekly_driving_time"
            ]
        )
        time_until_break = get_duration_value(
            combined,
            [
                "driver_time_until_next_break",
                "driver_1_time_until_next_break"
            ]
        )
        time_until_daily_rest = get_duration_value(
            combined,
            [
                "driver_time_until_next_daily_rest",
                "driver_1_time_until_next_daily_rest_period"
            ]
        )
        time_until_weekly_rest = get_duration_value(
            combined,
            [
                "driver_time_until_next_weekly_rest",
                "driver_1_time_until_next_weekly_rest_period"
            ]
        )
        daily_driving = get_duration_value(
            combined,
            [
                "driver_daily_driving_time",
                "driver_1_daily_driving_time"
            ]
        )
        weekly_driving = get_duration_value(
            combined,
            [
                "driver_weekly_driving_time",
                "driver_1_weekly_driving_time"
            ]
        )
        two_weekly_driving = get_duration_value(
            combined,
            [
                "driver_two_weekly_driving_time",
                "driver_1_two_weekly_driving_time"
            ]
        )
        daily_work = get_duration_value(
            combined,
            [
                "driver_daily_work_time",
                "driver_1_daily_work_time"
            ]
        )
        current_rest_remaining = get_duration_value(
            combined,
            [
                "driver_remaining_current_rest_time",
                "driver_1_remaining_current_rest_time"
            ]
        )
        break_time = get_duration_value(
            combined,
            [
                "driver_break_time",
                "driver_cumulative_break_time",
                "driver_1_cumulative_break_time"
            ]
        )

        warnings = []

        if not driver_state:
            warnings.append((
                "warning",
                "Navirec не повернув повний стан водія. "
                "Показані лише дані, наявні в автомобілі."
            ))

        if card_present is False:
            warnings.append((
                "error",
                "Картка водія не вставлена в тахограф."
            ))

        if age_seconds is None:
            warnings.append((
                "warning",
                "Немає часу останнього оновлення тахографа."
            ))
        elif age_seconds > 1800:
            warnings.append((
                "error",
                "Дані тахографа застарілі: останнє "
                f"оновлення {format_time(update_time)}."
            ))

        try:
            numeric_time_state = int(time_state)
        except (TypeError, ValueError):
            numeric_time_state = None

        if numeric_time_state not in (None, 0):
            warnings.append((
                "error",
                tachograph_time_state_label(time_state)
            ))

        for label, value in (
            ("безперервного керування", current_drive_remaining),
            ("денного керування", daily_drive_remaining),
            ("часу до обов'язкової перерви", time_until_break),
            ("часу до денного відпочинку", time_until_daily_rest)
        ):
            if value is not None and value <= 1800:
                warnings.append((
                    "warning",
                    f"Залишилося мало {label}: "
                    f"{format_duration_short(value)}."
                ))

        valid_until = card.get("valid_until")

        if valid_until:
            try:
                valid_date = datetime.fromisoformat(
                    str(valid_until)[:10]
                ).date()
                days_left = (
                    valid_date
                    - datetime.now(POLAND_TZ).date()
                ).days

                if days_left < 0:
                    warnings.append((
                        "error",
                        "Термін дії картки водія закінчився."
                    ))
                elif days_left <= 30:
                    warnings.append((
                        "warning",
                        "Термін дії картки закінчується "
                        f"через {days_left} дн."
                    ))
            except ValueError:
                pass

        warning_count += len(warnings)

        if warnings:
            warning_html = "".join(
                """
                <div class="alert alert-{kind}">
                    {text}
                </div>
                """.format(
                    kind=(
                        "error"
                        if kind == "error"
                        else "warning"
                    ),
                    text=html_text(text)
                )
                for kind, text in warnings
            )
        else:
            warning_html = """
            <div class="alert alert-ok">
                Активних попереджень немає.
            </div>
            """

        card_status = (
            "Вставлена"
            if card_present is True
            else "Не вставлена"
            if card_present is False
            else "Немає даних"
        )

        second_card_present = combined.get(
            "driver_2_card_present"
        )
        second_card_number = combined.get(
            "driver_2_card_id"
        )
        second_state = combined.get(
            "driver_2_working_state"
        )

        second_driver_block = ""

        if second_card_present or second_card_number:
            second_driver_block = """
            <div class="alert alert-warning">
                <strong>Другий водій:</strong>
                картка {card}, стан — {state}.
            </div>
            """.format(
                card=html_text(second_card_number),
                state=html_text(
                    working_state_label(second_state)
                )
            )

        vehicle_blocks.append(
            """
            <div class="card">

                <div class="vehicle-header">
                    <h2>{vehicle}</h2>
                    <span class="badge {state_class}">
                        {working_state}
                    </span>
                </div>

                <div class="detail-grid">

                    <div class="detail">
                        <div class="label">Водій</div>
                        <div class="value">{driver}</div>
                    </div>

                    <div class="detail">
                        <div class="label">Картка водія</div>
                        <div class="value">{card_number}</div>
                        <div class="small">{card_status}</div>
                    </div>

                    <div class="detail">
                        <div class="label">Оновлено</div>
                        <div class="value">{updated}</div>
                    </div>

                    <div class="detail">
                        <div class="label">Стан часу</div>
                        <div class="value">{time_state}</div>
                    </div>

                    <div class="detail">
                        <div class="label">До наступної перерви</div>
                        <div class="value">{until_break}</div>
                    </div>

                    <div class="detail">
                        <div class="label">Залишок безперервного керування</div>
                        <div class="value">{current_remaining}</div>
                    </div>

                    <div class="detail">
                        <div class="label">Залишок керування сьогодні</div>
                        <div class="value">{daily_remaining}</div>
                    </div>

                    <div class="detail">
                        <div class="label">Залишок у зміні</div>
                        <div class="value">{shift_remaining}</div>
                    </div>

                    <div class="detail">
                        <div class="label">Залишок керування цього тижня</div>
                        <div class="value">{weekly_remaining}</div>
                    </div>

                    <div class="detail">
                        <div class="label">До денного відпочинку</div>
                        <div class="value">{until_daily_rest}</div>
                    </div>

                    <div class="detail">
                        <div class="label">До тижневого відпочинку</div>
                        <div class="value">{until_weekly_rest}</div>
                    </div>

                    <div class="detail">
                        <div class="label">Керування сьогодні</div>
                        <div class="value">{daily_driving}</div>
                    </div>

                    <div class="detail">
                        <div class="label">Керування за тиждень</div>
                        <div class="value">{weekly_driving}</div>
                    </div>

                    <div class="detail">
                        <div class="label">Керування за два тижні</div>
                        <div class="value">{two_weekly_driving}</div>
                    </div>

                    <div class="detail">
                        <div class="label">Робота сьогодні</div>
                        <div class="value">{daily_work}</div>
                    </div>

                    <div class="detail">
                        <div class="label">Перерва / відпочинок</div>
                        <div class="value">{break_time}</div>
                    </div>

                    <div class="detail">
                        <div class="label">Залишок поточного відпочинку</div>
                        <div class="value">{rest_remaining}</div>
                    </div>

                    <div class="detail">
                        <div class="label">Картка дійсна до</div>
                        <div class="value">{valid_until}</div>
                    </div>

                </div>

                {second_driver}
                {warnings}

            </div>
            """.format(
                vehicle=html_text(vehicle["name"]),
                state_class=working_state_class(
                    working_state
                ),
                working_state=html_text(
                    working_state_label(working_state)
                ),
                driver=html_text(driver_name),
                card_number=html_text(card_number),
                card_status=html_text(card_status),
                updated=html_text(format_time(update_time)),
                time_state=html_text(
                    tachograph_time_state_label(time_state)
                ),
                until_break=format_duration_short(
                    time_until_break
                ),
                current_remaining=format_duration_short(
                    current_drive_remaining
                ),
                daily_remaining=format_duration_short(
                    daily_drive_remaining
                ),
                shift_remaining=format_duration_short(
                    shift_drive_remaining
                ),
                weekly_remaining=format_duration_short(
                    weekly_drive_remaining
                ),
                until_daily_rest=format_duration_short(
                    time_until_daily_rest
                ),
                until_weekly_rest=format_duration_short(
                    time_until_weekly_rest
                ),
                daily_driving=format_duration_short(
                    daily_driving
                ),
                weekly_driving=format_duration_short(
                    weekly_driving
                ),
                two_weekly_driving=format_duration_short(
                    two_weekly_driving
                ),
                daily_work=format_duration_short(
                    daily_work
                ),
                break_time=format_duration_short(
                    break_time
                ),
                rest_remaining=format_duration_short(
                    current_rest_remaining
                ),
                valid_until=html_text(
                    format_date(valid_until)
                ),
                second_driver=second_driver_block,
                warnings=warning_html
            )
        )

    error_block = ""

    if api_errors:
        error_block = """
        <div class="card">
            <h3 class="section-title">Помилки Navirec</h3>
            {errors}
        </div>
        """.format(
            errors="".join(
                "<div class='alert alert-error'>"
                + html_text(error)
                + "</div>"
                for error in api_errors
            )
        )

    body = """
    <div class="card">
        <p>
            Дані отримуються безпосередньо з Navirec:
            водії, картки та last_driver_states.
        </p>
        <p class="small">
            Час тахографа не вираховується з GPS.
            Саме ці значення надалі використовуватимуться
            для перевірки, чи можна брати рейс Trans.eu.
        </p>

        <a class="button" href="/tachograph-debug">
            Технічна перевірка даних
        </a>
    </div>

    {error_block}

    <div class="grid">
        <div class="stat">
            <div class="label">Автомобілі</div>
            <div class="value">{vehicles}</div>
        </div>
        <div class="stat">
            <div class="label">Картка вставлена</div>
            <div class="value">{cards_present}</div>
        </div>
        <div class="stat">
            <div class="label">Станів водіїв Navirec</div>
            <div class="value">{driver_states}</div>
        </div>
        <div class="stat">
            <div class="label">Попередження</div>
            <div class="value">{warnings}</div>
        </div>
    </div>

    <div style="height:18px"></div>

    {vehicle_blocks}
    """.format(
        error_block=error_block,
        vehicles=len(VEHICLES),
        cards_present=vehicles_with_cards,
        driver_states=len(driver_states),
        warnings=warning_count,
        vehicle_blocks="".join(vehicle_blocks)
    )

    return page(
        "Тахограф і час водіїв",
        body,
        "tachograph"
    )


@app.route("/tachograph-debug")
def tachograph_debug():
    vehicle_states = get_vehicle_states()
    driver_states_result = get_driver_states_result()
    drivers_result = get_drivers_result()
    cards_result = get_tachograph_cards_result()

    debug_data = {
        "account": NAVIREC_ACCOUNT_ID,
        "vehicle_states": vehicle_states,
        "driver_states": driver_states_result,
        "drivers": drivers_result,
        "tachograph_cards": cards_result
    }

    body = """
    <div class="card">
        <p class="small">
            Тут показані сирі дані Navirec без секретного токена.
            Вони потрібні лише для перевірки полів тахографа.
        </p>

        <a class="button" href="/tachograph">
            Назад до тахографа
        </a>

        <pre style="white-space:pre-wrap;overflow:auto">{data}</pre>
    </div>
    """.format(
        data=html_text(
            json.dumps(
                debug_data,
                ensure_ascii=False,
                indent=2
            )[:100000]
        )
    )

    return page(
        "Тахограф — технічні дані",
        body,
        "tachograph"
    )


@app.route("/navirec-debug")
def navirec_debug():
    states = get_vehicle_states()

    safe_states = []

    for state in states[:20]:
        safe_states.append(dict(state))

    body = """
    <div class="card">

        <p>
            <strong>NAVIREC_TOKEN:</strong>
            {token}
        </p>

        <p>
            <strong>Account:</strong>
            {account}
        </p>

        <p>
            <strong>Кількість state:</strong>
            {count}
        </p>

        <pre
            style="
                white-space:pre-wrap;
                overflow:auto
            "
        >{data}</pre>

    </div>
    """.format(
        token=(
            "налаштований"
            if NAVIREC_TOKEN
            else "НЕ НАЛАШТОВАНИЙ"
        ),
        account=NAVIREC_ACCOUNT_ID,
        count=len(states),
        data=json.dumps(
            safe_states,
            ensure_ascii=False,
            indent=2
        )[:30000]
    )

    return page(
        "Navirec debug",
        body
    )


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "company": COMPANY_NAME,
        "company_id": COMPANY_ID,
        "vehicles": len(VEHICLES),
        "navirec_token_configured": bool(
            NAVIREC_TOKEN
        )
    })


if __name__ == "__main__":
    port = int(
        os.environ.get(
            "PORT",
            "10000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
