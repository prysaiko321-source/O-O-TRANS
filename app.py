import os
import json
import math
import hmac
import secrets
import base64
from datetime import datetime, timezone
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

COMPANY_NAME = os.environ.get("COMPANY_NAME", "O&O TRANS")
COMPANY_ID = os.environ.get("COMPANY_ID", "O&O-TRANS")
PLATFORM_NAME = "TRANVIQ"
PLATFORM_TAGLINE = "Transport Intelligence Platform"
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
        "history",
        "fuel",
        "tachograph"
    },
    "driver": {
        "driver_dashboard"
    }
}

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

    if role == "driver":
        nav_items = [
            ("driver", "/driver", "Мої рейси")
        ]
    elif role == "dispatcher":
        nav_items = [
            ("dispatcher", "/dispatcher", "Робоча панель"),
            ("vehicles", "/vehicles", "Автомобілі"),
            ("gps", "/gps", "GPS"),
            ("history", "/history", "Історія маршрутів"),
            ("fuel", "/fuel", "Паливо"),
            ("tachograph", "/tachograph", "Тахограф")
        ]
    elif role == "director":
        nav_items = [
            ("home", "/", "Головна"),
            ("vehicles", "/vehicles", "Автомобілі"),
            ("gps", "/gps", "GPS"),
            ("history", "/history", "Історія маршрутів"),
            ("fuel", "/fuel", "Паливо"),
            ("tachograph", "/tachograph", "Тахограф"),
            ("finance", "/finance", "Фінанси"),
            ("health", "/health", "Health")
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
        nav_links.append('<a href="/logout">Вийти</a>')

    nav = '<nav class="nav">{}</nav>'.format(
        "".join(nav_links)
    )

    role_badge = ""

    if role:
        role_badge = (
            '<div class="small" style="margin-top:5px;color:#dfe6e9">'
            'Роль: <strong>{}</strong>'
            '</div>'
        ).format(ROLE_LABELS.get(role, role))

    return """
<!doctype html>
<html lang="uk">
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
    max-width: 1600px;
    margin: 0 auto;
    padding: 22px;
}}

.powered-by {{
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

h1 {{
    margin-top: 0;
}}

.card {{
    background: white;
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
    background: white;
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
    background: #fbfcfd;
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

    .company-logo {{
        width: 52px;
        height: 52px;
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
}}
</style>
{extra_head}
</head>

<body>

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
                alt="Логотип {company}"
            >
            <div>
                <div class="company-caption">Компанія</div>
                <div class="company-name">{company}</div>
                {role_badge}
            </div>
        </div>
    </div>
    {nav}
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
        title=title,
        company=COMPANY_NAME,
        platform=PLATFORM_NAME,
        platform_tagline=PLATFORM_TAGLINE,
        nav=nav,
        role_badge=role_badge,
        body=body,
        extra_head=""
    )


@app.route("/assets/company-logo.jpg")
def company_logo_asset():
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
        <div class="grid">
            <div class="card">
                <h2>Директор</h2>
                <p>Повний доступ до всієї системи.</p>
                <a class="button" href="/login/director">Увійти</a>
            </div>
            <div class="card">
                <h2>Логіст</h2>
                <p>Рейси, автомобілі, GPS і робочі документи.</p>
                <a class="button" href="/login/dispatcher">Увійти</a>
            </div>
            <div class="card">
                <h2>Водій</h2>
                <p>Власні завдання, статуси рейсу та CMR.</p>
                <a class="button" href="/login/driver">Увійти</a>
            </div>
        </div>
        """
        return page("Виберіть вхід", body)

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
                "Цей вхід ще не налаштований адміністратором."
                "</p>"
            )
        else:
            error = (
                "<p class='error'>"
                "Неправильний логін або пароль."
                "</p>"
            )
    else:
        error = ""

    body = """
    <div class="card" style="max-width:420px">
        {error}

        <form method="post">

            <p>
                <label>Логін</label>
                <input
                    name="username"
                    autocomplete="username"
                >
            </p>

            <p>
                <label>Пароль</label>
                <input
                    name="password"
                    type="password"
                    autocomplete="current-password"
                >
            </p>

            <button type="submit">Увійти</button>

        </form>
    </div>
    """.format(error=error)

    return page(
        "Вхід: " + ROLE_LABELS[role],
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

        markers.append({
            "id": vehicle["id"],
            "name": vehicle["name"],
            "latitude": latitude,
            "longitude": longitude,
            "speed": speed,
            "fuel": fuel
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
    <div class="card">

        <p class="small">
            Показано поточні координати,
            які Navirec повертає через
            last_vehicle_states.
        </p>

    </div>

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
