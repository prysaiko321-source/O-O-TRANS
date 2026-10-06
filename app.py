import os
import json
import re
import math
import hmac
import secrets
import base64
import time
import threading
import uuid
from datetime import datetime, timezone, timedelta
from html import escape
from zoneinfo import ZoneInfo
from werkzeug.local import LocalProxy
import tenancy
from gps_providers import PROVIDERS, GPSError, list_vehicles as gps_list_vehicles, live_states as gps_live_states
from werkzeug.security import generate_password_hash, check_password_hash

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
app.permanent_session_lifetime = timedelta(days=30)

NAVIREC_API = "https://api.navirec.com"
NAVIREC_TOKEN = os.environ.get("NAVIREC_TOKEN", "")
NAVIREC_ACCOUNT_ID = os.environ.get(
    "NAVIREC_ACCOUNT_ID",
    "5c980074-7a71-4c9b-b5a8-a7c45163adf5"
)

# Shared last-good Navirec cache for tachograph views.
# This is intentionally small and in-memory: it prevents the dispatcher page
# from making another burst of identical Navirec requests when the driver page
# has already fetched the same data a few seconds earlier.
NAVIREC_LIST_CACHE_TTL = 55
NAVIREC_LIST_STALE_MAX = 15 * 60
_NAVIREC_LIST_CACHE = {}
_NAVIREC_LIST_CACHE_LOCK = threading.Lock()

_LAST_GOOD_TACHO_VEHICLE_STATES = []
_LAST_GOOD_TACHO_VEHICLE_STATES_AT = 0.0
_LAST_GOOD_TACHO_LOCK = threading.Lock()
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
DISPATCHER_USER = os.environ.get("DISPATCHER_USER", "").strip()
DISPATCHER_PASSWORD = os.environ.get("DISPATCHER_PASSWORD", "").strip()
DRIVER_USER = (
    os.environ.get("DRIVER_USER")
    or os.environ.get("DRIVER_LOGIN")
    or ""
).strip()
DRIVER_PASSWORD = (
    os.environ.get("DRIVER_PASSWORD")
    or os.environ.get("DRIVER_PASS")
    or ""
).strip()
DRIVER_DXF_USER = (os.environ.get("DRIVER_DXF_USER") or "dxf").strip()
DRIVER_DXF_PASSWORD = (os.environ.get("DRIVER_DXF_PASSWORD") or "DXF-739184").strip()
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


def clear_session_keep_language():
    language = current_language()
    session.clear()
    session["language"] = language

ROLE_HOME_ENDPOINTS = {
    "director": "home",
    "dispatcher": "tachograph",
    "driver": "driver_dashboard"
}

ROLE_ENDPOINTS = {
    "dispatcher": {
        "vehicles", "vehicle_page", "history",
        "gps",
        "geocode_search",
        "route_calculate",
        "delivery_stop_status",
        "delivery_route_storage",
        "delivery_routes_list",
        "api_live_vehicle_states",
        "tachograph",
        "road_payments", "trip_documents", "trip_documents_api", "trip_document_file", "trip_document_delete", "trip_documents_unread", "trip_documents_seen"
    },
    "driver": {
        "driver_dashboard",
        "delivery_route_storage",
        "delivery_route_queue",
        "delivery_routes_list",
        "delivery_stop_status",
        "api_live_vehicle_states",
        "api_driver_gps",
        "driver_fleet_visibility",
        "geocode_search",
        "route_calculate",
        "message_recipients",
        "internal_messages",
        "internal_messages_read",
        "road_payments", "trip_documents", "trip_documents_api", "trip_document_file", "trip_document_delete",
        "driver_tachograph_api"
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
        "name": "Renault Master SH 9203G",
        "plate": "SH 9203G"
    },
    {
        "id": "cbb121b6-34dd-41c6-974b-5b7aa3d9a1cb",
        "name": "Renault Master DX 9043F",
        "plate": "DX 9043F"
    },
    {
        "id": "f016af91-dee6-4e72-9f86-4b2e27a253c1",
        "name": "Renault Master DX 5405A",
        "plate": "DX 5405A"
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


def normalize_driver_login(value):
    """Normalize a vehicle plate used as driver login: DX 9043F == DX9043F."""
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def current_driver_vehicle():
    """Vehicle assigned to the logged-in driver session."""
    assigned_id = normalize_vehicle_id(session.get("driver_vehicle_id", ""))
    if assigned_id:
        for vehicle in VEHICLES:
            if vehicle["id"] == assigned_id:
                return vehicle
    return VEHICLES[0] if VEHICLES else {}


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
    lang = current_language()
    if lang == "pl":
        return f"{hours} godz. {minutes:02d} min"
    if lang == "en":
        return f"{hours} h {minutes:02d} min"
    if lang == "de":
        return f"{hours} Std. {minutes:02d} Min."
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
    if tenancy.company():
        try:
            return tenancy.normalize_states(gps_live_states(tenancy.company().get("gps", {}), list(VEHICLES)))
        except GPSError:
            return []
    """Return the latest Navirec vehicle states with a short retry.

    Navirec can occasionally return a transient error/empty response.  The
    GPS planner must not lose every vehicle because of one failed request,
    so we retry before treating the state list as unavailable.
    """
    if not NAVIREC_TOKEN:
        return []

    url = f"{NAVIREC_API}/last_vehicle_states/"
    params = {"account": str(NAVIREC_ACCOUNT_ID)}

    for attempt in range(3):
        try:
            response = requests.get(
                url,
                headers=navirec_headers(),
                params=params,
                timeout=20
            )

            if response.status_code == 200:
                data = response.json()
                if isinstance(data, list) and data:
                    return data
                if isinstance(data, list) and attempt == 2:
                    return data
        except Exception:
            pass

        if attempt < 2:
            time.sleep(0.6)

    return []


def navirec_list(endpoint, params=None, timeout=25):
    if tenancy.company() and tenancy.company().get("gps", {}).get("provider", "navirec") != "navirec":
        return {"ok": False, "items": [], "error": "Цей GPS не надає дані Navirec/тахографа."}
    if not NAVIREC_TOKEN:
        return {
            "ok": False,
            "items": [],
            "error": "NAVIREC_TOKEN не налаштований."
        }

    params = params or {}
    cache_key = (
        endpoint.strip("/"),
        tuple(sorted((str(k), str(v)) for k, v in params.items()))
    )
    now = time.time()

    # Reuse a fresh response instead of hitting Navirec again for the same
    # driver/card list from another role/page.
    with _NAVIREC_LIST_CACHE_LOCK:
        cached = _NAVIREC_LIST_CACHE.get(cache_key)
        if cached and now - cached["time"] <= NAVIREC_LIST_CACHE_TTL:
            return {
                "ok": True,
                "items": list(cached["items"]),
                "error": None,
                "cached": True,
                "stale": False
            }

    try:
        response = requests.get(
            f"{NAVIREC_API}/{endpoint.strip('/')}/",
            headers=navirec_headers(),
            params=params,
            timeout=timeout
        )

        if response.status_code == 200:
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

            items = [
                item
                for item in items
                if isinstance(item, dict)
            ]

            items = tenancy.normalize_states(items)
            with _NAVIREC_LIST_CACHE_LOCK:
                _NAVIREC_LIST_CACHE[cache_key] = {
                    "time": now,
                    "items": list(items)
                }

            return {
                "ok": True,
                "items": items,
                "error": None,
                "cached": False,
                "stale": False
            }

        live_error = (
            f"Navirec HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )

    except Exception as exc:
        live_error = f"Помилка Navirec: {exc}"

    # On 429/temporary failure do NOT turn a working tachograph into zeros.
    # Show the last confirmed server-side values if they are still reasonably recent.
    with _NAVIREC_LIST_CACHE_LOCK:
        cached = _NAVIREC_LIST_CACHE.get(cache_key)
        if cached and now - cached["time"] <= NAVIREC_LIST_STALE_MAX:
            return {
                "ok": True,
                "items": list(cached["items"]),
                "error": None,
                "cached": True,
                "stale": True,
                "source_error": live_error
            }

    return {
        "ok": False,
        "items": [],
        "error": live_error
    }


def get_driver_states_result():
    return navirec_list(
        "last_driver_states",
        {"account": str(NAVIREC_ACCOUNT_ID)}
    )


def get_drivers_result():
    return navirec_list(
        "drivers",
        {
            "account": str(NAVIREC_ACCOUNT_ID),
            "active": "true",
            "page_size": 500
        }
    )


def get_tachograph_cards_result():
    return navirec_list(
        "tachograph_cards",
        {
            "account": str(NAVIREC_ACCOUNT_ID),
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


def build_driver_iq_balance(data):
    """Build a conservative IQ rest-balance snapshot from Navirec fields.

    This does not invent missing tachograph history. Values that Navirec does
    not expose are left unknown until TRANVIQ has durable driver memory.
    """
    min_daily_rest = get_duration_value(
        data, ["driver_min_daily_rest", "driver_1_min_daily_rest"]
    )
    min_weekly_rest = get_duration_value(
        data, ["driver_min_weekly_rest", "driver_1_min_weekly_rest"]
    )
    compensation_fields = (
        "driver_open_compensation_2nd_week_before_last",
        "driver_open_compensation_week_before_last",
        "driver_open_compensation_last_week",
    )
    compensation_parts = []
    for field in compensation_fields:
        value = safe_float(data.get(field))
        if value is not None and value > 0:
            compensation_parts.append(value)
    open_compensation = sum(compensation_parts)
    return {
        "min_daily_rest_s": min_daily_rest,
        "min_weekly_rest_s": min_weekly_rest,
        "open_weekly_compensation_s": open_compensation,
        "weekly_rest_plus_all_compensation_s": (
            45 * 3600 + open_compensation
        ),
        "last_daily_rest_end": get_first_value(
            data, ["driver_end_last_daily_rest"]
        ),
        "last_weekly_rest_end": get_first_value(
            data, ["driver_end_last_weekly_rest"]
        ),
        "second_last_weekly_rest_end": get_first_value(
            data, ["driver_end_second_last_weekly_rest"]
        ),
    }


def merge_tachograph_state(vehicle_state, driver_state):
    merged = {}

    if isinstance(vehicle_state, dict):
        merged.update(vehicle_state)

    if isinstance(driver_state, dict):
        for key, value in driver_state.items():
            if value is not None:
                merged[key] = value

    return merged


def build_tachograph_snapshots(vehicle_states):
    """Return privacy-safe driver-time data keyed by vehicle id.

    Only operational values required for route feasibility are exposed to
    the GPS page. Card numbers and other personal data stay on the server.
    """
    driver_states_result = get_driver_states_result()
    drivers_result = get_drivers_result()

    driver_states_by_vehicle = {}
    driver_states_by_driver = {}

    for driver_state in driver_states_result["items"]:
        vehicle_id = normalize_api_id(
            driver_state.get("vehicle")
        )
        driver_id = normalize_api_id(
            driver_state.get("driver")
        )

        if vehicle_id:
            driver_states_by_vehicle[vehicle_id] = driver_state
        if driver_id:
            driver_states_by_driver[driver_id] = driver_state

    drivers_by_id = {
        normalize_api_id(driver.get("id") or driver.get("url")): driver
        for driver in drivers_result["items"]
        if normalize_api_id(driver.get("id") or driver.get("url"))
    }
    vehicle_state_map = state_map_by_vehicle(vehicle_states)
    snapshots = {}

    for vehicle in VEHICLES:
        vehicle_id = vehicle["id"]
        vehicle_state = vehicle_state_map.get(vehicle_id, {})
        driver_id = normalize_api_id(vehicle_state.get("driver"))
        driver_state = driver_states_by_vehicle.get(vehicle_id)

        if not driver_state and driver_id:
            driver_state = driver_states_by_driver.get(driver_id)

        if driver_state:
            driver_id = normalize_api_id(
                driver_state.get("driver")
            ) or driver_id

        combined = merge_tachograph_state(
            vehicle_state,
            driver_state
        )
        driver = drivers_by_id.get(driver_id, {})
        driver_name = str(driver.get("name") or "").strip()

        if not driver_name:
            first_name = str(
                combined.get("driver_name") or ""
            ).strip()
            surname = str(
                combined.get("driver_surname") or ""
            ).strip()
            driver_name = " ".join(
                part for part in (first_name, surname) if part
            )

        update_time = get_first_value(
            combined,
            ["time", "updated_at", "received_at"]
        )
        card_present = get_first_value(
            combined,
            ["driver_1_card_present"]
        )
        if card_present is None:
            card_present = bool(
                get_first_value(
                    combined,
                    ["driver_1_card_id", "driver_code"]
                )
            )

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

        iq_balance = build_driver_iq_balance(combined)

        remaining_values = [
            current_drive_remaining,
            daily_drive_remaining,
            shift_drive_remaining,
            weekly_drive_remaining,
            time_until_break,
            time_until_daily_rest
        ]
        snapshots[vehicle_id] = {
            "driver_name": driver_name or "Водія не визначено",
            "card_present": bool(card_present),
            "working_state": get_first_value(
                combined,
                ["driver_working_state", "driver_1_working_state"]
            ),
            "time_state": get_first_value(
                combined,
                ["driver_time_state", "driver_1_time_state"]
            ),
            "updated_at": update_time,
            "age_seconds": state_age_seconds(update_time),
            "remaining_current_driving_s": current_drive_remaining,
            "remaining_daily_driving_s": daily_drive_remaining,
            "remaining_shift_driving_s": shift_drive_remaining,
            "remaining_weekly_driving_s": weekly_drive_remaining,
            "time_until_break_s": time_until_break,
            "time_until_daily_rest_s": time_until_daily_rest,
            "min_daily_rest_s": iq_balance["min_daily_rest_s"],
            "min_weekly_rest_s": iq_balance["min_weekly_rest_s"],
            "open_weekly_compensation_s": iq_balance["open_weekly_compensation_s"],
            "weekly_rest_plus_all_compensation_s": iq_balance["weekly_rest_plus_all_compensation_s"],
            "has_remaining_time": any(
                value is not None for value in remaining_values
            ),
            "api_ok": bool(
                driver_states_result["ok"]
                and drivers_result["ok"]
            )
        }

    return snapshots


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
    if tenancy.company() and tenancy.company().get("gps", {}).get("provider", "navirec") != "navirec": return {"ok": False, "points": [], "error": "Історія цього GPS ще не підключена. Дані не підміняються."}
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
            "vehicle": tenancy.provider_vehicle_id(vehicle_id),
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


@app.route("/api/vehicle-day-summary")
def vehicle_day_summary():
    vehicle_id = normalize_vehicle_id(
        request.args.get("vehicle", "")
    )
    date_string = request.args.get(
        "date",
        datetime.now(POLAND_TZ).strftime("%Y-%m-%d")
    )

    if not vehicle_by_id(vehicle_id):
        return jsonify({"error": "Автомобіль не знайдено."}), 404

    result = get_vehicle_history(vehicle_id, date_string)
    if not result["ok"]:
        return jsonify({
            "error": result["error"],
            "available": False
        }), 503

    points = result["points"]
    metrics = calculate_history_metrics(points)
    first_movement = None

    for point in points:
        speed = safe_float(point.get("speed")) or 0
        activity = str(point.get("activity") or "").lower()
        if (
            speed > 2
            or activity == "driving"
            or bool(point.get("ignition"))
        ):
            first_movement = point
            break

    return jsonify({
        "available": bool(points),
        "date": date_string,
        "distance_km": round(metrics["distance_km"], 1),
        "driving_seconds": round(metrics["driving_seconds"]),
        "parking_seconds": round(metrics["parking_seconds"]),
        "idling_seconds": round(metrics["idling_seconds"]),
        "first_movement_at": (
            first_movement.get("time")
            if first_movement else None
        ),
        "last_point_at": points[-1].get("time") if points else None
    })


_delivery_routes_env = os.environ.get("DELIVERY_ROUTES_FILE", "").strip()
if _delivery_routes_env:
    DELIVERY_ROUTES_FILE = _delivery_routes_env
elif os.path.isdir("/var/data") and os.access("/var/data", os.W_OK):
    DELIVERY_ROUTES_FILE = "/var/data/tranviq_delivery_routes.json"
else:
    DELIVERY_ROUTES_FILE = "/tmp/tranviq_delivery_routes.json"
DELIVERY_ROUTES_LOCK = threading.Lock()


def _load_delivery_routes():
    if tenancy.company(): return tenancy.json_read("routes", {})
    try:
        with open(DELIVERY_ROUTES_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_delivery_routes(data):
    if tenancy.company(): return tenancy.json_write("routes", data)
    folder = os.path.dirname(DELIVERY_ROUTES_FILE)
    if folder:
        os.makedirs(folder, exist_ok=True)
    temporary = DELIVERY_ROUTES_FILE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    os.replace(temporary, DELIVERY_ROUTES_FILE)


# Company accounts, vehicles and GPS settings share one transactional store.
TENANT_ACCOUNTS_FILE = os.environ.get('TENANT_ACCOUNTS_FILE') or os.path.join(os.path.dirname(DELIVERY_ROUTES_FILE), 'tranviq_tenant_accounts.json')
_TENANT_DB_URL = os.environ.get('DATABASE_URL', '').strip()
_TENANT_LOCAL = threading.local()

class _TenantStoreLock:
    def __init__(self):
        self.lock = threading.RLock()
    def __enter__(self):
        self.lock.acquire()
        try:
            if _TENANT_DB_URL:
                try:
                    import psycopg
                    connect = psycopg.connect
                except ImportError:
                    import psycopg2
                    connect = psycopg2.connect
                connection = connect(_TENANT_DB_URL, connect_timeout=10)
                _TENANT_LOCAL.connection = connection
                with connection.cursor() as cursor:
                    # Serializes first migration and every read/modify/write across workers.
                    cursor.execute('SELECT pg_advisory_xact_lock(814527306)')
                    cursor.execute('CREATE TABLE IF NOT EXISTS tranviq_company_store (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
                    cursor.execute('SELECT payload FROM tranviq_company_store WHERE id=1')
                    if cursor.fetchone() is None:
                        initial = _tenant_read_local()
                        cursor.execute('INSERT INTO tranviq_company_store(id,payload) VALUES (1,%s)', (json.dumps(initial, ensure_ascii=False),))
            return self
        except Exception:
            connection = getattr(_TENANT_LOCAL, 'connection', None)
            if connection: connection.close()
            _TENANT_LOCAL.connection = None
            self.lock.release()
            raise
    def __exit__(self, kind, value, traceback):
        connection = getattr(_TENANT_LOCAL, 'connection', None)
        try:
            if connection:
                if kind is None: connection.commit()
                else: connection.rollback()
        finally:
            if connection: connection.close()
            _TENANT_LOCAL.connection = None
            self.lock.release()

TENANT_ACCOUNTS_LOCK = _TenantStoreLock()

def _tenant_read_local():
    try:
        with open(TENANT_ACCOUNTS_FILE, encoding='utf-8') as handle:
            data = json.load(handle)
        if not isinstance(data, dict): raise ValueError('Invalid company store')
        return data
    except FileNotFoundError:
        return {'companies': {}, 'users': {}}

def _load_tenant_accounts():
    connection = getattr(_TENANT_LOCAL, 'connection', None)
    if _TENANT_DB_URL:
        if connection is None: raise RuntimeError('Company store requires a transaction')
        with connection.cursor() as cursor:
            cursor.execute('SELECT payload FROM tranviq_company_store WHERE id=1')
            return json.loads(cursor.fetchone()[0])
    return _tenant_read_local()

def _write_tenant_accounts(data):
    connection = getattr(_TENANT_LOCAL, 'connection', None)
    if _TENANT_DB_URL:
        if connection is None: raise RuntimeError('Company store requires a transaction')
        with connection.cursor() as cursor:
            cursor.execute('UPDATE tranviq_company_store SET payload=%s WHERE id=1', (json.dumps(data, ensure_ascii=False),))
        return
    folder = os.path.dirname(TENANT_ACCOUNTS_FILE)
    if folder: os.makedirs(folder, exist_ok=True)
    temporary = TENANT_ACCOUNTS_FILE + '.' + uuid.uuid4().hex + '.tmp'
    with open(temporary, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, TENANT_ACCOUNTS_FILE)

def _tenant_login_key(value):
    return str(value or '').strip().casefold()

def _tenant_company():
    company_id = str(session.get('tenant_company_id') or '').strip()
    if not company_id: return None
    with TENANT_ACCOUNTS_LOCK:
        data = _load_tenant_accounts()
    user = data.get('users', {}).get(_tenant_login_key(session.get('username')))
    if not isinstance(user, dict) or not user.get('enabled') or user.get('role') not in ROLE_LABELS or user.get('company_id') != company_id or user.get('id') != session.get('tenant_user_id'):
        return None
    company = data.get('companies', {}).get(company_id)
    return company if isinstance(company, dict) else None

def _tenant_storage_is_persistent():
    return bool(_TENANT_DB_URL) or os.path.abspath(TENANT_ACCOUNTS_FILE).startswith('/var/data/')

def _tenant_csrf():
    if not session.get('tenant_csrf'): session['tenant_csrf'] = secrets.token_hex(32)
    return session['tenant_csrf']

def _tenant_form_token():
    return '<input type="hidden" name="csrf" value="'+escape(_tenant_csrf())+'">'

def _tenant_check_csrf():
    return hmac.compare_digest(str(session.get('tenant_csrf') or ''), str(request.form.get('csrf') or request.headers.get('X-CSRF-Token') or '')) and bool(session.get('tenant_csrf'))

DRIVER_SETTINGS_FILE = os.path.join(os.path.dirname(DELIVERY_ROUTES_FILE), "tranviq_driver_settings.json")
DRIVER_SETTINGS_LOCK = threading.Lock()

DRIVER_ACCESS_FILE = os.path.join(os.path.dirname(DELIVERY_ROUTES_FILE), "tranviq_driver_access.json")
DRIVER_ACCESS_LOCK = threading.Lock()

def _driver_access_storage_is_persistent():
    if tenancy.company(): return _tenant_storage_is_persistent()
    """True only when driver credentials are stored on Render persistent disk."""
    try:
        path = os.path.abspath(DRIVER_ACCESS_FILE)
    except Exception:
        return False
    return path.startswith("/var/data/") or path == "/var/data"


def _load_driver_access():
    if tenancy.company(): return tenancy.json_read("driver_access", {})
    try:
        with open(DRIVER_ACCESS_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}

def _write_driver_access(data):
    if tenancy.company(): return tenancy.json_write("driver_access", data)
    folder = os.path.dirname(DRIVER_ACCESS_FILE)
    if folder:
        os.makedirs(folder, exist_ok=True)
    temporary = DRIVER_ACCESS_FILE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    os.replace(temporary, DRIVER_ACCESS_FILE)



def _load_driver_settings():
    if tenancy.company(): return tenancy.json_read("driver_settings", {})
    try:
        with open(DRIVER_SETTINGS_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_driver_settings(data):
    if tenancy.company(): return tenancy.json_write("driver_settings", data)
    folder = os.path.dirname(DRIVER_SETTINGS_FILE)
    if folder:
        os.makedirs(folder, exist_ok=True)
    temporary = DRIVER_SETTINGS_FILE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    os.replace(temporary, DRIVER_SETTINGS_FILE)


def driver_can_see_other_vehicles():
    with DRIVER_SETTINGS_LOCK:
        settings = _load_driver_settings()
    return bool(settings.get("show_other_vehicles", False))


@app.route("/api/driver-fleet-visibility", methods=["GET", "PUT"])
def driver_fleet_visibility():
    if request.method == "GET":
        return jsonify({"ok": True, "show_other_vehicles": driver_can_see_other_vehicles()})
    if current_role() != "director":
        return jsonify({"ok": False, "error": "director_only"}), 403
    payload = request.get_json(silent=True) or {}
    enabled = bool(payload.get("show_other_vehicles"))
    with DRIVER_SETTINGS_LOCK:
        settings = _load_driver_settings()
        settings["show_other_vehicles"] = enabled
        _write_driver_settings(settings)
    return jsonify({"ok": True, "show_other_vehicles": enabled})


@app.route("/driver-settings")
def driver_settings():
    if current_role() != "director":
        return redirect(role_home_url())

    lang = current_language()
    labels = {
        "uk": {
            "title": "Налаштування водіїв",
            "empty": "Немає доданих водіїв.",
            "drivers": "Водії компанії",
            "allow": "Дозволити водіям бачити інші автомобілі",
            "note": "Зміна застосовується автоматично на телефоні водія.",
            "saving": "Зберігаю…",
            "on": "УВІМКНЕНО — водії бачать інші автомобілі.",
            "off": "ВИМКНЕНО — водії бачать лише свій автомобіль.",
            "error": "Не вдалося зберегти налаштування.",
        },
        "pl": {
            "title": "Ustawienia kierowców",
            "empty": "Brak dodanych kierowców.",
            "drivers": "Kierowcy firmy",
            "allow": "Zezwól kierowcom widzieć inne pojazdy",
            "note": "Zmiana działa automatycznie na telefonie kierowcy.",
            "saving": "Zapisywanie…",
            "on": "WŁĄCZONE — kierowcy widzą inne pojazdy.",
            "off": "WYŁĄCZONE — kierowcy widzą tylko swój pojazd.",
            "error": "Nie udało się zapisać ustawienia.",
        },
        "en": {
            "title": "Driver settings",
            "empty": "No drivers have been added.",
            "drivers": "Company drivers",
            "allow": "Allow drivers to see other vehicles",
            "note": "The change is applied automatically on the driver's phone.",
            "saving": "Saving…",
            "on": "ON — drivers can see other vehicles.",
            "off": "OFF — drivers can see only their own vehicle.",
            "error": "Could not save the setting.",
        },
        "de": {
            "title": "Fahrereinstellungen",
            "empty": "Es wurden noch keine Fahrer hinzugefügt.",
            "drivers": "Fahrer des Unternehmens",
            "allow": "Fahrern erlauben, andere Fahrzeuge zu sehen",
            "note": "Die Änderung wird automatisch auf dem Telefon des Fahrers übernommen.",
            "saving": "Speichern…",
            "on": "EIN — Fahrer sehen andere Fahrzeuge.",
            "off": "AUS — Fahrer sehen nur ihr eigenes Fahrzeug.",
            "error": "Die Einstellung konnte nicht gespeichert werden.",
        },
    }[lang if lang in {"uk", "pl", "en", "de"} else "uk"]

    tenant_drivers = []
    company = _tenant_company()
    if company:
        with TENANT_ACCOUNTS_LOCK:
            tenant_data = _load_tenant_accounts()
        for user in tenant_data.get("users", {}).values():
            if not isinstance(user, dict):
                continue
            if str(user.get("company_id") or "") != str(company.get("id") or ""):
                continue
            if str(user.get("role") or "") != "driver" or not user.get("enabled", True):
                continue
            vehicle = vehicle_by_id(str(user.get("vehicle_id") or ""))
            tenant_drivers.append({
                "name": str(user.get("name") or user.get("login") or "").strip(),
                "vehicle": str((vehicle or {}).get("plate") or (vehicle or {}).get("name") or "").strip(),
            })

        if not tenant_drivers:
            body = (
                '<div class="card" style="max-width:760px;margin:0 auto">'
                '<h2>' + escape(labels["title"]) + '</h2>'
                '<p>' + escape(labels["empty"]) + '</p>'
                '</div>'
            )
            return page(labels["title"], body, "driver_settings")

    enabled = driver_can_see_other_vehicles()
    driver_list = ""
    if tenant_drivers:
        items = []
        for item in tenant_drivers:
            suffix = (" — " + escape(item["vehicle"])) if item["vehicle"] else ""
            items.append("<li><strong>" + escape(item["name"]) + "</strong>" + suffix + "</li>")
        driver_list = (
            "<p><strong>" + escape(labels["drivers"]) + ":</strong></p>"
            "<ul>" + "".join(items) + "</ul>"
        )

    body = f"""
    <div class="card" style="max-width:760px;margin:0 auto">
      <h2>{escape(labels["title"])}</h2>
      {driver_list}
      <label style="display:flex;align-items:center;gap:12px;font-size:18px;font-weight:800;margin:20px 0">
        <input id="fleetVisibility" type="checkbox" {'checked' if enabled else ''} style="width:24px;height:24px">
        {escape(labels["allow"])}
      </label>
      <div id="fleetVisibilityStatus" class="small">{escape(labels["note"])}</div>
    </div>
    <script>
    const fleetVisibilityText = {json.dumps(labels, ensure_ascii=False)};
    document.getElementById('fleetVisibility').addEventListener('change', async function() {{
      const status=document.getElementById('fleetVisibilityStatus');
      status.textContent=fleetVisibilityText.saving;
      try {{
        const r=await fetch('/api/driver-fleet-visibility', {{method:'PUT',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{show_other_vehicles:this.checked}})}});
        if(!r.ok) throw new Error('HTTP '+r.status);
        status.textContent=this.checked?fleetVisibilityText.on:fleetVisibilityText.off;
      }} catch(e) {{
        this.checked=!this.checked;
        status.textContent=fleetVisibilityText.error;
      }}
    }});
    </script>
    """
    return page(labels["title"], body, "driver_settings")


@app.route("/api/delivery-route/<vehicle_id>/queue", methods=["GET", "POST", "DELETE"])
def delivery_route_queue(vehicle_id):
    """Queue of future jobs for a vehicle. Active route stays separate."""
    vehicle_id = normalize_vehicle_id(vehicle_id)
    if not vehicle_by_id(vehicle_id):
        return jsonify({"error": "Автомобіль не знайдено."}), 404
    with DELIVERY_ROUTES_LOCK:
        routes = _load_delivery_routes()
        saved = routes.get(vehicle_id) or {}
        queue = saved.get("route_queue") if isinstance(saved, dict) else []
        if not isinstance(queue, list):
            queue = []
        if request.method == "GET":
            return jsonify({"queue": queue})
        if request.method == "DELETE":
            if isinstance(saved, dict):
                saved["route_queue"] = []
                routes[vehicle_id] = saved
                _write_delivery_routes(routes)
            return jsonify({"ok": True, "queue": []})
        payload = request.get_json(silent=True) or {}
        future_route = payload.get("route")
        if not isinstance(future_route, dict) or not isinstance(future_route.get("delivery_route"), dict):
            return jsonify({"error": "Неправильні дані наступного рейсу."}), 400
        future_route["vehicle_id"] = vehicle_id
        future_route["queue_status"] = "next"
        future_route["queued_at"] = datetime.now(timezone.utc).isoformat()
        queue.append(future_route)
        if not isinstance(saved, dict):
            saved = {}
        saved["route_queue"] = queue
        routes[vehicle_id] = saved
        _write_delivery_routes(routes)
        return jsonify({"ok": True, "queue": queue})


@app.route("/api/delivery-routes", methods=["GET"])
def delivery_routes_list():
    """Return all saved active routes for dispatcher live synchronization."""
    with DELIVERY_ROUTES_LOCK:
        routes = _load_delivery_routes()
    valid = {}
    for vehicle_id, saved in routes.items():
        normalized = normalize_vehicle_id(vehicle_id)
        if tenancy.company() and current_role() == "driver" and normalized != session.get("driver_vehicle_id"): continue
        if (
            vehicle_by_id(normalized)
            and isinstance(saved, dict)
            and isinstance(saved.get("delivery_route"), dict)
            and isinstance(saved.get("route_data"), dict)
        ):
            valid[normalized] = saved
    return jsonify({"routes": valid})


@app.route("/api/delivery-route/<vehicle_id>", methods=["GET", "PUT", "DELETE"])
def delivery_route_storage(vehicle_id):
    vehicle_id = normalize_vehicle_id(vehicle_id)
    if not vehicle_by_id(vehicle_id):
        return jsonify({"error": "Автомобіль не знайдено."}), 404

    with DELIVERY_ROUTES_LOCK:
        routes = _load_delivery_routes()

        if request.method == "GET":
            saved = routes.get(vehicle_id)
            return jsonify({"route": saved})

        if request.method == "DELETE":
            routes.pop(vehicle_id, None)
            _write_delivery_routes(routes)
            return jsonify({"ok": True})

        payload = request.get_json(silent=True) or {}
        saved = payload.get("route")
        if not isinstance(saved, dict):
            return jsonify({"error": "Неправильні дані маршруту."}), 400
        if normalize_vehicle_id(saved.get("vehicle_id", "")) != vehicle_id:
            return jsonify({"error": "Маршрут належить іншому автомобілю."}), 400
        if not isinstance(saved.get("delivery_route"), dict):
            return jsonify({"error": "Немає даних маршруту."}), 400

        previous = routes.get(vehicle_id)
        if isinstance(previous, dict) and isinstance(previous.get("route_queue"), list):
            saved["route_queue"] = previous.get("route_queue", [])
        routes[vehicle_id] = saved
        _write_delivery_routes(routes)
        return jsonify({"ok": True})


@app.route("/api/delivery-stop-status", methods=["POST"])
def delivery_stop_status():
    payload = request.get_json(silent=True) or {}
    vehicle_id = normalize_vehicle_id(payload.get("vehicle_id", ""))
    date_string = str(payload.get("date") or "").strip()
    raw_stops = payload.get("stops") or []

    if not vehicle_by_id(vehicle_id):
        return jsonify({"error": "Автомобіль не знайдено."}), 404

    try:
        selected_date = datetime.strptime(date_string, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "Неправильна дата маршруту."}), 400

    stops = []
    if isinstance(raw_stops, list):
        for raw_stop in raw_stops[:24]:
            if not isinstance(raw_stop, dict):
                continue
            latitude = safe_float(raw_stop.get("latitude"))
            longitude = safe_float(raw_stop.get("longitude"))
            if latitude is None or longitude is None:
                continue
            manual_status = str(raw_stop.get("manual_status") or "").strip().lower()
            if manual_status not in ("completed", "refused", "pending"):
                manual_status = ""
            stops.append({
                "latitude": latitude,
                "longitude": longitude,
                "manual_status": manual_status
            })

    if not stops:
        return jsonify({"error": "Немає координат точок."}), 400

    # The route date describes the planned job, not the date on which GPS
    # presence must be checked. An active route can continue for several days.
    today = datetime.now(POLAND_TZ).date()
    history = get_vehicle_history(vehicle_id, today.isoformat())
    points = history.get("points", []) if history.get("ok") else []
    current_state = state_for_vehicle(vehicle_id)
    current_latitude = None
    current_longitude = None

    if current_state:
        current_latitude, current_longitude = extract_coordinates(
            current_state.get("location")
        )

    # If the live Navirec state is temporarily unavailable, keep using the
    # newest history point instead of making the current stop flash red.
    if (
        (current_latitude is None or current_longitude is None)
        and points
    ):
        current_latitude = safe_float(points[-1].get("latitude"))
        current_longitude = safe_float(points[-1].get("longitude"))

    # Use a slightly wider radius for "vehicle is here" than for automatic
    # completion. Geocoded street addresses and real warehouse gates can be
    # noticeably apart.
    current_radius_km = 1.5
    completion_radius_km = 1.0

    # One minute stopped/slow inside the delivery area is enough to remember
    # the visit after the vehicle leaves. While the vehicle is still there the
    # stop remains CURRENT (yellow), not COMPLETED (green).
    minimum_dwell_seconds = 60
    statuses = []

    def point_time(point):
        raw = str(point.get("time") or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            return None

    for stop in stops:
        manual_status = stop.get("manual_status") or ""
        if manual_status in ("completed", "refused"):
            statuses.append(manual_status)
            continue

        is_current = False
        if current_latitude is not None and current_longitude is not None:
            current_distance = haversine_km(
                {
                    "latitude": current_latitude,
                    "longitude": current_longitude
                },
                stop
            )
            is_current = current_distance <= current_radius_km

        dwell_start = None
        dwell_end = None
        visited = False
        for point in points:
            distance = haversine_km(point, stop)
            speed = safe_float(point.get("speed")) or 0.0
            activity = str(point.get("activity") or "").strip().lower()
            stopped = speed <= 8 or activity not in ("driving", "moving")
            timestamp = point_time(point)

            if distance <= completion_radius_km and stopped and timestamp is not None:
                if dwell_start is None:
                    dwell_start = timestamp
                dwell_end = timestamp
                if (dwell_end - dwell_start).total_seconds() >= minimum_dwell_seconds:
                    visited = True
                    break
            else:
                dwell_start = None
                dwell_end = None

        if manual_status == "pending" and is_current:
            statuses.append("current")
        elif manual_status == "pending":
            statuses.append("pending")
        elif is_current:
            # Yellow must stay yellow while the vehicle is physically at the
            # stop. Only after it leaves can the GPS visit turn green.
            statuses.append("current")
        elif visited:
            statuses.append("completed")
        else:
            statuses.append("pending")

    return jsonify({
        "statuses": statuses,
        "current_radius_m": int(current_radius_km * 1000),
        "completion_radius_m": int(completion_radius_km * 1000),
        "minimum_dwell_seconds": minimum_dwell_seconds
    })


def get_vehicle_timeline_totals(vehicle_id, date_string):
    if tenancy.company() and tenancy.company().get("gps", {}).get("provider", "navirec") != "navirec": return None
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
            "vehicle": tenancy.provider_vehicle_id(vehicle_id),
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
    if tenancy.company() and tenancy.company().get("gps", {}).get("provider", "navirec") != "navirec": return None
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
                "vehicle": tenancy.provider_vehicle_id(vehicle_id),
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



# Global UI fallback translation.  All route bodies pass through this layer, so the
# selected language is consistent across GPS, tachograph, fuel, history, vehicles,
# finance and role dashboards.  Longer phrases are replaced first.
GLOBAL_UI_TRANSLATIONS = {
    "pl": {
        'Дані отримуються безпосередньо з Navirec:': 'Dane są pobierane bezpośrednio z Navirec:',
        'водії, картки та last_driver_states.': 'kierowcy, karty oraz last_driver_states.',
        'Час тахографа не вираховується з GPS.': 'Czas tachografu nie jest obliczany na podstawie GPS.',
        'Саме ці значення надалі використовуватимуться': 'Te wartości będą dalej wykorzystywane',
        'для перевірки, чи можна брати рейс Trans.eu.': 'do sprawdzania, czy można przyjąć zlecenie z Trans.eu.',
        'Технічна перевірка даних': 'Techniczna kontrola danych',
        'Автомобілі': 'Pojazdy',
        'Картка вставлена': 'Karta włożona',
        'Станів водіїв Navirec': 'Stanów kierowców Navirec',
        'Попередження тахографа (код ': 'Ostrzeżenia tachografu (kod ',
        ')': ')',
        'Попередження': 'Ostrzeżenia',
        'Залишилося мало безперервного керування:': 'Pozostało mało czasu jazdy ciągłej:',
        'Залишилося мало денного керування:': 'Pozostało mało czasu jazdy dzisiaj:',
        "Залишилося мало часу до обов'язкової перерви:": 'Pozostało mało czasu do obowiązkowej przerwy:',
        'Залишилося мало часу до денного відпочинку:': 'Pozostało mało czasu do odpoczynku dobowego:',
        'Водій': 'Kierowca',
        'Картка водія': 'Карта водія',
        'Вставлена': 'Włożona',
        'Оновлено': 'Zaktualizowano',
        'Стан часу': 'Stan czasu',
        'До наступної перерви': 'До наступної перерви',
        'Залишок безперервного керування': 'Pozostały czas jazdy ciągłej',
        'Залишок керування сьогодні': 'Pozostały czas jazdy dzisiaj',
        'Залишок у зміні': 'Pozostały czas jazdy w zmianie',
        'Залишок керування цього тижня': 'Pozostały czas jazdy w tym tygodniu',
        'До денного відпочинку': 'До добового відпочинку',
        'До тижневого відпочинку': 'Do odpoczynku tygodniowego',
        'Керування сьогодні': 'Jazda dzisiaj',
        'Керування за тиждень': 'Jazda w tym tygodniu',
        'Керування за два тижні': 'Jazda w ciągu dwóch tygodni',
        'Робота сьогодні': 'Praca dzisiaj',
        'Перерва / відпочинок': 'Przerwa / odpoczynek',
        'Залишок поточного відпочинку': 'Pozostały czas bieżącego odpoczynku',
        'Картка дійсна до': 'Karta ważna do',
        'TRANVIQ IQ — баланс відпочинку': 'TRANVIQ IQ — bilans odpoczynku',
        'Мінімальний добовий відпочинок зараз': 'Minimalny odpoczynek dobowy teraz',
        'Мінімальний тижневий відпочинок Navirec': 'Minimalny odpoczynek tygodniowy Navirec',
        'Відкрита компенсація тижневого відпочинку': 'Otwarta rekompensata odpoczynku tygodniowego',
        '45 год + весь відкритий борг': '45 godz. + całe otwarte zobowiązanie',
        'Кінець останнього добового відпочинку': 'Koniec ostatniego odpoczynku dobowego',
        'Кінець останнього тижневого відпочинку': 'Koniec ostatniego odpoczynku tygodniowego',
        'IQ використовує тільки підтверджені поля Navirec.': 'IQ korzysta wyłącznie z potwierdzonych pól Navirec.',
        'Лічильник використаних 9-годинних добових відпочинків': 'Licznik wykorzystanych 9-godzinnych skróconych odpoczynków dobowych',
        'додамо до постійної пам’яті TRANVIQ, щоб він не губився': 'zostanie dodany do trwałej pamięci TRANVIQ, aby nie był tracony',
        'після перезапуску сервера.': 'po ponownym uruchomieniu serwera.',
        'Активних попереджень немає.': 'Brak aktywnych ostrzeżeń.',
        'Navirec не повернув повний стан водія. Показані лише дані, наявні в автомобілі.': 'Navirec nie zwrócił pełnego stanu kierowcy. Wyświetlane są tylko dane dostępne w pojeździe.',
        'Дані тахографа застарілі: останнє оновлення': 'Dane tachografu są nieaktualne: ostatnia aktualizacja',
        'Помилки Navirec': 'Błędy Navirec',
        "Дозволяю передати картографічним сервісам лише адреси цього маршруту": "Zezwalam na przekazanie serwisom mapowym wyłącznie adresów tej trasy",
        "Для карти використовуються лише адреси й часові вікна. Імена та телефони не передаються.": "Do mapy używane są wyłącznie adresy i okna czasowe. Imiona, nazwiska i numery telefonów nie są przekazywane.",
        "Вартість є орієнтовною. Вона залежить від ваги, осей, екологічного класу, віньєт і способу оплати.": "Koszt jest orientacyjny. Zależy od masy, liczby osi, klasy emisji, winiet i sposobu płatności.",
        "Маршрут узгоджено з актуальним тахографом.": "Trasa jest zgodna z aktualnymi danymi tachografu.",
        "Наступне завантаження можна планувати:": "Następny załadunek można planować:",
        "Рекомендований наступний виїзд:": "Zalecany następny wyjazd:",
        "Початок сьогоднішньої роботи:": "Początek dzisiejszej pracy:",
        "Після завершення залишається щонайменше": "Po zakończeniu pozostaje co najmniej",
        "Орієнтовна оплата доріг:": "Szacunkowa opłata drogowa:",
        "Для цього автомобіля активного розвізного маршруту немає.": "Dla tego pojazdu nie ma aktywnej trasy dostaw.",
        "Потрібне підтвердження передачі адрес карті.": "Wymagane jest potwierdzenie przekazania adresów do mapy.",
        "Вставте адреси або текст транспортного завдання.": "Wklej adresy lub tekst zlecenia transportowego.",
        "Для автомобіля немає актуальної GPS-позиції.": "Brak aktualnej pozycji GPS pojazdu.",
        "Будую маршрут через усі точки...": "Wyznaczam trasę przez wszystkie punkty...",
        "Зберігаю активний маршрут...": "Zapisuję aktywną trasę...",
        "До наступної вигрузки:": "Do następnego rozładunku:", "До останньої вигрузки:": "Do ostatniego rozładunku:",
        "Тахограф і час водіїв": "Tachograf i czas kierowców", "Тахограф — технічні дані": "Tachograf — dane techniczne",
        "Планування маршруту": "Planowanie trasy", "Початок маршруту — автомобіль": "Początek trasy — pojazd",
        "Розвізний маршрут": "Trasa dostaw", "Адреси й часові вікна": "Adresy i okna czasowe",
        "Видалити маршрут автомобіля": "Usuń trasę pojazdu", "Змінити порядок адрес": "Zmień kolejność adresów",
        "Очистити всі адреси": "Wyczyść wszystkie adresy", "Прорахувати всі доставки": "Oblicz wszystkie dostawy",
        "Розвантаження, min": "Rozładunek, min", "Добовий відпочинок": "Odpoczynek dobowy",
        "Вулиця або точна адреса": "Ulica lub dokładny adres", "Прокласти маршрут": "Wyznacz trasę",
        "Варіант маршруту": "Wariant trasy", "Платні дороги дозволені": "Drogi płatne dozwolone",
        "Уникати платних доріг": "Unikaj dróg płatnych", "Безплатний": "Bezpłatny", "Швидкий": "Szybki",
        "Витрата, л/100 км": "Spalanie, l/100 km", "Ціна за літр": "Cena za litr", "Валюта": "Waluta",
        "Тип автомобіля": "Typ pojazdu", "Тип транспорту": "Typ transportu", "Місто": "Miasto",
        "Вулиця / адреса": "Ulica / adres", "Шукати": "Szukaj", "Знайти адресу": "Znajdź adres",
        "Виміряти маршрут": "Zmierz trasę", "Очистити карту": "Wyczyść mapę", "Очистити маршрут": "Wyczyść trasę",
        "Автомобіль:": "Pojazd:", "Водій:": "Kierowca:", "Відстань:": "Odległość:", "Паливо:": "Paliwo:",
        "Чистий час керування:": "Czysty czas jazdy:", "Планований виїзд:": "Planowany wyjazd:",
        "Сьогодні вже пройдено:": "Dzisiaj już przejechano:", "Фізично вільний:": "Fizycznie wolny:",
        "Перерв 45 хв:": "Przerw 45 min:", "без часового вікна": "bez okna czasowego",
        "від попередньої точки": "od poprzedniego punktu", "виїзд": "wyjazd", "керування:": "jazda:", "керування.": "jazdy.",
        "Панель керування": "Panel sterowania", "Автомобілі": "Pojazdy", "Паливо": "Paliwo", "Оплата доріг": "Opłaty drogowe",
        "Кабінет водія": "Panel kierowcy", "Кабінет логіста": "Panel spedytora", "Директор": "Dyrektor", "Логіст": "Spedytor", "Водій": "Kierowca",
        "Інформація про дороги": "Informacje o drogach", "Історія": "Historia", "Швидкість:": "Prędkość:", "Статус:": "Status:",
        "Їде": "Jedzie", "Стоїть": "Stoi", "Інша робота": "Inna praca", "Відпочинок": "Odpoczynek",
        "Немає даних": "Brak danych", "Немає координат": "Brak współrzędnych", "Водія не визначено": "Nie określono kierowcy",
        "Картка водія не вставлена в тахограф.": "Карта водія nie jest włożona do tachografu.",
        "Термін дії картки водія закінчився.": "Карта водія straciła ważność.", "Без попереджень": "Brak ostrzeżeń",
        "Готовність": "Gotowość", "Керування": "Jazda", "Доставка": "Dostawa", "Ще не вигружено": "Jeszcze nierozładowane",
        "Бус до 3,5 т": "Bus do 3,5 t", "Вантажний до 7,5 т": "Ciężarowy do 7,5 t", "Вантажний до 12 т": "Ciężarowy do 12 t",
        "Вантажний до 18 т": "Ciężarowy do 18 t", "Вантажний до 26 т": "Ciężarowy do 26 t", "Фура до 40 т": "Zestaw do 40 t", "Понад 40 т": "Powyżej 40 t",
        " км/год": " km/h", " км": " km", " л/100 км": " l/100 km", " л ": " l ", " год ": " godz. ", " хв": " min",
    },
    "en": {
        'Дані отримуються безпосередньо з Navirec:': 'Data is retrieved directly from Navirec:',
        'водії, картки та last_driver_states.': 'drivers, cards and last_driver_states.',
        'Час тахографа не вираховується з GPS.': 'Tachograph time is not calculated from GPS.',
        'Саме ці значення надалі використовуватимуться': 'These values will be used',
        'для перевірки, чи можна брати рейс Trans.eu.': 'to check whether a Trans.eu job can be accepted.',
        'Технічна перевірка даних': 'Technical data check',
        'Автомобілі': 'Vehicles',
        'Картка вставлена': 'Card inserted',
        'Станів водіїв Navirec': 'Navirec driver states',
        'Попередження': 'Warnings',
        'Водій': 'Driver',
        'Картка водія': 'Driver card',
        'Вставлена': 'Inserted',
        'Оновлено': 'Updated',
        'Стан часу': 'Time status',
        'До наступної перерви': 'Until next break',
        'Залишок безперервного керування': 'Continuous driving remaining',
        'Залишок керування сьогодні': 'Driving remaining today',
        'Залишок у зміні': 'Driving remaining in shift',
        'Залишок керування цього тижня': 'Driving remaining this week',
        'До денного відпочинку': 'Until daily rest',
        'До тижневого відпочинку': 'Until weekly rest',
        'Керування сьогодні': 'Driving today',
        'Керування за тиждень': 'Driving this week',
        'Керування за два тижні': 'Driving over two weeks',
        'Робота сьогодні': 'Work today',
        'Перерва / відпочинок': 'Break / rest',
        'Залишок поточного відпочинку': 'Current rest remaining',
        'Картка дійсна до': 'Card valid until',
        'TRANVIQ IQ — баланс відпочинку': 'TRANVIQ IQ — rest balance',
        'Мінімальний добовий відпочинок зараз': 'Minimum daily rest now',
        'Мінімальний тижневий відпочинок Navirec': 'Minimum weekly rest from Navirec',
        'Відкрита компенсація тижневого відпочинку': 'Open weekly-rest compensation',
        '45 год + весь відкритий борг': '45 h + all open compensation',
        'Кінець останнього добового відпочинку': 'End of last daily rest',
        'Кінець останнього тижневого відпочинку': 'End of last weekly rest',
        'IQ використовує тільки підтверджені поля Navirec.': 'IQ uses only confirmed Navirec fields.',
        'Лічильник використаних 9-годинних добових відпочинків': 'The counter of used 9-hour reduced daily rests',
        'додамо до постійної пам’яті TRANVIQ, щоб він не губився': 'will be added to TRANVIQ persistent memory so it is not lost',
        'після перезапуску сервера.': 'after a server restart.',
        'Активних попереджень немає.': 'No active warnings.',
        'Navirec не повернув повний стан водія. Показані лише дані, наявні в автомобілі.': 'Navirec did not return the full driver state. Only data available in the vehicle is shown.',
        'Дані тахографа застарілі: останнє оновлення': 'Tachograph data is stale: last update',
        'Помилки Navirec': 'Navirec errors',
        "Дозволяю передати картографічним сервісам лише адреси цього маршруту": "I allow only the addresses of this route to be sent to map services",
        "Для карти використовуються лише адреси й часові вікна. Імена та телефони не передаються.": "Only addresses and time windows are used for the map. Names and phone numbers are not shared.",
        "Вартість є орієнтовною. Вона залежить від ваги, осей, екологічного класу, віньєт і способу оплати.": "The cost is an estimate. It depends on weight, axles, emission class, vignettes and payment method.",
        "Маршрут узгоджено з актуальним тахографом.": "The route is consistent with current tachograph data.",
        "Наступне завантаження можна планувати:": "Next loading can be planned:", "Рекомендований наступний виїзд:": "Recommended next departure:",
        "Початок сьогоднішньої роботи:": "Start of today’s work:", "Після завершення залишається щонайменше": "After completion, at least",
        "Орієнтовна оплата доріг:": "Estimated road toll:", "До наступної вигрузки:": "To next unloading:", "До останньої вигрузки:": "To final unloading:",
        "Тахограф і час водіїв": "Tachograph and driver time", "Тахограф — технічні дані": "Tachograph — technical data",
        "Планування маршруту": "Route planning", "Початок маршруту — автомобіль": "Route start — vehicle", "Розвізний маршрут": "Delivery route",
        "Адреси й часові вікна": "Addresses and time windows", "Видалити маршрут автомобіля": "Delete vehicle route", "Змінити порядок адрес": "Change address order",
        "Очистити всі адреси": "Clear all addresses", "Прорахувати всі доставки": "Calculate all deliveries", "Розвантаження, min": "Unloading, min",
        "Добовий відпочинок": "Daily rest", "Вулиця або точна адреса": "Street or exact address", "Прокласти маршрут": "Calculate route",
        "Варіант маршруту": "Route option", "Платні дороги дозволені": "Toll roads allowed", "Уникати платних доріг": "Avoid toll roads", "Безплатний": "Toll-free", "Швидкий": "Fast",
        "Витрата, л/100 км": "Consumption, l/100 km", "Ціна за літр": "Price per litre", "Валюта": "Currency", "Тип автомобіля": "Vehicle type", "Тип транспорту": "Transport type",
        "Місто": "City", "Вулиця / адреса": "Street / address", "Почніть вводити назву міста": "Start typing a city name", "Спочатку виберіть місто": "Select a city first", "Шукати": "Search", "Знайти адресу": "Find address", "Виміряти маршрут": "Measure route", "Очистити карту": "Clear map",
        "Автомобіль:": "Vehicle:", "Водій:": "Driver:", "Відстань:": "Distance:", "Паливо:": "Fuel:", "Чистий час керування:": "Pure driving time:", "Планований виїзд:": "Planned departure:",
        "Сьогодні вже пройдено:": "Distance today:", "Фізично вільний:": "Physically available:", "Перерв 45 хв:": "45-min breaks:", "без часового вікна": "no time window",
        "від попередньої точки": "from previous point", "виїзд": "departure", "керування:": "driving:", "керування.": "driving.",
        "Панель керування": "Control panel", "Автомобілі": "Vehicles", "Паливо": "Fuel", "Оплата доріг": "Road tolls", "Кабінет водія": "Driver panel", "Кабінет логіста": "Dispatcher panel",
        "Директор": "Director", "Логіст": "Dispatcher", "Водій": "Driver", "Інформація про дороги": "Road information", "Історія": "History", "Швидкість:": "Speed:", "Статус:": "Status:",
        "Їде": "Driving", "Стоїть": "Stopped", "Інша робота": "Other work", "Відпочинок": "Rest", "Немає даних": "No data", "Немає координат": "No coordinates", "Водія не визначено": "Driver not identified",
        "Картка водія не вставлена в тахограф.": "Driver card is not inserted in the tachograph.", "Без попереджень": "No warnings", "Готовність": "Availability", "Керування": "Driving",
        "Бус до 3,5 т": "Van up to 3.5 t", "Вантажний до 7,5 т": "Truck up to 7.5 t", "Вантажний до 12 т": "Truck up to 12 t", "Вантажний до 18 т": "Truck up to 18 t", "Вантажний до 26 т": "Truck up to 26 t", "Фура до 40 т": "Combination up to 40 t", "Понад 40 т": "Over 40 t",
        " км/год": " km/h", " км": " km", " л/100 км": " l/100 km", " л ": " l ", " год ": " h ", " хв": " min",   "керування": "driving"
    },
    "de": {
        'Дані отримуються безпосередньо з Navirec:': 'Die Daten werden direkt von Navirec abgerufen:',
        'водії, картки та last_driver_states.': 'Fahrer, Karten und last_driver_states.',
        'Час тахографа не вираховується з GPS.': 'Die Tachographenzeit wird nicht aus GPS-Daten berechnet.',
        'Саме ці значення надалі використовуватимуться': 'Diese Werte werden verwendet,',
        'для перевірки, чи можна брати рейс Trans.eu.': 'um zu prüfen, ob ein Trans.eu-Auftrag angenommen werden kann.',
        'Технічна перевірка даних': 'Technische Datenprüfung',
        'Автомобілі': 'Fahrzeuge',
        'Картка вставлена': 'Karte eingelegt',
        'Станів водіїв Navirec': 'Navirec-Fahrerzustände',
        'Попередження': 'Warnungen',
        'Водій': 'Fahrer',
        'Картка водія': 'Fahrerkarte',
        'Вставлена': 'Eingelegt',
        'Оновлено': 'Aktualisiert',
        'Стан часу': 'Zeitstatus',
        'До наступної перерви': 'Bis zur nächsten Pause',
        'Залишок безперервного керування': 'Verbleibende ununterbrochene Fahrzeit',
        'Залишок керування сьогодні': 'Verbleibende Fahrzeit heute',
        'Залишок у зміні': 'Verbleibende Fahrzeit in der Schicht',
        'Залишок керування цього тижня': 'Verbleibende Fahrzeit diese Woche',
        'До денного відпочинку': 'Bis zur täglichen Ruhezeit',
        'До тижневого відпочинку': 'Bis zur wöchentlichen Ruhezeit',
        'Керування сьогодні': 'Fahrzeit heute',
        'Керування за тиждень': 'Fahrzeit diese Woche',
        'Керування за два тижні': 'Fahrzeit in zwei Wochen',
        'Робота сьогодні': 'Arbeit heute',
        'Перерва / відпочинок': 'Pause / Ruhezeit',
        'Залишок поточного відпочинку': 'Verbleibende aktuelle Ruhezeit',
        'Картка дійсна до': 'Karte gültig bis',
        'TRANVIQ IQ — баланс відпочинку': 'TRANVIQ IQ — Ruhezeitbilanz',
        'Мінімальний добовий відпочинок зараз': 'Minimale tägliche Ruhezeit jetzt',
        'Мінімальний тижневий відпочинок Navirec': 'Minimale wöchentliche Ruhezeit laut Navirec',
        'Відкрита компенсація тижневого відпочинку': 'Offener Ausgleich der Wochenruhezeit',
        '45 год + весь відкритий борг': '45 Std. + gesamter offener Ausgleich',
        'Кінець останнього добового відпочинку': 'Ende der letzten täglichen Ruhezeit',
        'Кінець останнього тижневого відпочинку': 'Ende der letzten wöchentlichen Ruhezeit',
        'IQ використовує тільки підтверджені поля Navirec.': 'IQ verwendet nur bestätigte Navirec-Felder.',
        'Лічильник використаних 9-годинних добових відпочинків': 'Der Zähler der genutzten verkürzten 9-Stunden-Tagesruhezeiten',
        'додамо до постійної пам’яті TRANVIQ, щоб він не губився': 'wird im persistenten TRANVIQ-Speicher hinterlegt, damit er nicht verloren geht',
        'після перезапуску сервера.': 'nach einem Serverneustart.',
        'Активних попереджень немає.': 'Keine aktiven Warnungen.',
        'Navirec не повернув повний стан водія. Показані лише дані, наявні в автомобілі.': 'Navirec hat keinen vollständigen Fahrerstatus geliefert. Es werden nur im Fahrzeug verfügbare Daten angezeigt.',
        'Дані тахографа застарілі: останнє оновлення': 'Tachographendaten sind veraltet: letzte Aktualisierung',
        'Помилки Navirec': 'Navirec-Fehler',
        "Дозволяю передати картографічним сервісам лише адреси цього маршруту": "Ich erlaube, ausschließlich die Adressen dieser Route an Kartendienste zu übermitteln",
        "Для карти використовуються лише адреси й часові вікна. Імена та телефони не передаються.": "Für die Karte werden nur Adressen und Zeitfenster verwendet. Namen und Telefonnummern werden nicht übermittelt.",
        "Вартість є орієнтовною. Вона залежить від ваги, осей, екологічного класу, віньєт і способу оплати.": "Die Kosten sind Richtwerte. Sie hängen von Gewicht, Achsen, Emissionsklasse, Vignetten und Zahlungsart ab.",
        "Маршрут узгоджено з актуальним тахографом.": "Die Route stimmt mit den aktuellen Tachographendaten überein.",
        "Наступне завантаження можна планувати:": "Nächste Beladung kann geplant werden:", "Рекомендований наступний виїзд:": "Empfohlene nächste Abfahrt:",
        "Початок сьогоднішньої роботи:": "Beginn der heutigen Arbeit:", "Після завершення залишається щонайменше": "Nach Abschluss verbleiben mindestens",
        "Орієнтовна оплата доріг:": "Geschätzte Maut:", "До наступної вигрузки:": "Bis zur nächsten Entladung:", "До останньої вигрузки:": "Bis zur letzten Entladung:",
        "Тахограф і час водіїв": "Tachograph und Fahrerzeiten", "Тахограф — технічні дані": "Tachograph — technische Daten",
        "Планування маршруту": "Routenplanung", "Початок маршруту — автомобіль": "Routenstart — Fahrzeug", "Розвізний маршрут": "Auslieferungsroute",
        "Адреси й часові вікна": "Adressen und Zeitfenster", "Видалити маршрут автомобіля": "Fahrzeugroute löschen", "Змінити порядок адрес": "Adressreihenfolge ändern",
        "Очистити всі адреси": "Alle Adressen löschen", "Прорахувати всі доставки": "Alle Lieferungen berechnen", "Розвантаження, min": "Entladung, Min.",
        "Добовий відпочинок": "Tägliche Ruhezeit", "Вулиця або точна адреса": "Straße oder genaue Adresse", "Прокласти маршрут": "Route berechnen",
        "Варіант маршруту": "Routenoption", "Платні дороги дозволені": "Mautstraßen erlaubt", "Уникати платних доріг": "Mautstraßen vermeiden", "Безплатний": "Mautfrei", "Швидкий": "Schnell",
        "Витрата, л/100 км": "Verbrauch, l/100 km", "Ціна за літр": "Preis pro Liter", "Валюта": "Währung", "Тип автомобіля": "Fahrzeugtyp", "Тип транспорту": "Transportart",
        "Місто": "Stadt", "Вулиця / адреса": "Straße / Adresse", "Шукати": "Suchen", "Знайти адресу": "Adresse suchen", "Виміряти маршрут": "Route messen", "Очистити карту": "Karte löschen",
        "Автомобіль:": "Fahrzeug:", "Водій:": "Fahrer:", "Відстань:": "Entfernung:", "Паливо:": "Kraftstoff:", "Чистий час керування:": "Reine Fahrzeit:", "Планований виїзд:": "Geplante Abfahrt:",
        "Сьогодні вже пройдено:": "Heute bereits gefahren:", "Фізично вільний:": "Physisch verfügbar:", "Перерв 45 хв:": "45-Min.-Pausen:", "без часового вікна": "ohne Zeitfenster",
        "від попередньої точки": "vom vorherigen Punkt", "виїзд": "Abfahrt", "керування:": "Fahrt:", "керування.": "Fahrt.",
        "Панель керування": "Steuerung", "Автомобілі": "Fahrzeuge", "Паливо": "Kraftstoff", "Оплата доріг": "Maut", "Кабінет водія": "Fahrerbereich", "Кабінет логіста": "Disponentenbereich",
        "Директор": "Direktor", "Логіст": "Disponent", "Водій": "Fahrer", "Інформація про дороги": "Straßeninformationen", "Історія": "Historie", "Швидкість:": "Geschwindigkeit:", "Статус:": "Status:",
        "Їде": "Fährt", "Стоїть": "Steht", "Інша робота": "Andere Arbeit", "Відпочинок": "Ruhezeit", "Немає даних": "Keine Daten", "Немає координат": "Keine Koordinaten", "Водія не визначено": "Fahrer nicht erkannt",
        "Картка водія не вставлена в тахограф.": "Fahrerkarte ist nicht im Tachographen eingelegt.", "Без попереджень": "Keine Warnungen", "Готовність": "Verfügbarkeit", "Керування": "Fahren",
        "Бус до 3,5 т": "Transporter bis 3,5 t", "Вантажний до 7,5 т": "Lkw bis 7,5 t", "Вантажний до 12 т": "Lkw bis 12 t", "Вантажний до 18 т": "Lkw bis 18 t", "Вантажний до 26 т": "Lkw bis 26 t", "Фура до 40 т": "Zug bis 40 t", "Понад 40 т": "Über 40 t",
        " км/год": " km/h", " км": " km", " л/100 км": " l/100 km", " л ": " l ", " год ": " Std. ", " хв": " Min.",   "керування": "Fahrt"
    }
}


# Full-page localization supplements.  Keep complete UI phrases here instead
# of replacing fragments inside words (which previously produced mixed text).
FULL_PAGE_TRANSLATIONS = {
    "pl": {
        "Компанія:": "Firma:", "Автомобілів у системі:": "Pojazdy w systemie:",
        "Є дані Navirec": "Dane Navirec dostępne", "Немає поточного стану": "Brak aktualnego stanu",
        "Відкрити": "Otwórz", "Автомобіль": "Pojazd", "Швидкість": "Prędkość", "Деталі": "Szczegóły",
        "Тут поки показується поточний рівень\n            палива з Navirec.\n            Збільшення рівня ще не вважаємо\n            автоматично заправкою.": "Na razie wyświetlany jest bieżący poziom paliwa z Navirec. Zwiększenia poziomu nie traktujemy jeszcze automatycznie jako tankowania.",
        "Тут поки показується поточний рівень палива з Navirec. Збільшення рівня ще не вважаємо автоматично заправкою.": "Na razie wyświetlany jest bieżący poziom paliwa z Navirec. Zwiększenia poziomu nie traktujemy jeszcze automatycznie jako tankowania.",
        "Дата": "Data", "Показати історію": "Pokaż historię", "GPS-точок": "Punkty GPS", "Початок": "Początek", "Кінець": "Koniec",
        "Відстань": "Odległość", "Макс. швидкість": "Maks. prędkość", "Паливо на початку": "Paliwo na początku", "Паливо в кінці": "Paliwo na końcu",
        "Рух": "Jazda", "Час руху": "Czas jazdy", "Стоянка": "Postój", "Холостий хід": "Praca na biegu jałowym", "Витрата": "Zużycie",
        "Останні 100 GPS-точок": "Ostatnie 100 punktów GPS", "Час": "Czas", "Стан": "Stan", "Координати": "Współrzędne",
        "Оплата доріг і віньєти": "Opłaty drogowe i winiety",
        "Вибір тарифу залежить від ваги, кількості осей,\n            висоти, екологічного класу й країни.\n            Купуйте тільки на офіційних сторінках операторів.": "Wybór taryfy zależy od masy, liczby osi, wysokości, klasy emisji i kraju. Kupuj wyłącznie na oficjalnych stronach operatorów.",
        "Розрахувати маршрут, паливо й оплату доріг": "Oblicz trasę, paliwo i opłaty drogowe",
        "🇵🇱 Польща": "🇵🇱 Polska", "🇩🇪 Німеччина": "🇩🇪 Niemcy", "🇦🇹 Австрія": "🇦🇹 Austria", "🇨🇿 Чехія": "🇨🇿 Czechy",
        "🇸🇰 Словаччина": "🇸🇰 Słowacja", "🇭🇺 Угорщина": "🇭🇺 Węgry", "🇸🇮 Словенія": "🇸🇮 Słowenia", "🇨🇭 Швейцарія": "🇨🇭 Szwajcaria",
        "🇷🇴 Румунія": "🇷🇴 Rumunia", "🇧🇬 Болгарія": "🇧🇬 Bułgaria", "🇧🇪 Бельгія": "🇧🇪 Belgia", "🇫🇷 🇮🇹 🇪🇸 🇵🇹 Західна Європа": "🇫🇷 🇮🇹 🇪🇸 🇵🇹 Europa Zachodnia",
        "До 3,5 т: окремі платні автомагістралі. Понад 3,5 т: система e-TOLL.": "Do 3,5 t: wybrane płatne autostrady. Powyżej 3,5 t: system e-TOLL.",
        "До 3,5 т: загальної віньєтки немає. Понад 3,5 т: вантажний дорожній збір Toll Collect.": "Do 3,5 t: brak ogólnej winiety. Powyżej 3,5 t: opłata drogowa Toll Collect.",
        "До 3,5 т: електронна віньєтка. Понад 3,5 т: GO-Box і кілометрова оплата.": "Do 3,5 t: e-winieta. Powyżej 3,5 t: GO-Box i opłata kilometrowa.",
        "До 3,5 т: електронна віньєтка. Понад 3,5 т: електронна система MYTO CZ.": "Do 3,5 t: e-winieta. Powyżej 3,5 t: elektroniczny system MYTO CZ.",
        "До 3,5 т: електронна віньєтка. Понад 3,5 т: кілометрова система eMyto.": "Do 3,5 t: e-winieta. Powyżej 3,5 t: system kilometrowy eMyto.",
        "До 3,5 т: e-Matrica, категорія залежить від авто. Понад 3,5 т: HU-GO.": "Do 3,5 t: e-Matrica, kategoria zależy od pojazdu. Powyżej 3,5 t: HU-GO.",
        "До 3,5 т: e-vignette 2A або 2B. Понад 3,5 т: DarsGo.": "Do 3,5 t: e-winieta 2A lub 2B. Powyżej 3,5 t: DarsGo.",
        "До 3,5 т: швейцарська віньєтка. Понад 3,5 т: збір для важкого транспорту.": "Do 3,5 t: winieta szwajcarska. Powyżej 3,5 t: opłata dla ciężkiego transportu.",
        "Rovinieta потрібна для більшості транспортних засобів. Категорія залежить від ваги та осей.": "Rovinieta jest wymagana dla większości pojazdów. Kategoria zależy od masy i liczby osi.",
        "До 3,5 т: електронна віньєтка. Понад 3,5 т: маршрутний або кілометровий збір.": "Do 3,5 t: e-winieta. Powyżej 3,5 t: opłata trasowa lub kilometrowa.",
        "До 3,5 т: загальної віньєтки немає. Понад 3,5 т: кілометровий збір Viapass.": "Do 3,5 t: brak ogólnej winiety. Powyżej 3,5 t: opłata kilometrowa Viapass.",
        "У Франції, Італії, Іспанії та Португалії оплата часто стягується за конкретні ділянки, мости або тунелі, а не загальною віньєткою.": "We Francji, Włoszech, Hiszpanii i Portugalii opłaty są często pobierane za konkretne odcinki, mosty lub tunele zamiast ogólnej winiety.",
        "Відкрити e-TOLL": "Otwórz e-TOLL", "Відкрити Toll Collect": "Otwórz Toll Collect", "Купити в ASFINAG": "Kup w ASFINAG",
        "Купити e-vignette": "Kup e-winietę", "Купити eZnamka": "Kup eZnamka", "Купити e-Matrica": "Kup e-Matrica", "Купити Rovinieta": "Kup Rovinieta",
        "Відкрити BG Toll": "Otwórz BG Toll", "Відкрити Viapass": "Otwórz Viapass",
    },
    "en": {
        "Тут поки показується поточний рівень\n            палива з Navirec.\n            Збільшення рівня ще не вважаємо\n            автоматично заправкою.": "The current fuel level from Navirec is shown here. An increase in fuel level is not yet automatically treated as a refuelling event.",
        "Компанія:": "Company:", "Автомобілів у системі:": "Vehicles in system:", "Є дані Navirec": "Navirec data available", "Немає поточного стану": "No current state", "Відкрити": "Open",
        "Автомобіль": "Vehicle", "Швидкість": "Speed", "Деталі": "Details", "Дата": "Date", "Показати історію": "Show history", "GPS-точок": "GPS points",
        "Початок": "Start", "Кінець": "End", "Відстань": "Distance", "Макс. швидкість": "Max. speed", "Паливо на початку": "Fuel at start", "Паливо в кінці": "Fuel at end",
        "Рух": "Driving", "Час руху": "Driving time", "Стоянка": "Parking", "Холостий хід": "Idling", "Витрата": "Consumption", "Останні 100 GPS-точок": "Last 100 GPS points",
        "Час": "Time", "Стан": "State", "Координати": "Coordinates", "Оплата доріг і віньєти": "Road tolls and vignettes",
        "Розрахувати маршрут, паливо й оплату доріг": "Calculate route, fuel and road tolls",
        "Вибір тарифу залежить від ваги, кількості осей,\n            висоти, екологічного класу й країни.\n            Купуйте тільки на офіційних сторінках операторів.": "The toll rate depends on vehicle weight, number of axles, height, emission class and country. Buy vignettes and toll products only from official operator websites.",
        "🇵🇱 Польща": "🇵🇱 Poland", "🇩🇪 Німеччина": "🇩🇪 Germany", "🇦🇹 Австрія": "🇦🇹 Austria", "🇨🇿 Чехія": "🇨🇿 Czech Republic",
        "🇸🇰 Словаччина": "🇸🇰 Slovakia", "🇭🇺 Угорщина": "🇭🇺 Hungary", "🇸🇮 Словенія": "🇸🇮 Slovenia", "🇨🇭 Швейцарія": "🇨🇭 Switzerland",
        "🇷🇴 Румунія": "🇷🇴 Romania", "🇧🇬 Болгарія": "🇧🇬 Bulgaria", "🇧🇪 Бельгія": "🇧🇪 Belgium", "🇫🇷 🇮🇹 🇪🇸 🇵🇹 Західна Європа": "🇫🇷 🇮🇹 🇪🇸 🇵🇹 Western Europe",
        "До 3,5 т: окремі платні автомагістралі. Понад 3,5 т: система e-TOLL.": "Up to 3.5 t: selected motorways are tolled. Over 3.5 t: the e-TOLL system applies.",
        "До 3,5 т: загальної віньєтки немає. Понад 3,5 т: вантажний дорожній збір Toll Collect.": "Up to 3.5 t: no general vignette. Over 3.5 t: Toll Collect road tolls apply to goods vehicles.",
        "До 3,5 т: електронна віньєтка. Понад 3,5 т: GO-Box і кілометрова оплата.": "Up to 3.5 t: electronic vignette. Over 3.5 t: GO-Box and distance-based tolling.",
        "До 3,5 т: електронна віньєтка. Понад 3,5 т: електронна система MYTO CZ.": "Up to 3.5 t: electronic vignette. Over 3.5 t: the MYTO CZ electronic toll system.",
        "До 3,5 т: електронна віньєтка. Понад 3,5 т: кілометрова система eMyto.": "Up to 3.5 t: electronic vignette. Over 3.5 t: the eMyto distance-based toll system.",
        "До 3,5 т: e-Matrica, категорія залежить від авто. Понад 3,5 т: HU-GO.": "Up to 3.5 t: e-Matrica; the category depends on the vehicle. Over 3.5 t: HU-GO.",
        "До 3,5 т: e-vignette 2A або 2B. Понад 3,5 т: DarsGo.": "Up to 3.5 t: e-vignette category 2A or 2B. Over 3.5 t: DarsGo.",
        "До 3,5 т: швейцарська віньєтка. Понад 3,5 т: збір для важкого транспорту.": "Up to 3.5 t: Swiss motorway vignette. Over 3.5 t: heavy vehicle charge applies.",
        "Rovinieta потрібна для більшості транспортних засобів. Категорія залежить від ваги та осей.": "A Rovinieta is required for most vehicles. The category depends on vehicle weight and number of axles.",
        "До 3,5 т: електронна віньєтка. Понад 3,5 т: маршрутний або кілометровий збір.": "Up to 3.5 t: electronic vignette. Over 3.5 t: route-based or distance-based tolling.",
        "До 3,5 т: загальної віньєтки немає. Понад 3,5 т: кілометровий збір Viapass.": "Up to 3.5 t: no general vignette. Over 3.5 t: Viapass distance-based tolling.",
        "У Франції, Італії, Іспанії та Португалії оплата часто стягується за конкретні ділянки, мости або тунелі, а не загальною віньєткою.": "In France, Italy, Spain and Portugal, tolls are often charged for specific road sections, bridges or tunnels rather than through a nationwide vignette.",
        "Відкрити e-TOLL": "Open e-TOLL", "Відкрити Toll Collect": "Open Toll Collect", "Купити в ASFINAG": "Buy from ASFINAG",
        "Купити e-vignette": "Buy e-vignette", "Купити eZnamka": "Buy eZnamka", "Купити e-Matrica": "Buy e-Matrica", "Купити Rovinieta": "Buy Rovinieta",
        "Відкрити BG Toll": "Open BG Toll", "Відкрити Viapass": "Open Viapass", "Інформація про дороги": "Road information",
        "Тут поки показується поточний рівень палива з Navirec. Збільшення рівня ще не вважаємо автоматично заправкою.": "For now, the current fuel level from Navirec is shown. An increase in level is not yet automatically treated as refuelling.",
    },
    "de": {
        "Тут поки показується поточний рівень\n            палива з Navirec.\n            Збільшення рівня ще не вважаємо\n            автоматично заправкою.": "Derzeit wird der aktuelle Kraftstoffstand von Navirec angezeigt. Ein Anstieg des Kraftstoffstands wird noch nicht automatisch als Tankvorgang erkannt.",
        "Компанія:": "Firma:", "Автомобілів у системі:": "Fahrzeuge im System:", "Є дані Navirec": "Navirec-Daten verfügbar", "Немає поточного стану": "Kein aktueller Status", "Відкрити": "Öffnen",
        "Автомобіль": "Fahrzeug", "Швидкість": "Geschwindigkeit", "Деталі": "Details", "Дата": "Datum", "Показати історію": "Historie anzeigen", "GPS-точок": "GPS-Punkte",
        "Початок": "Start", "Кінець": "Ende", "Відстань": "Entfernung", "Макс. швидкість": "Max. Geschwindigkeit", "Паливо на початку": "Kraftstoff am Anfang", "Паливо в кінці": "Kraftstoff am Ende",
        "Рух": "Fahrt", "Час руху": "Fahrzeit", "Стоянка": "Standzeit", "Холостий хід": "Leerlauf", "Витрата": "Verbrauch", "Останні 100 GPS-точок": "Letzte 100 GPS-Punkte",
        "Час": "Zeit", "Стан": "Status", "Координати": "Koordinaten", "Оплата доріг і віньєти": "Maut und Vignetten",
        "Розрахувати маршрут, паливо й оплату доріг": "Route, Kraftstoff und Maut berechnen",
        "Тут поки показується поточний рівень палива з Navirec. Збільшення рівня ще не вважаємо автоматично заправкою.": "Derzeit wird der aktuelle Kraftstoffstand aus Navirec angezeigt. Ein Anstieg wird noch nicht automatisch als Betankung gewertet.",
    }
}
for _lang, _items in FULL_PAGE_TRANSLATIONS.items():
    GLOBAL_UI_TRANSLATIONS.setdefault(_lang, {}).update(_items)


# German cleanup for the remaining visible Maut/Tachograph strings.
# Text only: no routing, GPS, Navirec or calculation logic is changed.
GLOBAL_UI_TRANSLATIONS.setdefault("de", {}).update({
    "Вибір тарифу залежить від ваги, кількості осей,\n            висоти, екологічного класу й країни.\n            Купуйте тільки на офіційних сторінках операторів.": "Die Tarifwahl hängt von Gewicht, Achsanzahl, Höhe, Emissionsklasse und Land ab. Kaufen Sie ausschließlich über die offiziellen Seiten der Betreiber.",
    "До 3,5 т: окремі платні автомагістралі. Понад 3,5 т: система e-TOLL.": "Bis 3,5 t: einzelne Autobahnen sind mautpflichtig. Über 3,5 t: e-TOLL-System.",
    "До 3,5 т: загальної віньєтки немає. Понад 3,5 т: вантажний дорожній збір Toll Collect.": "Bis 3,5 t: keine allgemeine Vignettenpflicht. Über 3,5 t: Lkw-Maut über Toll Collect.",
    "До 3,5 т: електронна віньєтка. Понад 3,5 т: GO-Box і кілометрова оплата.": "Bis 3,5 t: elektronische Vignette. Über 3,5 t: GO-Box und streckenabhängige Maut.",
    "До 3,5 т: електронна віньєтка. Понад 3,5 т: електронна система MYTO CZ.": "Bis 3,5 t: elektronische Vignette. Über 3,5 t: elektronisches Mautsystem MYTO CZ.",
    "До 3,5 т: електронна віньєтка. Понад 3,5 т: кілометрова система eMyto.": "Bis 3,5 t: elektronische Vignette. Über 3,5 t: streckenabhängiges eMyto-System.",
    "До 3,5 т: e-Matrica, категорія залежить від авто. Понад 3,5 т: HU-GO.": "Bis 3,5 t: e-Matrica; die Kategorie hängt vom Fahrzeug ab. Über 3,5 t: HU-GO.",
    "До 3,5 т: e-vignette 2A або 2B. Понад 3,5 т: DarsGo.": "Bis 3,5 t: E-Vignette der Kategorie 2A oder 2B. Über 3,5 t: DarsGo.",
    "До 3,5 т: швейцарська віньєтка. Понад 3,5 т: збір для важкого транспорту.": "Bis 3,5 t: Schweizer Autobahnvignette. Über 3,5 t: Schwerverkehrsabgabe.",
    "Rovinieta потрібна для більшості транспортних засобів. Категорія залежить від ваги та осей.": "Für die meisten Fahrzeuge ist eine Rovinieta erforderlich. Die Kategorie hängt von Gewicht und Achsanzahl ab.",
    "До 3,5 т: електронна віньєтка. Понад 3,5 т: маршрутний або кілометровий збір.": "Bis 3,5 t: elektronische Vignette. Über 3,5 t: strecken- oder kilometerabhängige Maut.",
    "До 3,5 т: загальної віньєтки немає. Понад 3,5 т: кілометровий збір Viapass.": "Bis 3,5 t: keine allgemeine Vignettenpflicht. Über 3,5 t: kilometerabhängige Maut über Viapass.",
    "У Франції, Італії, Іспанії та Португалії оплата часто стягується за конкретні ділянки, мости або тунелі, а не загальною віньєткою.": "In Frankreich, Italien, Spanien und Portugal wird die Maut häufig für bestimmte Streckenabschnitte, Brücken oder Tunnel erhoben und nicht über eine allgemeine Vignette.",
    "Відкрити e-TOLL": "e-TOLL öffnen",
    "Відкрити Toll Collect": "Toll Collect öffnen",
    "Купити в ASFINAG": "Bei ASFINAG kaufen",
    "Купити e-vignette": "E-Vignette kaufen",
    "Купити eZnamka": "eZnamka kaufen",
    "Купити e-Matrica": "e-Matrica kaufen",
    "Купити Rovinieta": "Rovinieta kaufen",
    "Відкрити BG Toll": "BG Toll öffnen",
    "Відкрити Viapass": "Viapass öffnen",
    "Інформація про дороги": "Straßeninformationen",
    "Немає часу останнього оновлення тахографа.": "Der Zeitpunkt der letzten Tachographen-Aktualisierung ist nicht verfügbar.",
})

# Complete Polish localization for Finance and Branding.  These modules
# render through page(), so translating complete labels/phrases here keeps
# their business logic untouched.
GLOBAL_UI_TRANSLATIONS.setdefault("pl", {}).update({
    "Результат у EUR": "Wynik w EUR",
    "Результат у PLN": "Wynik w PLN",
    "Доходи:": "Przychody:",
    "Витрати:": "Koszty:",
    "Підключені Gmail:": "Podłączone konta Gmail:",
    "Усі підключені пошти автоматично перевіряються кожні 15 хвилин.": "Wszystkie podłączone skrzynki są automatycznie sprawdzane co 15 minut.",
    "Підключена пошта": "Podłączona skrzynka",
    "Остання перевірка:": "Ostatnie sprawdzenie:",
    "Перевірено файлів:": "Sprawdzono plików:",
    "Нових документів:": "Nowych dokumentów:",
    "Відключити цю пошту": "Odłącz tę skrzynkę",
    "Перевірити всі пошти": "Sprawdź wszystkie skrzynki",
    "Додати ще один Gmail": "Dodaj kolejne konto Gmail",
    "Бухгалтерія": "Księgowość",
    "Вкажіть назву бухгалтерії та адресу або домен, з якого вона надсилає документи. Можна підключити декілька бухгалтерій.": "Podaj nazwę biura księgowego oraz adres lub domenę, z której wysyła dokumenty. Można podłączyć kilka biur księgowych.",
    "Назва бухгалтерії": "Nazwa biura księgowego",
    "Адреса або домен відправника": "Adres lub domena nadawcy",
    "Зберегти": "Zapisz", "Відключити": "Odłącz", "Додати бухгалтерію": "Dodaj biuro księgowe", "Додати": "Dodaj",
    "Документи бухгалтерії": "Dokumenty księgowe",
    "Податки, ZUS, зарплати, розрахунки водіїв та кадрові документи зберігаються окремо від фактур і транспортних замовлень.": "Podatki, ZUS, wynagrodzenia, rozliczenia kierowców i dokumenty kadrowe są przechowywane oddzielnie od faktur i zleceń transportowych.",
    "Тип": "Typ", "Податки": "Podatki", "Зарплати": "Wynagrodzenia", "Розрахунки водіїв": "Rozliczenia kierowców",
    "Кадрові документи": "Dokumenty kadrowe", "ZUS і страхові внески": "ZUS i składki ubezpieczeniowe",
    "Період": "Okres", "Сума": "Kwota", "Суму ще не визначено": "Kwota nie została jeszcze określona",
    "Статус": "Status", "На перевірку": "Do weryfikacji", "Отримано на Gmail": "Odebrano na Gmail", "Документ": "Dokument",
    "Otwórz документ": "Otwórz dokument", "Перевірити дані": "Sprawdź dane", "Зберегти в архіві": "Zapisz w archiwum",
    "Додати у витрати": "Dodaj do kosztów", "Додати в доходи": "Dodaj do przychodów",
    "Додати операцію": "Dodaj operację", "Дохід": "Przychód", "Витрата": "Koszt",
    "Категорія": "Kategoria", "Дохід за перевезення": "Przychód z transportu", "Ремонт і сервіс": "Naprawy i serwis",
    "Дороги й паркінги": "Drogi i parkingi", "Лізинг або кредит": "Leasing lub kredyt", "Страхування": "Ubezpieczenie",
    "Зарплата": "Wynagrodzenie", "Податки та збори": "Podatki i opłaty", "Офісні витрати": "Koszty biurowe", "Інше": "Inne",
    "Вся компанія": "Cała firma", "Опис": "Opis", "Контрагент": "Kontrahent", "Номер фактури": "Numer faktury",
    "Термін оплати": "Termin płatności", "Оплата": "Płatność", "Не оплачено": "Nieopłacone", "Оплачено": "Opłacone",
    "Останні операції": "Ostatnie operacje", "Редагувати": "Edytuj", "Оригінальний файл не прикріплений": "Nie dołączono oryginalnego pliku",
    "Фактури та транспортні замовлення з пошти": "Faktury i zlecenia transportowe z poczty",
    "Транспортне замовлення записується як дохід, а вхідна фактура — як витрата. Перед підтвердженням тип документа можна змінити через кнопку «Перевірити».": "Zlecenie transportowe jest zapisywane jako przychód, a faktura kosztowa jako koszt. Przed zatwierdzeniem typ dokumentu można zmienić przyciskiem „Sprawdź”.",
    "Програма перевірятиме дублікати за контрагентом, номером фактури, сумою та валютою.": "Program sprawdza duplikaty według kontrahenta, numeru faktury, kwoty i waluty.",
    "Нове": "Nowe", "Дані перевезення": "Dane transportu", "Номер замовника:": "Numer klienta:", "Маршрут:": "Trasa:",
    "Дати:": "Daty:", "Умови оплати:": "Warunki płatności:", "Перевірити": "Sprawdź", "Відхилити": "Odrzuć",
    "Спочатку перевірте суму.": "Najpierw sprawdź kwotę.", "Транспортне замовлення з Gmail": "Zlecenie transportowe z Gmail",
    "Фактура з Gmail": "Faktura z Gmail",
    "Логотип показується у шапці та великим прозорим фоном на вході.": "Logo jest wyświetlane w nagłówku oraz jako duże przezroczyste tło na ekranie logowania.",
    "Використовується початковий логотип.": "Używany jest domyślny logotyp.",
    "Логотип / Logo": "Logo", "PNG, JPG або WebP, максимум 2 МБ.": "PNG, JPG lub WebP, maksymalnie 2 MB.",
    "Зберегти / Save": "Zapisz"
})

# Complete English localization for Finance and Branding.
GLOBAL_UI_TRANSLATIONS.setdefault("en", {}).update({
    "Результат у EUR": "Result in EUR", "Результат у PLN": "Result in PLN",
    "Доходи:": "Income:", "Витрати:": "Expenses:", "Підключені Gmail:": "Connected Gmail accounts:",
    "Усі підключені пошти автоматично перевіряються кожні 15 хвилин.": "All connected mailboxes are automatically checked every 15 minutes.",
    "Підключена пошта": "Connected mailbox", "Остання перевірка:": "Last check:",
    "Перевірено файлів:": "Files checked:", "Нових документів:": "New documents:",
    "Відключити цю пошту": "Disconnect this mailbox", "Перевірити всі пошти": "Check all mailboxes",
    "Додати ще один Gmail": "Add another Gmail account", "Бухгалтерія": "Accounting",
    "Вкажіть назву бухгалтерії та адресу або домен, з якого вона надсилає документи. Можна підключити декілька бухгалтерій.": "Enter the accounting office name and the address or domain it uses to send documents. Multiple accounting offices can be connected.",
    "Назва бухгалтерії": "Accounting office name", "Адреса або домен відправника": "Sender address or domain",
    "Зберегти": "Save", "Відключити": "Disconnect", "Додати бухгалтерію": "Add accounting office", "Додати": "Add",
    "Документи бухгалтерії": "Accounting documents",
    "Податки, ZUS, зарплати, розрахунки водіїв та кадрові документи зберігаються окремо від фактур і транспортних замовлень.": "Taxes, ZUS, payroll, driver settlements and HR documents are stored separately from invoices and transport orders.",
    "Тип": "Type", "Податки": "Taxes", "Зарплати": "Payroll", "Розрахунки водіїв": "Driver settlements",
    "Кадрові документи": "HR documents", "ZUS і страхові внески": "ZUS and insurance contributions",
    "Період": "Period", "Сума": "Amount", "Суму ще не визначено": "Amount not yet determined",
    "Статус": "Status", "На перевірку": "For review", "Отримано на Gmail": "Received via Gmail", "Документ": "Document",
    "Otwórz документ": "Open document", "Перевірити дані": "Review data", "Зберегти в архіві": "Save to archive",
    "Додати у витрати": "Add to expenses", "Додати в доходи": "Add to income", "Додати операцію": "Add transaction",
    "Дохід": "Income", "Витрата": "Expense", "Категорія": "Category", "Дохід за перевезення": "Transport income",
    "Ремонт і сервіс": "Repairs and service", "Дороги й паркінги": "Roads and parking", "Лізинг або кредит": "Leasing or loan",
    "Страхування": "Insurance", "Зарплата": "Salary", "Податки та збори": "Taxes and fees",
    "Офісні витрати": "Office expenses", "Інше": "Other", "Вся компанія": "Entire company", "Опис": "Description",
    "Контрагент": "Counterparty", "Номер фактури": "Invoice number", "Термін оплати": "Payment due date",
    "Оплата": "Payment", "Не оплачено": "Unpaid", "Оплачено": "Paid", "Останні операції": "Recent transactions",
    "Редагувати": "Edit", "Оригінальний файл не прикріплений": "Original file not attached",
    "Фактури та транспортні замовлення з пошти": "Invoices and transport orders from email",
    "Транспортне замовлення записується як дохід, а вхідна фактура — як витрата. Перед підтвердженням тип документа можна змінити через кнопку «Перевірити».": "A transport order is recorded as income and an incoming invoice as an expense. Before confirmation, the document type can be changed using the Review button.",
    "Програма перевірятиме дублікати за контрагентом, номером фактури, сумою та валютою.": "The system checks for duplicates by counterparty, invoice number, amount and currency.",
    "Нове": "New", "Дані перевезення": "Transport details", "Номер замовника:": "Customer number:", "Маршрут:": "Route:",
    "Дати:": "Dates:", "Умови оплати:": "Payment terms:", "Перевірити": "Review", "Відхилити": "Reject",
    "Спочатку перевірте суму.": "Review the amount first.", "Транспортне замовлення з Gmail": "Transport order from Gmail",
    "Фактура з Gmail": "Invoice from Gmail",
    "Логотип показується у шапці та великим прозорим фоном на вході.": "The logo is displayed in the header and as a large transparent background on the login page.",
    "Використовується початковий логотип.": "The default logo is currently being used.",
    "Логотип / Logo": "Logo", "PNG, JPG або WebP, максимум 2 МБ.": "PNG, JPG or WebP, maximum 2 MB.",
    "Зберегти / Save": "Save"
})


# Extended German UI localization (Finance, Branding, vehicle/history and shared pages).
# Full phrases only: no route/GPS/calculation logic is changed.
GLOBAL_UI_TRANSLATIONS.setdefault("de", {}).update({
    "Результат у EUR": "Ergebnis in EUR", "Результат у PLN": "Ergebnis in PLN",
    "Доходи:": "Einnahmen:", "Витрати:": "Ausgaben:", "Фінансовий результат": "Finanzergebnis", "Ще немає даних": "Noch keine Daten",
    "Підключені Gmail:": "Verbundene Gmail-Konten:", "Усі підключені пошти автоматично перевіряються кожні 15 хвилин.": "Alle verbundenen Postfächer werden automatisch alle 15 Minuten geprüft.",
    "Підключена пошта": "Verbundenes Postfach", "Остання перевірка:": "Letzte Prüfung:", "Перевірено файлів:": "Geprüfte Dateien:", "Нових документів:": "Neue Dokumente:",
    "Відключити цю пошту": "Dieses Postfach trennen", "Перевірити всі пошти": "Alle Postfächer prüfen", "Додати ще один Gmail": "Weiteres Gmail-Konto hinzufügen",
    "Бухгалтерія": "Buchhaltung", "Вкажіть назву бухгалтерії та адресу або домен, з якого вона надсилає документи. Можна підключити декілька бухгалтерій.": "Geben Sie den Namen des Buchhaltungsbüros sowie die Adresse oder Domain an, von der die Dokumente gesendet werden. Es können mehrere Buchhaltungsbüros verbunden werden.",
    "Назва бухгалтерії": "Name des Buchhaltungsbüros", "Адреса або домен відправника": "Absenderadresse oder Domain",
    "Зберегти": "Speichern", "Відключити": "Trennen", "Додати бухгалтерію": "Buchhaltungsbüro hinzufügen", "Додати": "Hinzufügen",
    "Документи бухгалтерії": "Buchhaltungsunterlagen", "Податки, ZUS, зарплати, розрахунки водіїв та кадрові документи зберігаються окремо від фактур і транспортних замовлень.": "Steuern, ZUS, Lohnabrechnungen, Fahrerabrechnungen und Personalunterlagen werden getrennt von Rechnungen und Transportaufträgen gespeichert.",
    "Тип": "Typ", "Податки": "Steuern", "Зарплати": "Lohnabrechnungen", "Розрахунки водіїв": "Fahrerabrechnungen", "Кадрові документи": "Personalunterlagen", "ZUS і страхові внески": "ZUS und Versicherungsbeiträge",
    "Період": "Zeitraum", "Сума": "Betrag", "Суму ще не визначено": "Betrag noch nicht ermittelt", "На перевірку": "Zur Prüfung", "Отримано на Gmail": "Über Gmail erhalten", "Документ": "Dokument",
    "Otwórz документ": "Dokument öffnen", "Open документ": "Dokument öffnen", "Відкрити документ": "Dokument öffnen", "Перевірити дані": "Daten prüfen", "Зберегти в архіві": "Im Archiv speichern",
    "Додати у витрати": "Als Ausgabe buchen", "Додати в доходи": "Als Einnahme buchen", "Додати операцію": "Buchung hinzufügen", "Дохід": "Einnahme", "Витрата": "Ausgabe",
    "Категорія": "Kategorie", "Дохід за перевезення": "Transporterlös", "Ремонт і сервіс": "Reparatur und Service", "Дороги й паркінги": "Maut und Parken", "Лізинг або кредит": "Leasing oder Kredit",
    "Страхування": "Versicherung", "Зарплата": "Lohn", "Податки та збори": "Steuern und Abgaben", "Офісні витрати": "Bürokosten", "Інше": "Sonstiges",
    "Вся компанія": "Gesamtes Unternehmen", "Опис": "Beschreibung", "Контрагент": "Geschäftspartner", "Номер фактури": "Rechnungsnummer", "Термін оплати": "Fälligkeitsdatum",
    "Оплата": "Zahlung", "Не оплачено": "Unbezahlt", "Оплачено": "Bezahlt", "Останні операції": "Letzte Buchungen", "Редагувати": "Bearbeiten", "Оригінальний файл не прикріплений": "Originaldatei nicht angehängt",
    "Фактури та транспортні замовлення з пошти": "Rechnungen und Transportaufträge aus E-Mails",
    "Транспортне замовлення записується як дохід, а вхідна фактура — як витрата. Перед підтвердженням тип документа можна змінити через кнопку «Перевірити».": "Ein Transportauftrag wird als Einnahme und eine Eingangsrechnung als Ausgabe erfasst. Vor der Bestätigung kann der Dokumenttyp über die Schaltfläche „Prüfen“ geändert werden.",
    "Програма перевірятиме дублікати за контрагентом, номером фактури, сумою та валютою.": "Das System prüft Dubletten anhand von Geschäftspartner, Rechnungsnummer, Betrag und Währung.",
    "Нове": "Neu", "Дані перевезення": "Transportdaten", "Номер замовника:": "Kundennummer:", "Маршрут:": "Route:", "Дати:": "Daten:", "Умови оплати:": "Zahlungsbedingungen:",
    "Перевірити": "Prüfen", "Відхилити": "Ablehnen", "Спочатку перевірте суму.": "Prüfen Sie zuerst den Betrag.", "Транспортне замовлення з Gmail": "Transportauftrag aus Gmail", "Фактура з Gmail": "Rechnung aus Gmail",
    "Логотип показується у шапці та великим прозорим фоном на вході.": "Das Logo wird in der Kopfzeile und als großer transparenter Hintergrund auf der Anmeldeseite angezeigt.",
    "Використовується початковий логотип.": "Das Standardlogo wird derzeit verwendet.", "Логотип / Logo": "Logo", "PNG, JPG або WebP, максимум 2 МБ.": "PNG, JPG oder WebP, maximal 2 MB.", "Зберегти / Save": "Speichern",
    "Компанія:": "Unternehmen:", "Автомобілів у системі:": "Fahrzeuge im System:", "Є дані Navirec": "Navirec-Daten verfügbar", "Немає поточного стану": "Kein aktueller Status",
    "Відкрити": "Öffnen", "Автомобіль": "Fahrzeug", "Швидкість": "Geschwindigkeit", "Деталі": "Details", "Дата": "Datum", "Показати історію": "Historie anzeigen",
    "GPS-точок": "GPS-Punkte", "Початок": "Beginn", "Кінець": "Ende", "Відстань": "Entfernung", "Макс. швидкість": "Max. Geschwindigkeit", "Паливо на початку": "Kraftstoff am Anfang", "Паливо в кінці": "Kraftstoff am Ende",
    "Рух": "Fahrt", "Час руху": "Fahrzeit", "Стоянка": "Standzeit", "Холостий хід": "Leerlauf", "Витрата": "Verbrauch", "Останні 100 GPS-точок": "Letzte 100 GPS-Punkte", "Час": "Zeit", "Стан": "Status", "Координати": "Koordinaten",
    "Оплата доріг і віньєти": "Maut und Vignetten", "Розрахувати маршрут, паливо й оплату доріг": "Route, Kraftstoff und Maut berechnen",
    "🇵🇱 Польща": "🇵🇱 Polen", "🇩🇪 Німеччина": "🇩🇪 Deutschland", "🇦🇹 Австрія": "🇦🇹 Österreich", "🇨🇿 Чехія": "🇨🇿 Tschechien", "🇸🇰 Словаччина": "🇸🇰 Slowakei", "🇭🇺 Угорщина": "🇭🇺 Ungarn", "🇸🇮 Словенія": "🇸🇮 Slowenien", "🇨🇭 Швейцарія": "🇨🇭 Schweiz", "🇷🇴 Румунія": "🇷🇴 Rumänien", "🇧🇬 Болгарія": "🇧🇬 Bulgarien", "🇧🇪 Бельгія": "🇧🇪 Belgien", "🇫🇷 🇮🇹 🇪🇸 🇵🇹 Західна Європа": "🇫🇷 🇮🇹 🇪🇸 🇵🇹 Westeuropa",
    "Тут поки показується поточний рівень палива з Navirec. Збільшення рівня ще не вважаємо автоматично заправкою.": "Derzeit wird der aktuelle Kraftstoffstand aus Navirec angezeigt. Ein Anstieg des Füllstands wird noch nicht automatisch als Betankung gewertet.",
    "Вибір тарифу залежить від ваги, кількості осей, висоти, екологічного класу й країни. Купуйте тільки на офіційних сторінках операторів.": "Der Tarif hängt von Gewicht, Achsanzahl, Höhe, Emissionsklasse und Land ab. Kaufen Sie nur über die offiziellen Seiten der Betreiber.",
    "Напрямок": "Fahrtrichtung", "Оберти двигуна": "Motordrehzahl", "об/хв": "U/min", "Загальна відстань": "Gesamtstrecke", "Запалювання": "Zündung", "Увімкнено": "Ein", "Вимкнено": "Aus", "Історія маршруту": "Routenhistorie"
})

# Never replace bare word fragments inside other words.  Units are handled
# with whitespace-aware forms, preventing strings such as "сьоgodz.ні".
for _lang in ("pl", "en", "de"):
    for _unsafe in ("год", "км", "хв", "керування"):
        GLOBAL_UI_TRANSLATIONS.get(_lang, {}).pop(_unsafe, None)


# Route queue button: one canonical Ukrainian source string, translated by selected UI language.
GLOBAL_UI_TRANSLATIONS.setdefault("pl", {}).update({
    "+ Додати як наступний маршрут": "+ Dodaj jako następną trasę",
})
GLOBAL_UI_TRANSLATIONS.setdefault("en", {}).update({
    "+ Додати як наступний маршрут": "+ Add as next route",
})
GLOBAL_UI_TRANSLATIONS.setdefault("de", {}).update({
    "+ Додати як наступний маршрут": "+ Als nächste Route hinzufügen",
})


# Complete localization for multi-company onboarding/settings and GPS provider messages.
GLOBAL_UI_TRANSLATIONS.setdefault('pl', {}).update({'Картка водія': 'Karta kierowcy', 'До наступної перерви': 'Do następnej przerwy', 'До денного відпочинку': 'Do odpoczynku dobowego', 'Картка водія не вставлена в тахограф.': 'Karta kierowcy nie jest włożona do tachografu.', 'Термін дії картки водія закінчився.': 'Karta kierowcy straciła ważność.', 'Нова компанія': 'Nowa firma', 'Створити окремий кабінет у TRANVIQ.': 'Utwórz oddzielny panel firmy w TRANVIQ.', 'Зареєструвати компанію': 'Zarejestruj firmę', 'Вхід компанії': 'Logowanie firmy', 'Для цієї машини пароль ще не встановлено. Директор має встановити його в розділі «🔐 Паролі».': 'Dla tego pojazdu hasło nie zostało jeszcze ustawione. Dyrektor musi je ustawić w sekcji „🔐 Hasła”.', 'Заповніть усі поля.': 'Wypełnij wszystkie pola.', 'Пароль має містити щонайменше 8 символів.': 'Hasło musi mieć co najmniej 8 znaków.', 'Паролі не співпадають.': 'Hasła nie są takie same.', 'Такий логін уже використовується.': 'Ten login jest już używany.', 'Компанія з таким NIP уже зареєстрована.': 'Firma z takim NIP jest już zarejestrowana.', 'Тестовий режим: після перезапуску Render реєстрація може зникнути.': 'Tryb testowy: po ponownym uruchomieniu Render rejestracja może zniknąć.', 'Реєстрація компанії в TRANVIQ': 'Rejestracja firmy w TRANVIQ', 'Назва компанії': 'Nazwa firmy', 'Ім’я директора': 'Imię i nazwisko dyrektora', 'E-mail або логін директора': 'E-mail lub login dyrektora', 'Повторіть пароль': 'Powtórz hasło', 'Створити компанію': 'Utwórz firmę', 'Уже маю акаунт': 'Mam już konto', 'Реєстрація компанії': 'Rejestracja firmy', 'E-mail або логін': 'E-mail lub login', 'Реєстрація': 'Rejestracja', 'TRANVIQ — вхід': 'TRANVIQ — logowanie', 'Оновіть сторінку та повторіть.': 'Odśwież stronę i spróbuj ponownie.', 'Вкажіть ім’я, логін та пароль мінімум 8 символів.': 'Podaj imię, login i hasło o długości co najmniej 8 znaków.', 'Оберіть роль.': 'Wybierz rolę.', 'Призначте автомобіль водію.': 'Przypisz pojazd kierowcy.', 'Доступ створено.': 'Dostęp utworzony.', 'Команда': 'Zespół', 'Ім’я': 'Imię', 'Автомобіль': 'Pojazd', 'Додати користувача': 'Dodaj użytkownika', 'Автомобіль для водія': 'Pojazd dla kierowcy', 'Оберіть': 'Wybierz', 'Створити доступ': 'Utwórz dostęp', 'Команда компанії': 'Zespół firmy', 'Доступ лише для директора.': 'Dostęp tylko dla dyrektora.', 'Вкажіть номер автомобіля.': 'Podaj numer rejestracyjny pojazdu.', 'Вкажіть обидві правильні координати.': 'Podaj obie prawidłowe współrzędne.', 'Автомобіль не знайдено.': 'Nie znaleziono pojazdu.', 'Такий номер уже є у вашому парку.': 'Taki numer rejestracyjny jest już w Twojej flocie.', 'Не підключено': 'Nie podłączono', 'Редагувати': 'Edytuj', 'Номер автомобіля': 'Numer rejestracyjny', 'Назва автомобіля': 'Nazwa pojazdu', 'GPS-ID (заповнюється після імпорту)': 'GPS-ID (uzupełniane po imporcie)', 'Широта (для ручної позиції)': 'Szerokość geograficzna (pozycja ręczna)', 'Довгота (для ручної позиції)': 'Długość geograficzna (pozycja ręczna)', 'Редагувати автомобіль': 'Edytuj pojazd', 'Додати автомобіль': 'Dodaj pojazd', 'Зберегти': 'Zapisz', 'Парк компанії': 'Flota firmy', 'Вибрати машини з GPS': 'Wybierz pojazdy z GPS', 'Номер': 'Numer', 'Назва': 'Nazwa', 'Дії': 'Działania', 'Додайте перший автомобіль.': 'Dodaj pierwszy pojazd.', 'Автомобілі компанії': 'Pojazdy firmy', 'Оберіть автомобілі зі списку.': 'Wybierz pojazdy z listy.', 'Список змінився. Оновіть його.': 'Lista się zmieniła. Odśwież ją.', 'Оберіть постачальника GPS.': 'Wybierz dostawcę GPS.', 'Оберіть регіон.': 'Wybierz region.', 'Підключення збережено. Оберіть машини нижче.': 'Połączenie zapisane. Wybierz pojazdy poniżej.', 'Ручні позиції увімкнено.': 'Pozycje ręczne zostały włączone.', 'Назву GPS збережено. Для цього постачальника ще потрібно додати інтеграцію.': 'Nazwa GPS została zapisana. Dla tego dostawcy trzeba jeszcze dodać integrację.', 'Підключити GPS': 'Podłącz GPS', 'Постачальник GPS': 'Dostawca GPS', 'Назва іншого GPS': 'Nazwa innego GPS', 'ID акаунта (тільки Navirec)': 'ID konta (tylko Navirec)', 'Сервер Wialon (як у вашому кабінеті)': 'Serwer Wialon (jak w Twoim koncie)', 'API-токен (порожнє поле зберігає чинний токен цього постачальника)': 'Token API (puste pole zachowuje aktualny token tego dostawcy)', 'Підключити та отримати список машин': 'Połącz i pobierz listę pojazdów', 'Іншого постачальника підключаємо через його API. Паливо, тахограф та історія залежать від даних, які він надає.': 'Innego dostawcę podłączamy przez jego API. Paliwo, tachograf i historia zależą od danych, które udostępnia.', 'Машини з вашого GPS-акаунта': 'Pojazdy z Twojego konta GPS', 'Додати вибрані автомобілі': 'Dodaj wybrane pojazdy', 'Підключення GPS': 'Połączenie GPS', 'Пароль має містити мінімум 8 символів.': 'Hasło musi mieć minimum 8 znaków.', 'Користувача не знайдено.': 'Nie znaleziono użytkownika.', 'Доступ збережено.': 'Dostęp zapisany.', 'Логін:': 'Login:', 'увімкнено': 'włączony', 'вимкнено': 'wyłączony', 'Новий пароль': 'Nowe hasło', 'Зберегти пароль': 'Zapisz hasło', 'Вимкнути доступ': 'Wyłącz dostęp', 'Паролі та доступи': 'Hasła i dostępy', 'Додати водія або логіста': 'Dodaj kierowcę lub logistyka', 'База компаній тимчасово недоступна. Спробуйте пізніше.': 'Baza firm jest chwilowo niedostępna. Spróbuj ponownie później.', 'Без GPS / ручна позиція': 'Bez GPS / pozycja ręczna', 'Інший GPS (потрібне підключення)': 'Inny GPS (wymaga integracji)', 'Вкажіть токен та ID акаунта Navirec.': 'Podaj token i ID konta Navirec.', 'Navirec не надав дані. Перевірте токен і доступ до акаунта.': 'Navirec nie zwrócił danych. Sprawdź token i dostęp do konta.', 'Акаунт містить більше 500 записів. Потрібне додаткове налаштування імпорту.': 'Konto zawiera ponad 500 rekordów. Wymagana jest dodatkowa konfiguracja importu.', 'Вкажіть API-токен Wialon.': 'Podaj token API Wialon.', 'Wialon тимчасово недоступний.': 'Wialon jest chwilowo niedostępny.', 'Wialon відхилив запит. Перевірте токен і права доступу.': 'Wialon odrzucił żądanie. Sprawdź token i uprawnienia.', 'Не вдалося відкрити сесію Wialon.': 'Nie udało się otworzyć sesji Wialon.', 'Цей постачальник ще не підключений.': 'Ten dostawca nie jest jeszcze podłączony.', 'Не вдалося отримати список автомобілів. Спробуйте ще раз.': 'Nie udało się pobrać listy pojazdów. Spróbuj ponownie.', 'Не вдалося отримати позиції GPS.': 'Nie udało się pobrać pozycji GPS.'})
GLOBAL_UI_TRANSLATIONS.setdefault('en', {}).update({'Нова компанія': 'New company', 'Створити окремий кабінет у TRANVIQ.': 'Create a separate company workspace in TRANVIQ.', 'Зареєструвати компанію': 'Register company', 'Вхід компанії': 'Company sign-in', 'Для цієї машини пароль ще не встановлено. Директор має встановити його в розділі «🔐 Паролі».': 'No password has been set for this vehicle yet. The director must set it in the “🔐 Passwords” section.', 'Заповніть усі поля.': 'Fill in all fields.', 'Пароль має містити щонайменше 8 символів.': 'The password must contain at least 8 characters.', 'Паролі не співпадають.': 'The passwords do not match.', 'Такий логін уже використовується.': 'This login is already in use.', 'Компанія з таким NIP уже зареєстрована.': 'A company with this NIP is already registered.', 'Тестовий режим: після перезапуску Render реєстрація може зникнути.': 'Test mode: registration may disappear after Render restarts.', 'Реєстрація компанії в TRANVIQ': 'Company registration in TRANVIQ', 'Назва компанії': 'Company name', 'Ім’я директора': 'Director name', 'E-mail або логін директора': 'Director email or login', 'Повторіть пароль': 'Repeat password', 'Створити компанію': 'Create company', 'Уже маю акаунт': 'I already have an account', 'Реєстрація компанії': 'Company registration', 'E-mail або логін': 'Email or login', 'Реєстрація': 'Register', 'TRANVIQ — вхід': 'TRANVIQ — sign in', 'Оновіть сторінку та повторіть.': 'Refresh the page and try again.', 'Вкажіть ім’я, логін та пароль мінімум 8 символів.': 'Enter a name, login and a password of at least 8 characters.', 'Оберіть роль.': 'Select a role.', 'Призначте автомобіль водію.': 'Assign a vehicle to the driver.', 'Доступ створено.': 'Access created.', 'Команда': 'Team', 'Ім’я': 'Name', 'Автомобіль': 'Vehicle', 'Додати користувача': 'Add user', 'Автомобіль для водія': 'Driver vehicle', 'Оберіть': 'Select', 'Створити доступ': 'Create access', 'Команда компанії': 'Company team', 'Доступ лише для директора.': 'Director access only.', 'Вкажіть номер автомобіля.': 'Enter the vehicle registration number.', 'Вкажіть обидві правильні координати.': 'Enter both valid coordinates.', 'Автомобіль не знайдено.': 'Vehicle not found.', 'Такий номер уже є у вашому парку.': 'This registration number is already in your fleet.', 'Не підключено': 'Not connected', 'Редагувати': 'Edit', 'Номер автомобіля': 'Registration number', 'Назва автомобіля': 'Vehicle name', 'GPS-ID (заповнюється після імпорту)': 'GPS ID (filled after import)', 'Широта (для ручної позиції)': 'Latitude (manual position)', 'Довгота (для ручної позиції)': 'Longitude (manual position)', 'Редагувати автомобіль': 'Edit vehicle', 'Додати автомобіль': 'Add vehicle', 'Зберегти': 'Save', 'Парк компанії': 'Company fleet', 'Вибрати машини з GPS': 'Choose vehicles from GPS', 'Номер': 'Number', 'Назва': 'Name', 'Дії': 'Actions', 'Додайте перший автомобіль.': 'Add the first vehicle.', 'Автомобілі компанії': 'Company vehicles', 'Оберіть автомобілі зі списку.': 'Select vehicles from the list.', 'Список змінився. Оновіть його.': 'The list has changed. Refresh it.', 'Оберіть постачальника GPS.': 'Select a GPS provider.', 'Оберіть регіон.': 'Select a region.', 'Підключення збережено. Оберіть машини нижче.': 'Connection saved. Select vehicles below.', 'Ручні позиції увімкнено.': 'Manual positions enabled.', 'Назву GPS збережено. Для цього постачальника ще потрібно додати інтеграцію.': 'GPS name saved. An integration still needs to be added for this provider.', 'Підключити GPS': 'Connect GPS', 'Постачальник GPS': 'GPS provider', 'Назва іншого GPS': 'Other GPS name', 'ID акаунта (тільки Navirec)': 'Account ID (Navirec only)', 'Сервер Wialon (як у вашому кабінеті)': 'Wialon server (as in your account)', 'API-токен (порожнє поле зберігає чинний токен цього постачальника)': 'API token (leave blank to keep the current token for this provider)', 'Підключити та отримати список машин': 'Connect and get vehicle list', 'Іншого постачальника підключаємо через його API. Паливо, тахограф та історія залежать від даних, які він надає.': 'Other providers are connected through their API. Fuel, tachograph and history depend on the data they provide.', 'Машини з вашого GPS-акаунта': 'Vehicles from your GPS account', 'Додати вибрані автомобілі': 'Add selected vehicles', 'Підключення GPS': 'GPS connection', 'Пароль має містити мінімум 8 символів.': 'The password must contain at least 8 characters.', 'Користувача не знайдено.': 'User not found.', 'Доступ збережено.': 'Access saved.', 'Логін:': 'Login:', 'увімкнено': 'enabled', 'вимкнено': 'disabled', 'Новий пароль': 'New password', 'Зберегти пароль': 'Save password', 'Вимкнути доступ': 'Disable access', 'Паролі та доступи': 'Passwords and access', 'Додати водія або логіста': 'Add driver or dispatcher', 'База компаній тимчасово недоступна. Спробуйте пізніше.': 'The company database is temporarily unavailable. Try again later.', 'Без GPS / ручна позиція': 'No GPS / manual position', 'Інший GPS (потрібне підключення)': 'Other GPS (integration required)', 'Вкажіть токен та ID акаунта Navirec.': 'Enter the Navirec token and account ID.', 'Navirec не надав дані. Перевірте токен і доступ до акаунта.': 'Navirec returned no data. Check the token and account access.', 'Акаунт містить більше 500 записів. Потрібне додаткове налаштування імпорту.': 'The account contains more than 500 records. Additional import configuration is required.', 'Вкажіть API-токен Wialon.': 'Enter the Wialon API token.', 'Wialon тимчасово недоступний.': 'Wialon is temporarily unavailable.', 'Wialon відхилив запит. Перевірте токен і права доступу.': 'Wialon rejected the request. Check the token and permissions.', 'Не вдалося відкрити сесію Wialon.': 'Could not open a Wialon session.', 'Цей постачальник ще не підключений.': 'This provider is not connected yet.', 'Не вдалося отримати список автомобілів. Спробуйте ще раз.': 'Could not retrieve the vehicle list. Try again.', 'Не вдалося отримати позиції GPS.': 'Could not retrieve GPS positions.'})
GLOBAL_UI_TRANSLATIONS.setdefault('de', {}).update({'Нова компанія': 'Neues Unternehmen', 'Створити окремий кабінет у TRANVIQ.': 'Einen separaten Unternehmensbereich in TRANVIQ erstellen.', 'Зареєструвати компанію': 'Unternehmen registrieren', 'Вхід компанії': 'Unternehmensanmeldung', 'Для цієї машини пароль ще не встановлено. Директор має встановити його в розділі «🔐 Паролі».': 'Für dieses Fahrzeug wurde noch kein Passwort festgelegt. Die Geschäftsleitung muss es im Bereich „🔐 Passwörter“ festlegen.', 'Заповніть усі поля.': 'Füllen Sie alle Felder aus.', 'Пароль має містити щонайменше 8 символів.': 'Das Passwort muss mindestens 8 Zeichen enthalten.', 'Паролі не співпадають.': 'Die Passwörter stimmen nicht überein.', 'Такий логін уже використовується.': 'Dieser Benutzername wird bereits verwendet.', 'Компанія з таким NIP уже зареєстрована.': 'Ein Unternehmen mit dieser NIP ist bereits registriert.', 'Тестовий режим: після перезапуску Render реєстрація може зникнути.': 'Testmodus: Nach einem Neustart von Render kann die Registrierung verloren gehen.', 'Реєстрація компанії в TRANVIQ': 'Unternehmensregistrierung in TRANVIQ', 'Назва компанії': 'Unternehmensname', 'Ім’я директора': 'Name der Geschäftsleitung', 'E-mail або логін директора': 'E-Mail oder Benutzername der Geschäftsleitung', 'Повторіть пароль': 'Passwort wiederholen', 'Створити компанію': 'Unternehmen erstellen', 'Уже маю акаунт': 'Ich habe bereits ein Konto', 'Реєстрація компанії': 'Unternehmensregistrierung', 'E-mail або логін': 'E-Mail oder Benutzername', 'Реєстрація': 'Registrieren', 'TRANVIQ — вхід': 'TRANVIQ — Anmeldung', 'Оновіть сторінку та повторіть.': 'Aktualisieren Sie die Seite und versuchen Sie es erneut.', 'Вкажіть ім’я, логін та пароль мінімум 8 символів.': 'Geben Sie Name, Benutzername und ein Passwort mit mindestens 8 Zeichen ein.', 'Оберіть роль.': 'Wählen Sie eine Rolle.', 'Призначте автомобіль водію.': 'Weisen Sie dem Fahrer ein Fahrzeug zu.', 'Доступ створено.': 'Zugang erstellt.', 'Команда': 'Team', 'Ім’я': 'Name', 'Автомобіль': 'Fahrzeug', 'Додати користувача': 'Benutzer hinzufügen', 'Автомобіль для водія': 'Fahrzeug für den Fahrer', 'Оберіть': 'Auswählen', 'Створити доступ': 'Zugang erstellen', 'Команда компанії': 'Unternehmensteam', 'Доступ лише для директора.': 'Zugriff nur für die Geschäftsleitung.', 'Вкажіть номер автомобіля.': 'Geben Sie das Kennzeichen ein.', 'Вкажіть обидві правильні координати.': 'Geben Sie beide gültigen Koordinaten ein.', 'Автомобіль не знайдено.': 'Fahrzeug nicht gefunden.', 'Такий номер уже є у вашому парку.': 'Dieses Kennzeichen ist bereits in Ihrem Fuhrpark vorhanden.', 'Не підключено': 'Nicht verbunden', 'Редагувати': 'Bearbeiten', 'Номер автомобіля': 'Kennzeichen', 'Назва автомобіля': 'Fahrzeugname', 'GPS-ID (заповнюється після імпорту)': 'GPS-ID (wird nach dem Import ausgefüllt)', 'Широта (для ручної позиції)': 'Breitengrad (manuelle Position)', 'Довгота (для ручної позиції)': 'Längengrad (manuelle Position)', 'Редагувати автомобіль': 'Fahrzeug bearbeiten', 'Додати автомобіль': 'Fahrzeug hinzufügen', 'Зберегти': 'Speichern', 'Парк компанії': 'Fuhrpark', 'Вибрати машини з GPS': 'Fahrzeuge aus GPS auswählen', 'Номер': 'Nummer', 'Назва': 'Name', 'Дії': 'Aktionen', 'Додайте перший автомобіль.': 'Fügen Sie das erste Fahrzeug hinzu.', 'Автомобілі компанії': 'Unternehmensfahrzeuge', 'Оберіть автомобілі зі списку.': 'Wählen Sie Fahrzeuge aus der Liste.', 'Список змінився. Оновіть його.': 'Die Liste hat sich geändert. Aktualisieren Sie sie.', 'Оберіть постачальника GPS.': 'Wählen Sie einen GPS-Anbieter.', 'Оберіть регіон.': 'Wählen Sie eine Region.', 'Підключення збережено. Оберіть машини нижче.': 'Verbindung gespeichert. Wählen Sie unten die Fahrzeuge aus.', 'Ручні позиції увімкнено.': 'Manuelle Positionen aktiviert.', 'Назву GPS збережено. Для цього постачальника ще потрібно додати інтеграцію.': 'Der GPS-Name wurde gespeichert. Für diesen Anbieter muss noch eine Integration hinzugefügt werden.', 'Підключити GPS': 'GPS verbinden', 'Постачальник GPS': 'GPS-Anbieter', 'Назва іншого GPS': 'Name des anderen GPS', 'ID акаунта (тільки Navirec)': 'Konto-ID (nur Navirec)', 'Сервер Wialon (як у вашому кабінеті)': 'Wialon-Server (wie in Ihrem Konto)', 'API-токен (порожнє поле зберігає чинний токен цього постачальника)': 'API-Token (leer lassen, um den aktuellen Token dieses Anbieters zu behalten)', 'Підключити та отримати список машин': 'Verbinden und Fahrzeugliste laden', 'Іншого постачальника підключаємо через його API. Паливо, тахограф та історія залежать від даних, які він надає.': 'Andere Anbieter werden über ihre API angebunden. Kraftstoff, Tachograph und Verlauf hängen von den bereitgestellten Daten ab.', 'Машини з вашого GPS-акаунта': 'Fahrzeuge aus Ihrem GPS-Konto', 'Додати вибрані автомобілі': 'Ausgewählte Fahrzeuge hinzufügen', 'Підключення GPS': 'GPS-Verbindung', 'Пароль має містити мінімум 8 символів.': 'Das Passwort muss mindestens 8 Zeichen enthalten.', 'Користувача не знайдено.': 'Benutzer nicht gefunden.', 'Доступ збережено.': 'Zugang gespeichert.', 'Логін:': 'Benutzername:', 'увімкнено': 'aktiviert', 'вимкнено': 'deaktiviert', 'Новий пароль': 'Neues Passwort', 'Зберегти пароль': 'Passwort speichern', 'Вимкнути доступ': 'Zugang deaktivieren', 'Паролі та доступи': 'Passwörter und Zugänge', 'Додати водія або логіста': 'Fahrer oder Disponenten hinzufügen', 'База компаній тимчасово недоступна. Спробуйте пізніше.': 'Die Unternehmensdatenbank ist vorübergehend nicht verfügbar. Versuchen Sie es später erneut.', 'Без GPS / ручна позиція': 'Ohne GPS / manuelle Position', 'Інший GPS (потрібне підключення)': 'Anderes GPS (Integration erforderlich)', 'Вкажіть токен та ID акаунта Navirec.': 'Geben Sie den Navirec-Token und die Konto-ID ein.', 'Navirec не надав дані. Перевірте токен і доступ до акаунта.': 'Navirec hat keine Daten geliefert. Prüfen Sie Token und Kontozugriff.', 'Акаунт містить більше 500 записів. Потрібне додаткове налаштування імпорту.': 'Das Konto enthält mehr als 500 Einträge. Eine zusätzliche Importkonfiguration ist erforderlich.', 'Вкажіть API-токен Wialon.': 'Geben Sie den Wialon-API-Token ein.', 'Wialon тимчасово недоступний.': 'Wialon ist vorübergehend nicht verfügbar.', 'Wialon відхилив запит. Перевірте токен і права доступу.': 'Wialon hat die Anfrage abgelehnt. Prüfen Sie Token und Berechtigungen.', 'Не вдалося відкрити сесію Wialon.': 'Die Wialon-Sitzung konnte nicht geöffnet werden.', 'Цей постачальник ще не підключений.': 'Dieser Anbieter ist noch nicht verbunden.', 'Не вдалося отримати список автомобілів. Спробуйте ще раз.': 'Die Fahrzeugliste konnte nicht geladen werden. Versuchen Sie es erneut.', 'Не вдалося отримати позиції GPS.': 'GPS-Positionen konnten nicht geladen werden.'})


# Final UI language pass. These values override older fallback entries above.
FINAL_UI_FIXES = {
    "pl": {
        "Пароль": "Hasło",
        "Повторіть пароль": "Powtórz hasło",
        "Команда": "Zespół",
        "Підключення GPS": "Połączenie GPS",
        "Компанія:": "Firma:",
        "Автомобілів:": "Pojazdy:",
        "Збереження:": "Przechowywanie:",
        "постійне": "trwałe",
        "тимчасове": "tymczasowe",
        "Додайте автомобілі, щоб відкрити історію.": "Dodaj pojazd, aby otworzyć historię.",
        "Не вдалося отримати стани водіїв:": "Nie udało się pobrać stanów kierowców:",
        "Не вдалося отримати список водіїв:": "Nie udało się pobrać listy kierowców:",
        "Не вдалося отримати картки тахографа:": "Nie udało się pobrać kart tachografu:",
        "NAVIREC_TOKEN не налаштований.": "NAVIREC_TOKEN nie jest skonfigurowany.",
        "Оберіть регіон Wialon.": "Wybierz region Wialon.",
        "Кожна точка з нового рядка: адреса | 08:00 | 10:00": "Każdy punkt w osobnym wierszu: adres | 08:00 | 10:00",
        "Для цього автомобіля активного розвізного маршруту немає.": "Dla tego pojazdu nie ma aktywnej trasy dostaw.",
        "Не вдалося відкрити фактуру:": "Nie udało się otworzyć faktury:",
        "Помилка Gmail:": "Błąd Gmail:",
        "Операцію збережено.": "Operację zapisano.",
        "Документ підтверджено та додано у фінансовий облік.": "Dokument zatwierdzono i dodano do ewidencji finansowej.",
        "Фактуру відхилено. У фінанси її не додано.": "Fakturę odrzucono. Nie dodano jej do finansów.",
        "Перевірені дані фактури збережено.": "Zweryfikowane dane faktury zapisano.",
        "Фінансову операцію оновлено. Результат перераховано.": "Operację finansową zaktualizowano. Wynik przeliczono.",
        "Gmail успішно підключено. Тепер можна завантажити фактури.": "Gmail został podłączony. Można teraz pobierać faktury.",
        "Gmail відключено від програми.": "Gmail został odłączony od programu.",
        "Спочатку потрібно додати в Render ключі Google Gmail API.": "Najpierw dodaj w Render klucze Google Gmail API.",
        "Налаштування бухгалтерії збережено.": "Ustawienia księgowości zapisano.",
        "Бухгалтерію відключено. Раніше отримані документи збережені.": "Księgowość odłączono. Wcześniej odebrane dokumenty zostały zachowane.",
        "Документ збережено в бухгалтерському архіві.": "Dokument zapisano w archiwum księgowym.",
        "Не вдалося зберегти бухгалтерію:": "Nie udało się zapisać ustawień księgowości:",
        "Основна пошта": "Główna skrzynka",
        "Не визначено": "Nie określono",
        "Ще не перевірялося": "Jeszcze nie sprawdzano",
        "Підтверджено": "Zatwierdzono",
        "В архіві": "W archiwum",
        "Відхилено": "Odrzucono",
        "Дублікат": "Duplikat",
        "Контрагент:": "Kontrahent:",
        "Документ №": "Dokument nr",
        "Номер замовника:": "Numer klienta:",
        "Маршрут:": "Trasa:",
        "Дати:": "Daty:",
        "Автомобіль:": "Pojazd:",
        "Оплата:": "Płatność:",
        "Відправник:": "Nadawca:",
        "Файл:": "Plik:",
    },
    "en": {
        "Пароль": "Password",
        "Повторіть пароль": "Repeat password",
        "Команда": "Team",
        "Підключення GPS": "GPS connection",
        "Компанія:": "Company:",
        "Автомобілів:": "Vehicles:",
        "Збереження:": "Storage:",
        "постійне": "persistent",
        "тимчасове": "temporary",
        "Додайте автомобілі, щоб відкрити історію.": "Add a vehicle to open history.",
        "Не вдалося отримати стани водіїв:": "Could not retrieve driver states:",
        "Не вдалося отримати список водіїв:": "Could not retrieve the driver list:",
        "Не вдалося отримати картки тахографа:": "Could not retrieve tachograph cards:",
        "NAVIREC_TOKEN не налаштований.": "NAVIREC_TOKEN is not configured.",
        "Оберіть регіон Wialon.": "Select the Wialon region.",
        "Кожна точка з нового рядка: адреса | 08:00 | 10:00": "One point per line: address | 08:00 | 10:00",
        "Для цього автомобіля активного розвізного маршруту немає.": "This vehicle has no active delivery route.",
        "Не вдалося відкрити фактуру:": "Could not open the invoice:",
        "Помилка Gmail:": "Gmail error:",
        "Операцію збережено.": "Transaction saved.",
        "Документ підтверджено та додано у фінансовий облік.": "Document approved and added to financial records.",
        "Фактуру відхилено. У фінанси її не додано.": "Invoice rejected. It was not added to finances.",
        "Перевірені дані фактури збережено.": "Verified invoice data saved.",
        "Фінансову операцію оновлено. Результат перераховано.": "Financial transaction updated. Result recalculated.",
        "Gmail успішно підключено. Тепер можна завантажити фактури.": "Gmail connected successfully. Invoices can now be imported.",
        "Gmail відключено від програми.": "Gmail disconnected from the app.",
        "Спочатку потрібно додати в Render ключі Google Gmail API.": "First add the Google Gmail API keys in Render.",
        "Налаштування бухгалтерії збережено.": "Accounting settings saved.",
        "Бухгалтерію відключено. Раніше отримані документи збережені.": "Accounting was disconnected. Previously received documents were kept.",
        "Документ збережено в бухгалтерському архіві.": "Document saved to the accounting archive.",
        "Не вдалося зберегти бухгалтерію:": "Could not save accounting settings:",
        "Основна пошта": "Primary mailbox",
        "Не визначено": "Not specified",
        "Ще не перевірялося": "Not checked yet",
        "Підтверджено": "Approved",
        "В архіві": "Archived",
        "Відхилено": "Rejected",
        "Дублікат": "Duplicate",
        "Контрагент:": "Counterparty:",
        "Документ №": "Document no.",
        "Номер замовника:": "Customer number:",
        "Маршрут:": "Route:",
        "Дати:": "Dates:",
        "Автомобіль:": "Vehicle:",
        "Оплата:": "Payment:",
        "Відправник:": "Sender:",
        "Файл:": "File:",
    },
    "de": {
        "Пароль": "Passwort",
        "Повторіть пароль": "Passwort wiederholen",
        "Команда": "Team",
        "Підключення GPS": "GPS-Verbindung",
        "Компанія:": "Unternehmen:",
        "Автомобілів:": "Fahrzeuge:",
        "Збереження:": "Speicherung:",
        "постійне": "dauerhaft",
        "тимчасове": "temporär",
        "Додайте автомобілі, щоб відкрити історію.": "Fügen Sie ein Fahrzeug hinzu, um den Verlauf zu öffnen.",
        "Не вдалося отримати стани водіїв:": "Fahrerstatus konnten nicht abgerufen werden:",
        "Не вдалося отримати список водіїв:": "Fahrerliste konnte nicht abgerufen werden:",
        "Не вдалося отримати картки тахографа:": "Tachographenkarten konnten nicht abgerufen werden:",
        "NAVIREC_TOKEN не налаштований.": "NAVIREC_TOKEN ist nicht konfiguriert.",
        "Оберіть регіон Wialon.": "Wählen Sie die Wialon-Region.",
        "Кожна точка з нового рядка: адреса | 08:00 | 10:00": "Ein Punkt pro Zeile: Adresse | 08:00 | 10:00",
        "Для цього автомобіля активного розвізного маршруту немає.": "Für dieses Fahrzeug gibt es keine aktive Liefertour.",
        "Не вдалося відкрити фактуру:": "Rechnung konnte nicht geöffnet werden:",
        "Помилка Gmail:": "Gmail-Fehler:",
        "Операцію збережено.": "Buchung gespeichert.",
        "Документ підтверджено та додано у фінансовий облік.": "Dokument bestätigt und zur Finanzbuchhaltung hinzugefügt.",
        "Фактуру відхилено. У фінанси її не додано.": "Rechnung abgelehnt. Sie wurde nicht in die Finanzen übernommen.",
        "Перевірені дані фактури збережено.": "Geprüfte Rechnungsdaten gespeichert.",
        "Фінансову операцію оновлено. Результат перераховано.": "Finanzbuchung aktualisiert. Ergebnis neu berechnet.",
        "Gmail успішно підключено. Тепер можна завантажити фактури.": "Gmail erfolgreich verbunden. Rechnungen können jetzt importiert werden.",
        "Gmail відключено від програми.": "Gmail wurde von der App getrennt.",
        "Спочатку потрібно додати в Render ключі Google Gmail API.": "Fügen Sie zuerst die Google-Gmail-API-Schlüssel in Render hinzu.",
        "Налаштування бухгалтерії збережено.": "Buchhaltungseinstellungen gespeichert.",
        "Бухгалтерію відключено. Раніше отримані документи збережені.": "Die Buchhaltung wurde getrennt. Bereits empfangene Dokumente bleiben erhalten.",
        "Документ збережено в бухгалтерському архіві.": "Dokument im Buchhaltungsarchiv gespeichert.",
        "Не вдалося зберегти бухгалтерію:": "Buchhaltungseinstellungen konnten nicht gespeichert werden:",
        "Основна пошта": "Hauptpostfach",
        "Не визначено": "Nicht angegeben",
        "Ще не перевірялося": "Noch nicht geprüft",
        "Підтверджено": "Bestätigt",
        "В архіві": "Archiviert",
        "Відхилено": "Abgelehnt",
        "Дублікат": "Duplikat",
        "Контрагент:": "Geschäftspartner:",
        "Документ №": "Dokument Nr.",
        "Номер замовника:": "Kundennummer:",
        "Маршрут:": "Route:",
        "Дати:": "Daten:",
        "Автомобіль:": "Fahrzeug:",
        "Оплата:": "Zahlung:",
        "Відправник:": "Absender:",
        "Файл:": "Datei:",
    },
}
for _lang, _items in FINAL_UI_FIXES.items():
    GLOBAL_UI_TRANSLATIONS.setdefault(_lang, {}).update(_items)


def translate_full_app_body(language, body, preserve_scripts=False):
    if language == "uk":
        return body
    mapping = GLOBAL_UI_TRANSLATIONS.get(language)
    if not mapping:
        return body

    def apply_mapping(fragment):
        for source, target in sorted(mapping.items(), key=lambda item: len(item[0]), reverse=True):
            fragment = fragment.replace(source, target)
        return fragment

    if not preserve_scripts:
        return apply_mapping(body)

    # GPS JavaScript is shared by every language. Never translate source code
    # inside <script> blocks; only translate the visible HTML around it.
    parts = re.split(r'(<script\b[^>]*>.*?</script>)', body, flags=re.IGNORECASE | re.DOTALL)
    return ''.join(part if re.match(r'<script\b', part, flags=re.IGNORECASE) else apply_mapping(part) for part in parts)


def replace_visible_gps_text(body, source, target):
    """Replace GPS UI text without ever modifying JavaScript source."""
    parts = re.split(r'(<script\b[^>]*>.*?</script>)', body, flags=re.IGNORECASE | re.DOTALL)
    return ''.join(part if re.match(r'<script\b', part, flags=re.IGNORECASE) else part.replace(source, target) for part in parts)

def page(title, body, active=""):
    role = current_role()
    language = current_language()
    visible_title = translate_full_app_body(language, translate_title(language, title))
    body = translate_full_app_body(language, body, preserve_scripts=(active in {"gps", "driver", "documents"}))

    # Targeted cleanup for Finance only; transport/GPS logic is untouched.
    if active == "finance" and language == "en":
        finance_en_cleanup = {
            "Усі підключені пошти автоматично перевіряються кожні 15 minилин.": "All connected mailboxes are automatically checked every 15 minutes.",
            "Усі підключені пошти автоматично перевіряються кожні 15 хвилин.": "All connected mailboxes are automatically checked every 15 minutes.",
            "Вкажіть назву бухгалтерії та адресу або домен, з якого вона надсилає документи. Можна підключити декілька бухгалтерій.": "Enter the accounting office name and the address or domain it uses to send documents. Multiple accounting offices can be connected.",
            "Taxes, ZUS, зарплати, розрахунки водіїв та кадрові документи зберігаються окремо від фактур і транспортних замовлень.": "Taxes, ZUS, payroll, driver settlements and HR documents are stored separately from invoices and transport orders.",
            "Податки, ZUS, зарплати, розрахунки водіїв та кадрові документи зберігаються окремо від фактур і транспортних замовлень.": "Taxes, ZUS, payroll, driver settlements and HR documents are stored separately from invoices and transport orders.",
            "Open документ": "Open document",
            "Otwórz документ": "Open document",
        }
        for source, target in finance_en_cleanup.items():
            body = body.replace(source, target)

    # Localized custom file picker for Branding; upload behavior stays unchanged.
    if active in {"branding", "documents"}:
        file_picker_text = {
            "uk": ("Вибрати файл", "Файл не вибрано"),
            "pl": ("Wybierz plik", "Nie wybrano pliku"),
            "en": ("Choose file", "No file selected"),
            "de": ("Datei auswählen", "Keine Datei ausgewählt"),
        }.get(language, ("Choose file", "No file selected"))
        choose_label, empty_label = file_picker_text
        body += f'''
<style>
.tranviq-file-picker {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin:6px 0; }}
.tranviq-file-picker button {{ cursor:pointer; }}
.tranviq-file-picker-name {{ opacity:.82; }}


</style>
<script>
document.addEventListener('DOMContentLoaded', function () {{
  document.querySelectorAll('input[type="file"]').forEach(function (input) {{
    if (input.dataset.tranviqLocalized === '1') return;
    input.dataset.tranviqLocalized = '1';
    input.style.display = 'none';
    const box = document.createElement('div');
    box.className = 'tranviq-file-picker';
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = {json.dumps(choose_label, ensure_ascii=False)};
    const name = document.createElement('span');
    name.className = 'tranviq-file-picker-name';
    name.textContent = {json.dumps(empty_label, ensure_ascii=False)};
    button.addEventListener('click', function () {{ input.click(); }});
    input.addEventListener('change', function () {{
      name.textContent = input.files && input.files.length ? input.files[0].name : {json.dumps(empty_label, ensure_ascii=False)};
    }});
    box.appendChild(button);
    box.appendChild(name);
    input.insertAdjacentElement('afterend', box);
  }});
}});
</script>
'''

    page_class = "page-gps" if active == "gps" else ""
    tenant_company = _tenant_company()
    branding = get_company_branding(COMPANY_ID, COMPANY_NAME)
    if tenant_company or active == "tenant_public":
        company_display_name = escape(str(branding["company_name"] if tenant_company else "TRANVIQ"))
        company_logo_html = ('<img class="company-logo" src="/assets/company-logo.jpg" alt="'+company_display_name+'">') if tenant_company and branding.get("has_custom_logo") else ""
        company_watermark_html = ""
    else:
        company_display_name = escape(branding["company_name"])
        company_logo_html = '<img class="company-logo" src="/assets/company-logo.jpg" alt="'+company_display_name+'">'
        company_watermark_html = '<div class="page-watermark" aria-hidden="true"><img src="/assets/company-logo.jpg" alt=""></div>' 

    road_payments_label = {
        "uk": "🛣️ Оплата доріг",
        "pl": "🛣️ Opłaty drogowe",
        "en": "🛣️ Road tolls",
        "de": "🛣️ Maut",
    }.get(language, "🛣️ Оплата доріг")

    if role == "driver":
        driver_gps_label = {
            "uk": "📍 GPS машин",
            "pl": "📍 GPS pojazdów",
            "en": "📍 Vehicle GPS",
            "de": "📍 Fahrzeug-GPS",
        }.get(language, "📍 GPS pojazdów")
        nav_items = [
            ("driver", "/driver", t("my_trips")),
            ("documents", "/documents", "📄 " + {"uk":"Документи","pl":"Dokumenty","en":"Documents","de":"Dokumente"}.get(current_language(),"Документи")),
            ("driver_gps", "/driver#gps", driver_gps_label),
            (
                "road_payments",
                "/road-payments",
                road_payments_label
            )
        ]
    elif role == "dispatcher":
        nav_items = [
            ("gps", "/gps", t("gps")),
            ("documents", "/documents", "📄 " + {"uk":"Документи","pl":"Dokumenty","en":"Documents","de":"Dokumente"}.get(current_language(),"Документи")),
            ("tachograph", "/tachograph", t("tachograph")),
            (
                "road_payments",
                "/road-payments",
                road_payments_label
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
            ("documents", "/documents", "📄 " + {"uk":"Документи","pl":"Dokumenty","en":"Documents","de":"Dokumente"}.get(current_language(),"Документи")),
            (
                "road_payments",
                "/road-payments",
                road_payments_label
            ),
            ("branding", "/settings/branding", t("branding")),
            ("driver_settings", "/driver-settings", "🚐 " + {"uk":"Водії","pl":"Kierowcy","en":"Drivers","de":"Fahrer"}.get(current_language(),"Водії")),
            ("driver_access", "/driver-access", "🔐 " + {
                "uk": "Паролі",
                "pl": "Hasła",
                "en": "Passwords",
                "de": "Passwörter"
            }.get(current_language(), "Паролі")),
            ("health", "/health", {
                "uk": "Стан системи",
                "pl": "Stan systemu",
                "en": "System status",
                "de": "Systemstatus"
            }.get(current_language(), "Стан системи"))
        ]
    else:
        nav_items = []

    if tenant_company and role == "director":
        tenant_nav_labels = {
            "uk": ("Команда", "Підключення GPS"),
            "pl": ("Zespół", "Połączenie GPS"),
            "en": ("Team", "GPS connection"),
            "de": ("Team", "GPS-Verbindung"),
        }.get(language, ("Команда", "Підключення GPS"))
        nav_items.extend([
            ("company_users", "/company/users", tenant_nav_labels[0]),
            ("company_gps_settings", "/company/gps/settings", tenant_nav_labels[1]),
        ])
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

    if role in ("director", "dispatcher"):
        nav_links.append("""<script>async function documentAlerts(){try{const r=await fetch('/api/documents/unread',{cache:'no-store'});if(!r.ok)return;const d=await r.json(),a=document.querySelector('a[href="/documents"]');if(a){const labels={uk:'Новий документ',pl:'Nowy dokument',en:'New document',de:'Neues Dokument'};a.textContent=a.textContent.split(' · ')[0]+(d.count?' · '+(labels[document.documentElement.lang||'uk']||labels.uk)+' ('+d.count+')':'');}}catch(e){}}documentAlerts();setInterval(documentAlerts,30000);if(location.pathname==='/documents'){fetch('/api/documents/seen',{method:'POST'});}</script>""")
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
    visible_languages = ("uk", "pl", "en", "de")
    for language_code in visible_languages:
        if language_code not in LANGUAGES:
            continue
        language_name = LANGUAGES[language_code]
        selected = " selected" if language_code == language else ""
        language_options.append(
            '<option value="{}"{}>{}</option>'.format(
                language_code,
                selected,
                escape(language_name)
            )
        )

    extra_head = ""
    if active == "driver":
        extra_head = """
<link rel="manifest" href="/driver-manifest.webmanifest">
<meta name="theme-color" content="#0b1724">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="TRANVIQ Driver">
<link rel="apple-touch-icon" href="/assets/tranviq-driver-icon.svg">
<script>
if ('serviceWorker' in navigator) {
  window.addEventListener('load', function () {
    navigator.serviceWorker.register('/driver-service-worker.js', {scope: '/'})
      .catch(function () {});
  });
}
</script>
"""

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

.vehicle-marker-icon {{
    background: transparent;
    border: 0;
}}

.vehicle-marker-pin {{
    position: relative;
    width: 28px;
    height: 28px;
    border: 3px solid #ffffff;
    border-radius: 50% 50% 50% 0;
    box-shadow: 0 3px 8px rgba(0, 0, 0, .38);
    transform: rotate(-45deg);
}}

.vehicle-marker-pin::after {{
    content: '';
    position: absolute;
    top: 7px;
    left: 7px;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: rgba(255, 255, 255, .9);
}}

.vehicle-marker-moving {{ background: #149447; }}
.vehicle-marker-idling {{ background: #f2ad16; }}
.vehicle-marker-stopped {{ background: #d63b32; }}

.vehicle-number-label {{
    padding: 4px 7px !important;
    border: 1px solid rgba(16, 42, 59, .24) !important;
    border-radius: 6px !important;
    background: rgba(255, 255, 255, .96) !important;
    color: #102a3b !important;
    box-shadow: 0 2px 7px rgba(0, 0, 0, .18) !important;
    font-size: 12px !important;
    font-weight: 900 !important;
    white-space: nowrap;
}}

.vehicle-number-label::before {{
    border-top-color: rgba(255, 255, 255, .96) !important;
}}

.delivery-stop-icon {{
    background: transparent;
    border: 0;
}}

.delivery-stop-pin {{
    display: flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    border: 3px solid #ffffff;
    border-radius: 50%;
    box-shadow: 0 3px 9px rgba(0, 0, 0, .34);
    color: #ffffff;
    font-size: 13px;
    font-weight: 900;
}}

.delivery-stop-pending {{ background: #d63b32; }}
.delivery-stop-current {{ background: #f2ad16; color: #17202a; }}
.delivery-stop-refused {{ background: #f2ad16; color: #17202a; box-shadow: 0 0 0 3px rgba(242,173,22,.24), 0 3px 8px rgba(0,0,0,.30); }}
.delivery-stop-completed {{ background: #149447; }}

.gps-status-legend {{
    position: absolute;
    z-index: 750;
    top: 12px;
    right: 12px;
    display: flex;
    flex-wrap: wrap;
    gap: 7px 11px;
    padding: 8px 10px;
    border-radius: 9px;
    background: rgba(255, 255, 255, .94);
    box-shadow: 0 3px 12px rgba(15, 37, 51, .18);
    color: #21313c;
    font-size: 11px;
    font-weight: 800;
}}

.gps-status-legend span {{
    display: inline-flex;
    align-items: center;
    gap: 5px;
}}

.gps-status-dot {{
    width: 10px;
    height: 10px;
    border-radius: 50%;
}}

.gps-status-dot.moving {{ background: #149447; }}
.gps-status-dot.idling {{ background: #f2ad16; }}
.gps-status-dot.stopped {{ background: #d63b32; }}

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

/* Keep the save-next-route action reachable in a long planner. */
#queue-delivery-route-button {{
    position: sticky;
    bottom: 8px;
    z-index: 25;
    min-height: 44px;
}}

#queue-delivery-route-button:disabled {{
    position: static;
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

.gps-address-row.city-only {{
    grid-template-columns: minmax(0, 1fr);
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

.gps-delivery-planner {{
    margin-top: 12px;
    padding: 10px;
    border: 1px solid #b8d4dc;
    border-radius: 9px;
    background: #eef8fa;
}}

.gps-delivery-planner-title {{
    margin-bottom: 7px;
    color: #123b50;
    font-size: 13px;
    font-weight: 900;
}}

.gps-delivery-planner textarea {{
    width: 100%;
    min-height: 108px;
    margin: 0 0 9px;
    padding: 9px;
    border: 1px solid #b8c8d1;
    border-radius: 7px;
    resize: vertical;
    font: inherit;
    font-size: 12px;
    line-height: 1.35;
}}

.gps-delivery-settings {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 7px;
}}

.gps-delivery-planner button {{
    width: 100%;
    margin-top: 2px;
}}

.gps-privacy-consent {{
    display: flex !important;
    align-items: flex-start;
    gap: 7px;
    margin: 2px 0 8px !important;
    padding: 8px;
    border-radius: 7px;
    background: #ffffff;
    font-weight: 700 !important;
    line-height: 1.3;
}}

.gps-privacy-consent input {{
    width: auto !important;
    margin: 2px 0 0 !important;
}}

.gps-delivery-privacy {{
    margin-top: 7px;
    color: #4d6875;
    font-size: 11px;
    line-height: 1.35;
}}

.route-feasibility {{
    margin-top: 10px;
    padding: 10px;
    border-radius: 8px;
    background: #edf1f3;
}}

.route-feasibility.ok {{
    background: #e7f6ed;
    color: #17652c;
}}

.route-feasibility.warning {{
    background: #fff4db;
    color: #744d00;
}}

.route-feasibility.error {{
    background: #fdebea;
    color: #8d1717;
}}

.route-stop-list {{
    margin: 9px 0 0;
    padding-left: 20px;
}}

.route-stop-list li {{
    margin: 5px 0;
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

    /* GPS planner: its visible height is calculated in JS BELOW the header. */
    body.page-gps {{
        overflow: hidden;
    }}

    .gps-screen {{
        min-height: 0;
    }}

    .gps-screen #map {{
        min-height: 0;
    }}

    .gps-map-toolbar {{
        top: 10px;
        bottom: auto;
        left: 48px;
        width: calc(100vw - 60px);
        padding: 10px 10px 88px;
        overflow-y: auto;
        overflow-x: hidden;
        -webkit-overflow-scrolling: touch;
        overscroll-behavior: contain;
        touch-action: pan-y;
    }}

    .gps-toolbar-content {{
        padding-bottom: 20px;
    }}

    /* Keep the important "next route" action reachable on a phone while
       scrolling through a long calculated route. */
    #queue-delivery-route-button {{
        position: sticky;
        bottom: 8px;
        z-index: 25;
        min-height: 46px;
        background: #17652c;
        color: #ffffff;
        border: 1px solid #0f4f21;
        box-shadow: 0 4px 14px rgba(0, 0, 0, .22);
    }}

    #queue-delivery-route-button:disabled {{
        position: static;
        box-shadow: none;
    }}

    .gps-map-toolbar.collapsed {{
        bottom: auto;
        width: min(280px, calc(100vw - 60px));
        max-height: none;
        padding: 6px;
        overflow: hidden;
    }}

    .gps-map-brand {{
        right: 8px;
        bottom: 21px;
    }}

    .gps-status-legend {{
        top: auto;
        right: auto;
        bottom: 42px;
        left: 8px;
        gap: 5px 8px;
        padding: 6px 8px;
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
            {company_logo_html}
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

{company_watermark_html}

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
        company_logo_html=company_logo_html,
        company_watermark_html=company_watermark_html,
        company_label=escape(t("company")),
        platform=PLATFORM_NAME,
        platform_tagline=PLATFORM_TAGLINE,
        language_picker=language_picker,
        nav=nav,
        role_badge=role_badge,
        body=body,
        extra_head=extra_head
    )


@app.route("/driver-manifest.webmanifest")
def driver_manifest():
    manifest = {
        "name": "TRANVIQ Driver",
        "short_name": "TRANVIQ",
        "description": "TRANVIQ driver application",
        "start_url": "/driver",
        "scope": "/",
        "display": "standalone",
        "background_color": "#f1f3f5",
        "theme_color": "#0b1724",
        "orientation": "portrait-primary",
        "icons": [
            {
                "src": "/assets/tranviq-driver-icon.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any maskable"
            }
        ],
        "shortcuts": [
            {"name": "Route", "url": "/driver"},
            {"name": "GPS", "url": "/driver#gps"},
            {"name": "Documents", "url": "/documents"}
        ]
    }
    response = Response(
        json.dumps(manifest, ensure_ascii=False),
        mimetype="application/manifest+json"
    )
    response.headers["Cache-Control"] = "public, max-age=300"
    return response


@app.route("/driver-service-worker.js")
def driver_service_worker():
    script = r"""
const CACHE_NAME = 'tranviq-driver-shell-v1';
const STATIC_FILES = [
  '/assets/tranviq-driver-icon.svg',
  '/driver-manifest.webmanifest'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(STATIC_FILES))
      .catch(() => null)
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;

  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;

  // Driver data, routes, messages, documents and GPS always come from the
  // network. We intentionally do not cache authenticated company data.
  event.respondWith(
    fetch(event.request).catch(async () => {
      const cached = await caches.match(event.request);
      if (cached) return cached;
      if (event.request.mode === 'navigate') {
        return new Response(
          'TRANVIQ Driver requires an internet connection.',
          {status: 503, headers: {'Content-Type': 'text/plain; charset=utf-8'}}
        );
      }
      return Response.error();
    })
  );
});
"""
    response = Response(script, mimetype="application/javascript")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.route("/assets/tranviq-driver-icon.svg")
def driver_app_icon():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="112" fill="#0b1724"/>
<path d="M92 143h328v74H294v208h-76V217H92z" fill="#fff"/>
<rect x="302" y="256" width="118" height="118" rx="24" fill="#63e6be"/>
<text x="361" y="334" text-anchor="middle" font-family="Arial,sans-serif" font-size="64" font-weight="900" fill="#07131f">IQ</text>
</svg>"""
    response = Response(svg, mimetype="image/svg+xml")
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


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

    if tenancy.company(): return Response(status=404)
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


@app.route("/callback", methods=["GET"])
def trans_eu_callback():
    """Public OAuth redirect URI registered for O&O TRANS Load Finder."""
    error = (request.args.get("error") or "").strip()
    error_description = (request.args.get("error_description") or "").strip()
    code = (request.args.get("code") or "").strip()

    if error:
        detail = escape(error_description or error)
        return (
            """<!doctype html>
<html lang="pl">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trans.eu — O&O TRANS</title></head>
<body style="font-family:Arial,sans-serif;max-width:720px;margin:48px auto;padding:0 18px">
<h2>Trans.eu — błąd autoryzacji</h2>
<p>Trans.eu zwrócił błąd: <strong>{}</strong></p>
<p>Możesz zamknąć tę stronę i wrócić do aplikacji.</p>
</body></html>""".format(detail),
            400,
        )

    if code:
        return """<!doctype html>
<html lang="pl">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trans.eu — O&O TRANS</title></head>
<body style="font-family:Arial,sans-serif;max-width:720px;margin:48px auto;padding:0 18px">
<h2>Trans.eu — przekierowanie odebrane</h2>
<p>Adres callback działa poprawnie i aplikacja odebrała odpowiedź autoryzacyjną.</p>
<p>Wymiana kodu na token zostanie podłączona po otrzymaniu danych dostępowych API od Trans.eu.</p>
</body></html>"""

    return """<!doctype html>
<html lang="pl">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trans.eu callback — O&O TRANS</title></head>
<body style="font-family:Arial,sans-serif;max-width:720px;margin:48px auto;padding:0 18px">
<h2>O&O TRANS Load Finder</h2>
<p>Callback Trans.eu jest aktywny.</p>
<p>Ten adres służy do przekierowania po autoryzacji Trans.eu.</p>
</body></html>"""


# Preserve legacy defaults; resolve tenant values from request-local context.
_LEGACY_VEHICLES = VEHICLES
_LEGACY_COMPANY_ID = COMPANY_ID
_LEGACY_COMPANY_NAME = COMPANY_NAME
_LEGACY_NAVIREC_TOKEN = NAVIREC_TOKEN
_LEGACY_NAVIREC_ACCOUNT_ID = NAVIREC_ACCOUNT_ID
VEHICLES = LocalProxy(lambda: tenancy.company().get("vehicles", []) if tenancy.company() else _LEGACY_VEHICLES)
COMPANY_ID = LocalProxy(lambda: tenancy.company()["id"] if tenancy.company() else _LEGACY_COMPANY_ID)
COMPANY_NAME = LocalProxy(lambda: tenancy.company()["name"] if tenancy.company() else _LEGACY_COMPANY_NAME)
NAVIREC_TOKEN = LocalProxy(lambda: tenancy.company().get("gps", {}).get("token", "") if tenancy.company() else _LEGACY_NAVIREC_TOKEN)
NAVIREC_ACCOUNT_ID = LocalProxy(lambda: tenancy.company().get("gps", {}).get("account_id", "") if tenancy.company() else _LEGACY_NAVIREC_ACCOUNT_ID)
_NAVIREC_LIST_CACHE = LocalProxy(lambda: tenancy.cache("navirec_list"))
VEHICLE_CONSUMPTION_CACHE = LocalProxy(lambda: tenancy.cache("consumption"))
GEOCODE_CACHE = LocalProxy(lambda: tenancy.cache("geocode"))
ROUTE_CACHE = LocalProxy(lambda: tenancy.cache("routes"))
app.config.update(TENANT_STORE_LOCK=TENANT_ACCOUNTS_LOCK, TENANT_STORE_READ=_load_tenant_accounts, TENANT_STORE_WRITE=_write_tenant_accounts)

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


@app.route("/login/driver", methods=["GET", "POST"])
def driver_login_compat():
    return login("driver")


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
            <div class="card" style="border:2px solid #25a86b">
                <h2>Нова компанія</h2><p>Створити окремий кабінет у TRANVIQ.</p>
                <a class="button" href="/company/register">Зареєструвати компанію</a>
                <a class="button" href="/company/login">Вхід компанії</a>
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

        # Login fields often arrive with a trailing space from phone/autofill.
        # Normalize only the dispatcher login; do not change driver/director logic.
        if role == "dispatcher":
            username = username.strip()
            password = password.strip()

        credentials_configured = bool(expected_user and expected_password)
        credentials_valid = False
        assigned_vehicle_id = ""

        if role == "driver":
            entered_login = normalize_driver_login(username)
            access = _load_driver_access()
            credentials_configured = False

            # Strict login: vehicle plate -> this vehicle's saved password hash.
            # No DXF fallback can override or confuse the director-managed password.
            for vehicle in VEHICLES:
                plate_login = normalize_driver_login(vehicle.get("plate", ""))
                item = access.get(vehicle["id"], {})
                password_hash = str(item.get("password_hash") or "")
                enabled = bool(item.get("enabled"))

                if enabled and password_hash:
                    credentials_configured = True

                if not hmac.compare_digest(entered_login, plate_login):
                    continue

                if enabled and password_hash and check_password_hash(password_hash, password):
                    credentials_valid = True
                    assigned_vehicle_id = vehicle["id"]
                break

        else:
            if role == "dispatcher":
                credentials_valid = (
                    credentials_configured
                    and hmac.compare_digest(
                        username.casefold(),
                        expected_user.casefold()
                    )
                    and hmac.compare_digest(password, expected_password)
                )
            else:
                credentials_valid = (
                    credentials_configured
                    and hmac.compare_digest(username, expected_user)
                    and hmac.compare_digest(password, expected_password)
                )

        if credentials_valid:
            clear_session_keep_language()
            session["logged_in"] = True
            session["role"] = role
            session["username"] = username
            if role == "driver":
                session["driver_vehicle_id"] = assigned_vehicle_id
                session.permanent = True
            return redirect(role_home_url(role))

        if not credentials_configured:
            # Never expose secret values. For the driver login, show which
            # Render Environment key is missing so configuration is easy to fix.
            if role == "driver":
                error = (
                    "<p class='error'>"
                    "Для цієї машини пароль ще не встановлено. "
                    "Директор має встановити його в розділі «🔐 Паролі»."
                    "</p>"
                )
            else:
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


@app.route("/company/register", methods=["GET", "POST"])
def company_register():
    error = ""
    if request.method == "POST":
        company_name = str(request.form.get("company_name") or "").strip()
        nip = re.sub(r"[^0-9A-Za-z]", "", str(request.form.get("company_tax_id") or "")).upper()
        director_name = str(request.form.get("director_name") or "").strip()
        login_value = str(request.form.get("company_director_login") or "").strip()
        password = str(request.form.get("company_new_password") or "")
        password2 = str(request.form.get("company_new_password2") or "")
        login_key = _tenant_login_key(login_value)
        if not company_name or not nip or not director_name or not login_key:
            error = "Заповніть усі поля."
        elif len(password) < 8:
            error = "Пароль має містити щонайменше 8 символів."
        elif password != password2:
            error = "Паролі не співпадають."
        else:
            with TENANT_ACCOUNTS_LOCK:
                data = _load_tenant_accounts(); companies=data.setdefault("companies",{}); users=data.setdefault("users",{})
                duplicate_nip = any(str(x.get("nip") or "").upper()==nip for x in companies.values() if isinstance(x,dict))
                if login_key in users: error = "Такий логін уже використовується."
                elif duplicate_nip: error = "Компанія з таким NIP уже зареєстрована."
                else:
                    company_id=uuid.uuid4().hex; user_id=uuid.uuid4().hex
                    companies[company_id]={"id":company_id,"name":company_name,"nip":nip,"created_at":datetime.now(timezone.utc).isoformat(),"vehicles":[]}
                    users[login_key]={"id":user_id,"company_id":company_id,"name":director_name,"login":login_value,"role":"director","password_hash":generate_password_hash(password),"enabled":True}
                    _write_tenant_accounts(data)
                    clear_session_keep_language(); session["logged_in"]=True; session["role"]="director"; session["username"]=login_value; session["tenant_company_id"]=company_id; session["tenant_user_id"]=user_id
                    return redirect(url_for("company_dashboard"))
    warning = "" if _tenant_storage_is_persistent() else "<p class='error'>Тестовий режим: після перезапуску Render реєстрація може зникнути.</p>"
    error_html = "<p class='error'>"+escape(error)+"</p>" if error else ""
    body = """<div class="card" style="max-width:620px;margin:0 auto"><h2>Реєстрація компанії в TRANVIQ</h2>{warning}{error}<form method="post" autocomplete="off"><p><label>Назва компанії</label><input name="company_name" autocomplete="organization" autocapitalize="words" required></p><p><label>NIP / VAT ID</label><input name="company_tax_id" id="company_tax_id" autocomplete="off" autocorrect="off" autocapitalize="characters" spellcheck="false" inputmode="text" required></p><p><label>Ім’я директора</label><input name="director_name" autocomplete="name" required></p><p><label>E-mail або логін директора</label><input name="company_director_login" autocomplete="username" required></p><p><label>Пароль</label><input name="company_new_password" type="password" autocomplete="new-password" required></p><p><label>Повторіть пароль</label><input name="company_new_password2" type="password" autocomplete="new-password" required></p><button type="submit">Створити компанію</button> <a class="button" href="/company/login">Уже маю акаунт</a></form></div>""".format(warning=warning,error=error_html)
    return page("Реєстрація компанії", body, "tenant_public")

@app.route("/company/login", methods=["GET", "POST"])
def company_login():
    error=""
    if request.method=="POST":
        login_value=str(request.form.get("login") or "").strip(); password=str(request.form.get("password") or "")
        with TENANT_ACCOUNTS_LOCK: data=_load_tenant_accounts()
        user=data.get("users",{}).get(_tenant_login_key(login_value))
        if isinstance(user,dict) and user.get("enabled") and check_password_hash(str(user.get("password_hash") or ""),password):
            clear_session_keep_language(); session["logged_in"]=True; session["role"]=str(user.get("role") or "dispatcher"); session["username"]=str(user.get("login") or login_value); session["tenant_company_id"]=str(user.get("company_id") or ""); session["tenant_user_id"]=str(user.get("id") or "")
            if session["role"] == "driver":
                session.permanent = True
            return redirect(url_for("company_dashboard"))

        # Also allow the original O&O TRANS director account to enter through
        # the public "login or register company" screen.
        legacy_director_ok = (
            bool(ADMIN_USER and ADMIN_PASSWORD)
            and hmac.compare_digest(login_value, ADMIN_USER)
            and hmac.compare_digest(password, ADMIN_PASSWORD)
        )
        if legacy_director_ok:
            clear_session_keep_language()
            session["logged_in"] = True
            session["role"] = "director"
            session["username"] = login_value
            return redirect(role_home_url("director"))

        error="Неправильний логін або пароль."
    error_html="<p class='error'>"+escape(error)+"</p>" if error else ""
    body="""<div class="card" style="max-width:440px;margin:0 auto"><h2>Вхід компанії</h2>{error}<form method="post"><p><label>E-mail або логін</label><input name="login" required></p><p><label>Пароль</label><input name="password" type="password" required></p><button type="submit">Увійти</button> <a class="button" href="/company/register">Реєстрація</a></form></div>""".format(error=error_html)
    return page("TRANVIQ — вхід",body,"tenant_public")

@app.route("/company")
def company_dashboard():
    return redirect(role_home_url())

@app.route("/company/users", methods=["GET", "POST"])
def company_users():
    company = _tenant_company()
    if not company or current_role() != 'director': return redirect(role_home_url())
    message = ''
    if request.method == 'POST':
        if not _tenant_check_csrf(): return Response('Оновіть сторінку та повторіть.',status=400)
        name = str(request.form.get('name') or '').strip()[:120]
        login_value = str(request.form.get('login') or '').strip()[:120]
        password = str(request.form.get('password') or '')
        role = request.form.get('role','dispatcher')
        vehicle_id = request.form.get('vehicle_id','')
        key = _tenant_login_key(login_value)
        if not name or not key or len(password)<8: message = 'Вкажіть ім’я, логін та пароль мінімум 8 символів.'
        elif role not in {'dispatcher','driver'}:message = 'Оберіть роль.'
        elif role == 'driver' and not vehicle_by_id(vehicle_id):message = 'Призначте автомобіль водію.'
        else:
            with TENANT_ACCOUNTS_LOCK:
                data = _load_tenant_accounts()
                users = data.setdefault('users',{})
                if key in users:message = 'Такий логін уже використовується.'
                else:
                    users[key] = {'id':uuid.uuid4().hex,'company_id':company['id'],'name':name,'login':login_value,'role':role,'vehicle_id':vehicle_id if role == 'driver' else '', 'password_hash':generate_password_hash(password),'enabled':True}
                    _write_tenant_accounts(data)
                    message = 'Доступ створено.'
    with TENANT_ACCOUNTS_LOCK: data = _load_tenant_accounts()
    rows=[]
    for user in data.get('users',{}).values():
        if user.get('company_id') != company['id']:continue
        vehicle=vehicle_by_id(user.get('vehicle_id')) or {}
        rows.append('<tr><td>'+escape(user.get('name',''))+'</td><td>'+escape(user.get('login',''))+'</td><td>'+escape(ROLE_LABELS.get(user.get('role'),'—'))+'</td><td>'+escape(vehicle.get('plate','—'))+'</td></tr>')
    options=''.join('<option value="'+escape(v['id'])+'">'+escape(v.get('plate') or v['name'])+'</option>' for v in VEHICLES)
    body='<div class="card"><h2>Команда</h2><p>'+escape(message)+'</p><table><tr><th>Ім’я</th><th>Логін</th><th>Роль</th><th>Автомобіль</th></tr>'+''.join(rows)+'</table></div><div class="card"><h2>Додати користувача</h2><form method="post">'+_tenant_form_token()+'<p><label>Ім’я</label><input name="name" required></p><p><label>Логін</label><input name="login" required autocomplete="off"></p><p><label>Роль</label><select name="role"><option value="dispatcher">Логіст</option><option value="driver">Водій</option></select></p><p><label>Автомобіль для водія</label><select name="vehicle_id"><option value="">Оберіть</option>'+options+'</select></p><p><label>Пароль</label><input name="password" type="password" minlength="8" autocomplete="new-password" required></p><button>Створити доступ</button></form></div>'
    return page('Команда компанії',body,'company_users')

@app.route('/company/vehicles', methods=['GET', 'POST'])
def company_vehicles():
    company = _tenant_company()
    if not company: return redirect(url_for('company_login'))
    message = ''
    if request.method == 'POST':
        if current_role() != 'director': return Response('Доступ лише для директора.', status=403)
        if not _tenant_check_csrf(): return Response('Оновіть сторінку та повторіть.', status=400)
        plate = str(request.form.get('plate') or '').strip().upper()[:32]
        name = str(request.form.get('name') or '').strip()[:120]
        navirec_id = normalize_api_id(request.form.get('navirec_id'))
        vehicle_id = str(request.form.get('vehicle_id') or '')
        lat_text = str(request.form.get('latitude') or '').strip()
        lon_text = str(request.form.get('longitude') or '').strip()
        lat, lon = safe_float(lat_text), safe_float(lon_text)
        if not plate:
            message = 'Вкажіть номер автомобіля.'
        elif (lat_text or lon_text) and (lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180):
            message = 'Вкажіть обидві правильні координати.'
        else:
            with TENANT_ACCOUNTS_LOCK:
                data = _load_tenant_accounts()
                record = data['companies'][company['id']]
                vehicles = record.setdefault('vehicles', [])
                vehicle = next((v for v in vehicles if v.get('id') == vehicle_id), None) if vehicle_id else None
                if vehicle_id and vehicle is None: return Response('Автомобіль не знайдено.', status=404)
                if any(v.get('plate') == plate and v is not vehicle for v in vehicles):
                    message = 'Такий номер уже є у вашому парку.'
                else:
                    if vehicle is None:
                        vehicle = {'id': uuid.uuid4().hex}
                        vehicles.append(vehicle)
                    vehicle.update(plate=plate, name=name or plate, navirec_id=navirec_id, gps_provider=record.get('gps',{}).get('provider','navirec'))
                    if lat is not None and lon is not None:
                        vehicle.update(latitude=lat, longitude=lon, position_at=datetime.now(timezone.utc).isoformat())
                    _write_tenant_accounts(data)
                    return redirect(url_for('company_vehicles'))
    company = _tenant_company()
    rows = []
    for vehicle in company.get('vehicles', []):
        rows.append('<tr><td>'+escape(vehicle.get('plate', ''))+'</td><td>'+escape(vehicle.get('name', ''))+'</td><td>'+escape(vehicle.get('navirec_id', '') or 'Не підключено')+'</td><td><a href="/company/vehicles?edit='+escape(vehicle['id'])+'">Редагувати</a> · <a href="/company/gps?vehicle='+escape(vehicle['id'])+'">GPS</a></td></tr>')
    edit_id = str(request.args.get('edit') or '')
    selected = next((v for v in company.get('vehicles', []) if v.get('id') == edit_id), {})
    if edit_id and not selected: return Response('Автомобіль не знайдено.', status=404)
    form = ''
    if current_role() == 'director':
        fields = [('plate', 'Номер автомобіля', 'text'), ('name', 'Назва автомобіля', 'text'), ('navirec_id', 'GPS-ID (заповнюється після імпорту)', 'text'), ('latitude', 'Широта (для ручної позиції)', 'number'), ('longitude', 'Довгота (для ручної позиції)', 'number')]
        inputs = ''.join('<p><label>'+label+'</label><input name="'+key+'" type="'+kind+'" step="any" value="'+escape(str(selected.get(key, '')))+'" '+('required' if key == 'plate' else '')+'></p>' for key,label,kind in fields)
        form = '<div class="card"><h2>'+('Редагувати автомобіль' if selected else 'Додати автомобіль')+'</h2><form method="post">'+_tenant_form_token()+'<input type="hidden" name="vehicle_id" value="'+escape(selected.get('id', ''))+'">'+inputs+'<button>Зберегти</button></form></div>'
    body = '<div class="card"><h2>Парк компанії</h2><a class="button" href="/company/gps/settings">Вибрати машини з GPS</a><p>'+escape(message)+'</p><table><tr><th>Номер</th><th>Назва</th><th>Navirec</th><th>Дії</th></tr>'+''.join(rows)+'</table>'+('' if rows else '<p>Додайте перший автомобіль.</p>')+'</div>'+form
    return page('Автомобілі компанії', body, 'company_vehicles')

@app.route('/company/gps/settings', methods=['GET', 'POST'])
def company_gps_settings():
    company = _tenant_company()
    if not company: return redirect(url_for('company_login'))
    if current_role() != 'director': return Response('Доступ лише для директора.', status=403)
    message = ''
    settings = company.get('gps', {})
    candidates = []
    if request.method == 'POST':
        if not _tenant_check_csrf(): return Response('Оновіть сторінку та повторіть.', status=400)
        action = request.form.get('action', 'save')
        if action == 'import':
            try:
                available = {v['id']:v for v in gps_list_vehicles(settings) if v.get('id')}
                chosen = request.form.getlist('vehicles')
                if not chosen: raise GPSError('Оберіть автомобілі зі списку.')
                if any(v not in available for v in chosen): raise GPSError('Список змінився. Оновіть його.')
                with TENANT_ACCOUNTS_LOCK:
                    data = _load_tenant_accounts()
                    record = data['companies'][company['id']]
                    fleet = record.setdefault('vehicles', [])
                    for external in chosen:
                        provider = settings.get('provider', 'navirec')
                        if any(v.get('navirec_id') == external and v.get('gps_provider', 'navirec') == provider for v in fleet): continue
                        remote = available[external]
                        fleet.append({'id':uuid.uuid4().hex,'name':remote['name'],'plate':remote['plate'] or remote['name'],'navirec_id':external,'gps_provider':provider})
                    _write_tenant_accounts(data)
                return redirect(url_for('vehicles'))
            except GPSError as exc: message = str(exc)
        else:
            provider = request.form.get('provider', 'navirec')
            if provider not in PROVIDERS: return Response('Оберіть постачальника GPS.', status=400)
            new_settings = {'provider':provider,'provider_name':str(request.form.get('provider_name') or '').strip()[:100],'account_id':normalize_api_id(request.form.get('account_id')),'region':request.form.get('region', 'com')}
            token = str(request.form.get('navirec_token') or '').strip()
            new_settings['token'] = token or (settings.get('token', '') if settings.get('provider', 'navirec') == provider else '')
            if provider in {'manual','other'}: new_settings['token'] = ''
            if new_settings['region'] not in {'com','eu','us','org'}: return Response('Оберіть регіон.', status=400)
            if provider in {'navirec','wialon'}:
                try:
                    candidates = gps_list_vehicles(new_settings)
                except GPSError as exc: message = str(exc)
            if not message:
                with TENANT_ACCOUNTS_LOCK:
                    data = _load_tenant_accounts()
                    data['companies'][company['id']]['gps'] = new_settings
                    _write_tenant_accounts(data)
                settings = new_settings
                message = 'Підключення збережено. Оберіть машини нижче.' if provider in {'navirec','wialon'} else ('Ручні позиції увімкнено.' if provider == 'manual' else 'Назву GPS збережено. Для цього постачальника ще потрібно додати інтеграцію.')
    elif settings.get('token'):
        try: candidates = gps_list_vehicles(settings)
        except GPSError as exc: message = str(exc)
    options = ''.join('<option value="'+key+'"'+(' selected' if key == settings.get('provider','navirec') else '')+'>'+escape(label)+'</option>' for key,label in PROVIDERS.items())
    regions = ''.join('<option value="'+region+'"'+(' selected' if settings.get('region','com') == region else '')+'>wialon.'+region+'</option>' for region in ('com','eu','us','org'))
    body = '<div class="card"><h2>Підключити GPS</h2><p>'+escape(message)+'</p><form method="post">'+_tenant_form_token()+'<input type="hidden" name="action" value="save"><p><label>Постачальник GPS</label><select name="provider">'+options+'</select></p><p><label>Назва іншого GPS</label><input name="provider_name" value="'+escape(settings.get('provider_name',''))+'"></p><p><label>ID акаунта (тільки Navirec)</label><input name="account_id" value="'+escape(str(settings.get('account_id','')))+'"></p><p><label>Сервер Wialon (як у вашому кабінеті)</label><select name="region">'+regions+'</select></p><p><label>API-токен (порожнє поле зберігає чинний токен цього постачальника)</label><input name="navirec_token" type="password" autocomplete="new-password"></p><button>Підключити та отримати список машин</button></form><p>Іншого постачальника підключаємо через його API. Паливо, тахограф та історія залежать від даних, які він надає.</p></div>'
    if candidates:
        checks = ''.join('<p><label><input type="checkbox" name="vehicles" value="'+escape(v['id'])+'"> '+escape(v['name'])+' · '+escape(v['plate'])+'</label></p>' for v in candidates if v.get('id'))
        body += '<div class="card"><h2>Машини з вашого GPS-акаунта</h2><form method="post">'+_tenant_form_token()+'<input type="hidden" name="action" value="import">'+checks+'<button>Додати вибрані автомобілі</button></form></div>'
    return page('Підключення GPS', body, 'company_gps_settings')


@app.route('/company/gps')
def company_gps():
    return redirect(url_for("gps", **({"vehicle": request.args["vehicle"]} if request.args.get("vehicle") else {})))


def tenant_driver_access():
    if current_role() != 'director': return Response('Доступ лише для директора.',status=403)
    company=tenancy.company()
    message=''
    if request.method=='POST':
        if not _tenant_check_csrf():return Response('Оновіть сторінку та повторіть.',status=400)
        user_id=request.form.get('user_id','')
        password=str(request.form.get('password') or '')
        action=request.form.get('action','save')
        if action != 'disable' and len(password)<8:message='Пароль має містити мінімум 8 символів.'
        else:
            with TENANT_ACCOUNTS_LOCK:
                data=_load_tenant_accounts()
                user=next((u for u in data.get('users',{}).values() if u.get('id')==user_id and u.get('company_id')==company['id'] and u.get('role')!='director'),None)
                if not user:return Response('Користувача не знайдено.',status=404)
                if action=='disable':user['enabled']=False
                else:user.update(password_hash=generate_password_hash(password),enabled=True)
                _write_tenant_accounts(data)
                message='Доступ збережено.'
    with TENANT_ACCOUNTS_LOCK:data=_load_tenant_accounts()
    rows=[]
    for user in data.get('users',{}).values():
        if user.get('company_id')!=company['id'] or user.get('role')=='director':continue
        rows.append('<div class="card"><h3>'+escape(user.get('name',''))+'</h3><p>Логін: '+escape(user.get('login',''))+' · '+('увімкнено' if user.get('enabled') else 'вимкнено')+'</p><form method="post">'+_tenant_form_token()+'<input name="user_id" type="hidden" value="'+escape(user['id'])+'"><label>Новий пароль</label><input name="password" type="password" autocomplete="new-password"><button name="action" value="save">Зберегти пароль</button> <button name="action" value="disable">Вимкнути доступ</button></form></div>')
    return page('Паролі та доступи','<p>'+escape(message)+'</p><a class="button" href="/company/users">Додати водія або логіста</a>'+''.join(rows),'driver_access')

@app.route("/logout")
def logout():
    clear_session_keep_language()
    return redirect(url_for("login"))


@app.after_request
def tenant_response_protection(response):
    if tenancy.company():
        response.headers["Cache-Control"] = "private, no-store"
        if response.mimetype == "text/html":
            html = response.get_data(as_text=True)
            token = _tenant_csrf()
            html = re.sub(r"(<form\b[^>]*>)", lambda match: match.group(1)+_tenant_form_token(), html, flags=re.I)
            script = "<script>const tenantCSRF="+json.dumps(token)+";const tenantFetch=window.fetch.bind(window);window.fetch=function(input,options){const target=new URL(typeof input==='string'?input:input.url,location.href);const opts=Object.assign({},options||{});const method=(opts.method||(input instanceof Request?input.method:'GET')).toUpperCase();if(target.origin===location.origin&&!['GET','HEAD','OPTIONS'].includes(method)){const headers=new Headers(opts.headers||(input instanceof Request?input.headers:undefined));headers.set('X-CSRF-Token',tenantCSRF);opts.headers=headers;}return tenantFetch(input,opts);};</script>"
            html = html.replace("</head>",script+"</head>",1)
            response.set_data(html)
    return response

@app.before_request
def bind_request_company():
    from flask import g
    g.company_scope_token = tenancy.bind_company(None)
    if is_logged_in() and session.get("tenant_company_id"):
        try:
            company = _tenant_company()
        except Exception:
            app.logger.error("Company store unavailable")
            return Response("База компаній тимчасово недоступна. Спробуйте пізніше.", status=503)
        if not company:
            clear_session_keep_language()
            return redirect(url_for("company_login"))
        tenancy.bind_company(company)
        with TENANT_ACCOUNTS_LOCK:
            user = _load_tenant_accounts().get("users", {}).get(_tenant_login_key(session.get("username")), {})
        session["role"] = user["role"]
        if current_role() == "driver":
            assigned = user.get("vehicle_id")
            if not any(v.get("id") == assigned for v in company.get("vehicles", [])):
                clear_session_keep_language()
                return redirect(url_for("company_login"))
            session["driver_vehicle_id"] = assigned

@app.teardown_request
def clear_request_company(error):
    from flask import g
    token = getattr(g, "company_scope_token", None)
    if token is not None: tenancy.clear_company(token)

@app.before_request
def require_login():
    if (
        request.path == "/health"
        or request.path == "/login"
        or request.path.startswith("/login/")
        or request.path == "/assets/company-logo.jpg"
        or request.path == "/assets/tranviq-driver-icon.svg"
        or request.path == "/driver-manifest.webmanifest"
        or request.path == "/driver-service-worker.js"
        or request.path == "/language"
        or request.path == "/callback"
        or request.path == "/company/register"
        or request.path == "/company/login"
        or request.path == "/api/finance/email-invoices/import"
    ):
        return None

    if not is_logged_in():
        return redirect(url_for("login"))

    if tenancy.company() and request.method not in {"GET", "HEAD", "OPTIONS"} and not _tenant_check_csrf():
        return jsonify({"error": "Оновіть сторінку та повторіть дію."}), 400
    # Tenant scope is already bound. Use the same role gate as the main company.
    if tenancy.company():
        if request.endpoint in {"company_vehicles", "company_gps", "company_gps_settings", "company_users", "company_dashboard"}:
            if current_role() == "driver": return redirect(url_for("driver_dashboard"))
            return None

    role = current_role()
    if tenancy.company() and role == "driver":
        vehicle_id = (request.view_args or {}).get("vehicle_id")
        if request.endpoint == "delivery_stop_status": vehicle_id = (request.get_json(silent=True) or {}).get("vehicle_id")
        if vehicle_id and str(vehicle_id) != str(session.get("driver_vehicle_id")):
            return jsonify({"error": "Автомобіль недоступний."}), 404

    if role == "director":
        return None

    allowed_endpoints = ROLE_ENDPOINTS.get(role, set())

    if (
        request.endpoint in allowed_endpoints
        or request.endpoint == "logout"
    ):
        return None

    return redirect(role_home_url(role))


from documents import register_document_routes
register_document_routes(app, page, DELIVERY_ROUTES_FILE, VEHICLES)


@app.route("/driver-access", methods=["GET", "POST"])
def driver_access():
    if tenancy.company(): return tenant_driver_access()
    message = ""
    if request.method == "POST":
        vehicle_id = normalize_vehicle_id(request.form.get("vehicle_id", ""))
        action = request.form.get("action", "save")
        vehicle = vehicle_by_id(vehicle_id)
        if not vehicle:
            message = "<p class='error'>Nie znaleziono pojazdu.</p>"
        else:
            with DRIVER_ACCESS_LOCK:
                access = _load_driver_access()
                if action == "disable":
                    access[vehicle_id] = {"enabled": False, "password_hash": access.get(vehicle_id, {}).get("password_hash", "")}
                    _write_driver_access(access)
                    message = "<p class='ok'>Доступ вимкнено для {}</p>".format(escape(vehicle["plate"]))
                else:
                    password = request.form.get("password", "")
                    if len(password) < 4:
                        message = "<p class='error'>Пароль має містити щонайменше 4 символи.</p>"
                    else:
                        access[vehicle_id] = {
                            "enabled": True,
                            "password_hash": generate_password_hash(password),
                            "updated_at": datetime.now(POLAND_TZ).isoformat()
                        }
                        _write_driver_access(access)
                        message = "<p class='ok'>Пароль збережено для {}</p>".format(escape(vehicle["plate"]))

    access = _load_driver_access()
    rows = []
    for vehicle in VEHICLES:
        item = access.get(vehicle["id"], {})
        enabled = bool(item.get("enabled") and item.get("password_hash"))
        rows.append("""
        <div class="card" style="margin-bottom:12px">
          <h3>{plate}</h3>
          <div class="small">Логін водія: <strong>{plate}</strong></div>
          <div class="small" style="margin:5px 0 12px">Статус: <strong>{status}</strong></div>
          <form method="post">
            <input type="hidden" name="vehicle_id" value="{vehicle_id}">
            <label>Новий пароль</label>
            <input name="password" type="password" autocomplete="new-password" placeholder="Введи пароль" style="width:100%;box-sizing:border-box;margin:6px 0 10px">
            <button type="submit" name="action" value="save">💾 ЗБЕРЕГТИ ПАРОЛЬ</button>
            <button type="submit" name="action" value="disable" style="margin-left:6px;background:#c92a2a">⛔ ВИМКНУТИ ДОСТУП</button>
          </form>
        </div>
        """.format(
            plate=escape(vehicle.get("plate") or vehicle["name"]),
            vehicle_id=escape(vehicle["id"]),
            status=("Доступ увімкнений" if enabled else "Пароль ще не встановлено")
        ))
    storage_notice = ""
    if not _driver_access_storage_is_persistent():
        storage_notice = """
        <div class="card" style="border:2px solid #c92a2a;background:#fff5f5">
          <strong>⚠️ Паролі зараз зберігаються у тимчасовому сховищі Render.</strong>
          <p style="margin-bottom:0">
            Після нового deploy/restart вони можуть зникнути.
            Для постійного збереження підключи Persistent Disk до
            <code>/var/data</code>. Після цього пароль, встановлений тут один раз,
            залишатиметься до моменту, коли директор сам його змінить або вимкне.
          </p>
        </div>
        """

    body = """
    <div class="card">
      <h2>🔐 Доступ водіїв</h2>
      <p>Логін для кожної машини — її державний номер. Тут ти сам задаєш або змінюєш пароль.</p>
      <p class="small">Зміна пароля не викидає водія, який уже увійшов. Новий пароль діятиме при наступному вході.</p>
    </div>
    {storage_notice}
    {message}
    {rows}
    """.format(
        storage_notice=storage_notice,
        message=message,
        rows="".join(rows)
    )
    return page("Паролі водіїв", body, "driver_access")


@app.route("/director")
def director_dashboard():
    return redirect(url_for("home"))


@app.route("/dispatcher")
def dispatcher_dashboard():
    # The dispatcher home is the live operational GPS/route map.
    return redirect(url_for("gps"))


def _legacy_dispatcher_dashboard():
    lang = current_language()
    vehicle_labels = {
        "uk": {"speed": "Швидкість", "fuel": "Паливо", "heading": "Напрямок", "engine": "Оберти двигуна", "distance": "Загальна відстань", "ignition": "Запалювання", "history": "Історія маршруту"},
        "pl": {"speed": "Prędkość", "fuel": "Paliwo", "heading": "Kierunek", "engine": "Obroty silnika", "distance": "Całkowity przebieg", "ignition": "Zapłon", "history": "Historia trasy"},
        "en": {"speed": "Speed", "fuel": "Fuel", "heading": "Heading", "engine": "Engine RPM", "distance": "Total distance", "ignition": "Ignition", "history": "Route history"},
        "de": {"speed": "Geschwindigkeit", "fuel": "Kraftstoff", "heading": "Fahrtrichtung", "engine": "Motordrehzahl", "distance": "Gesamtstrecke", "ignition": "Zündung", "history": "Routenverlauf"},
    }.get(lang, {})

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



# Fuel-control state is persistent per company/vehicle. It stores only
# operational baselines and calibration values; live GPS/fuel data still comes
# from Navirec.
FUEL_TRACKING_FILE = os.path.join(os.path.dirname(DELIVERY_ROUTES_FILE), "tranviq_fuel_tracking.json")
FUEL_TRACKING_LOCK = threading.Lock()


def _load_fuel_tracking():
    if tenancy.company():
        return tenancy.json_read("fuel_tracking", {})
    try:
        with open(FUEL_TRACKING_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_fuel_tracking(data):
    if tenancy.company():
        return tenancy.json_write("fuel_tracking", data)
    folder = os.path.dirname(FUEL_TRACKING_FILE)
    if folder:
        os.makedirs(folder, exist_ok=True)
    temporary = FUEL_TRACKING_FILE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, FUEL_TRACKING_FILE)


def _fuel_calibration_factor(vehicle_data):
    rows = vehicle_data.get("calibrations", []) if isinstance(vehicle_data, dict) else []
    ratios = []
    for item in rows[-12:]:
        if not isinstance(item, dict):
            continue
        actual = safe_float(item.get("actual_liters"))
        navirec = safe_float(item.get("navirec_liters"))
        if actual and navirec and actual > 0 and navirec > 0:
            ratio = actual / navirec
            if 0.70 <= ratio <= 1.30:
                ratios.append(ratio)
    if not ratios:
        return 1.0
    # Median is intentionally used instead of an average so one bad receipt or
    # one noisy fuel reading cannot distort a vehicle's long-term correction.
    ordered = sorted(ratios)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


# Lightweight internal TRANVIQ messenger. Stored beside route data so it survives
# normal page refreshes and can use Render persistent disk when /var/data is mounted.
MESSAGES_FILE = os.path.join(os.path.dirname(DELIVERY_ROUTES_FILE), "tranviq_messages.json")
MESSAGES_LOCK = threading.Lock()


def _load_messages():
    if tenancy.company(): return tenancy.json_read("messages", [])
    try:
        with open(MESSAGES_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except (OSError, ValueError, TypeError):
        return []


def _write_messages(items):
    if tenancy.company(): return tenancy.json_write("messages", items)
    folder = os.path.dirname(MESSAGES_FILE)
    if folder:
        os.makedirs(folder, exist_ok=True)
    temporary = MESSAGES_FILE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(items[-2000:], handle, ensure_ascii=False)
    os.replace(temporary, MESSAGES_FILE)


def _message_identity():
    role = current_role()
    if role == "driver":
        # Pilot driver account is bound to SH 9203G.
        v = current_driver_vehicle()
        return "vehicle:" + str(v["id"]), v.get("plate") or v.get("name") or "SH"
    if role == "dispatcher":
        return "role:dispatcher", "Logistyk"
    if role == "director":
        return "role:director", "Dyrektor"
    return "", ""


@app.route("/api/messages/recipients")
def message_recipients():
    if not current_role():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    recipients = [
        {"id": "role:director", "label": "Dyrektor", "kind": "role"},
        {"id": "role:dispatcher", "label": "Logistyk", "kind": "role"},
    ]
    for vehicle in VEHICLES:
        recipients.append({
            "id": "vehicle:" + str(vehicle["id"]),
            "label": vehicle.get("plate") or vehicle.get("name") or str(vehicle["id"]),
            "kind": "driver",
        })
    return jsonify({"ok": True, "recipients": recipients})


@app.route("/api/messages", methods=["GET", "POST"])
def internal_messages():
    sender_id, sender_label = _message_identity()
    if not sender_id:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        recipient = str(payload.get("recipient") or "").strip()
        text = str(payload.get("text") or "").strip()
        if not recipient or not text:
            return jsonify({"ok": False, "error": "recipient_and_text_required"}), 400
        allowed = {"role:director", "role:dispatcher"}
        allowed.update("vehicle:" + str(v["id"]) for v in VEHICLES)
        if recipient not in allowed:
            return jsonify({"ok": False, "error": "bad_recipient"}), 400
        item = {
            "id": uuid.uuid4().hex,
            "sender": sender_id,
            "sender_label": sender_label,
            "recipient": recipient,
            "text": text,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "read_by": [sender_id],
        }
        with MESSAGES_LOCK:
            items = _load_messages()
            items.append(item)
            _write_messages(items)
        return jsonify({"ok": True, "message": item})

    after = str(request.args.get("after") or "").strip()
    with MESSAGES_LOCK:
        items = _load_messages()
    visible = []
    for item in items:
        if item.get("sender") == sender_id or item.get("recipient") == sender_id:
            if after and str(item.get("created_at") or "") <= after:
                continue
            visible.append(item)
    return jsonify({"ok": True, "messages": visible[-200:]})


@app.route("/api/messages/read", methods=["POST"])
def internal_messages_read():
    identity, _ = _message_identity()
    if not identity:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    ids = {str(x) for x in (payload.get("ids") or [])}
    if not ids:
        return jsonify({"ok": True})
    with MESSAGES_LOCK:
        items = _load_messages()
        changed = False
        for item in items:
            if item.get("id") in ids and item.get("recipient") == identity:
                read_by = item.get("read_by") if isinstance(item.get("read_by"), list) else []
                if identity not in read_by:
                    read_by.append(identity)
                    item["read_by"] = read_by
                    changed = True
        if changed:
            _write_messages(items)
    return jsonify({"ok": True})


@app.route("/api/driver-tachograph/<vehicle_id>")
def driver_tachograph_api(vehicle_id):
    """Operational tachograph snapshot for the logged-in driver UI.

    Returns only time/status values already exposed by the shared tachograph
    calculation. Missing Navirec values stay null; the UI must never invent them.
    """
    vehicle_id = normalize_api_id(vehicle_id) or str(vehicle_id)
    # Use the same live vehicle-state source as the working GPS/tachograph view.

    states = get_vehicle_states()
    if states:
        with _LAST_GOOD_TACHO_LOCK:
            tenancy.cache("tacho")["states"] = list(states)
            tenancy.cache("tacho")["at"] = time.time()
    else:
        with _LAST_GOOD_TACHO_LOCK:
            if (
                tenancy.cache("tacho").get("states", [])
                and time.time() - tenancy.cache("tacho").get("at", 0) <= NAVIREC_LIST_STALE_MAX
            ):
                states = list(tenancy.cache("tacho").get("states", []))

    snapshots = build_tachograph_snapshots(states)
    snapshot = snapshots.get(vehicle_id)
    if snapshot is None:
        return jsonify({"ok": False, "vehicle_id": vehicle_id, "tachograph": None}), 404
    return jsonify({
        "ok": True,
        "vehicle_id": vehicle_id,
        "tachograph": snapshot,
        "source_ok": True,
    })


@app.route("/driver")
def driver_dashboard():
    if not current_driver_vehicle(): return page("Кабінет водія", "<p>Директор має призначити автомобіль.</p>", "driver")
    # Each driver sees the vehicle assigned by their login.
    driver_vehicle = current_driver_vehicle()
    vehicle_id = driver_vehicle["id"]
    vehicle_name = driver_vehicle["name"]
    vehicle_plate = driver_vehicle.get("plate") or vehicle_name

    body = """
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
       .driver-tabs{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:12px 0}.driver-tab{border:1px solid #adb5bd;background:#fff;padding:18px 10px;border-radius:16px;font-weight:900;cursor:pointer;min-height:76px;font-size:16px}.driver-tab.active{background:#0b7285;color:#fff;border-color:#0b7285}.driver-msg{display:none}.driver-msg-list{display:grid;gap:8px;max-height:46vh;overflow:auto;margin:12px 0}.driver-msg-item{padding:10px 12px;border:1px solid #d8e1e5;border-radius:12px;background:#fff}.driver-msg-item.mine{background:#e7f5ff}.driver-msg-meta{font-size:11px;color:#68757d;margin-bottom:4px}.driver-msg-compose{display:grid;gap:8px}.driver-msg-compose select,.driver-msg-compose textarea{width:100%;box-sizing:border-box;border:1px solid #adb5bd;border-radius:10px;padding:10px;font-size:16px}.driver-msg-actions{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}.driver-msg-voice,.driver-msg-clear,.driver-msg-send{border:0;border-radius:11px;padding:12px;color:#fff;font-weight:900;cursor:pointer}.driver-msg-voice{background:#7048e8}.driver-msg-voice.listening{background:#c2255c}.driver-msg-clear{background:#6c757d}.driver-msg-send{background:#0b7285}.driver-msg-heard{display:none;padding:10px;border:1px solid #d8e1e5;border-radius:10px;background:#f8f9fa}.driver-msg-heard.show{display:block}.driver-msg-heard-label{font-size:11px;font-weight:900;color:#68757d;text-transform:uppercase;margin-bottom:4px}.driver-unread{display:inline-flex;min-width:20px;height:20px;padding:0 5px;align-items:center;justify-content:center;border-radius:999px;background:#c92a2a;color:#fff;font-size:11px;margin-left:5px}
      #driverMapPane{display:none}.driver-map{height:58vh;min-height:390px;border-radius:16px;overflow:hidden;border:1px solid #ced4da}.driver-map-note{font-size:12px;color:#68757d;margin:8px 0}.driver-vehicle-card{font-size:13px;line-height:1.35}.driver-vehicle-card strong{font-size:15px}.driver-to-vehicle{display:inline-block;margin-top:8px;padding:8px 10px;border-radius:9px;background:#0b7285;color:white!important;text-decoration:none;font-weight:900}
      .driver-shell{max-width:760px;margin:0 auto;padding-bottom:90px}
      .driver-head{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:12px}
      .driver-plate{font-size:20px;font-weight:900}
      .driver-live{font-size:12px;font-weight:800;color:#087f5b}
      .driver-next{border:2px solid #0b7285;border-radius:16px;padding:16px;background:#f8fdff;margin:12px 0}
      .driver-kicker{font-size:12px;font-weight:900;text-transform:uppercase;color:#5c6b73;margin-bottom:6px}
      .driver-address{font-size:22px;line-height:1.2;font-weight:900;margin:5px 0 8px}
      .driver-window{font-size:15px;font-weight:800;margin-bottom:12px}
      .driver-actions{display:grid;grid-template-columns:1fr 1fr;gap:10px}
      .driver-btn{display:flex;align-items:center;justify-content:center;min-height:54px;border-radius:12px;border:0;font-size:16px;font-weight:900;text-decoration:none;cursor:pointer}
      .driver-nav{background:#0b7285;color:white}.driver-done{background:#2f9e44;color:white}.driver-done:disabled{opacity:.45}
      .driver-status-actions{display:grid;grid-template-columns:1fr;gap:7px;margin-top:10px}.driver-status-choice{min-height:46px;border:0;border-radius:11px;padding:9px 10px;font-weight:900;cursor:pointer}.driver-status-choice:disabled{opacity:.55;cursor:default}.driver-status-done{background:#2f9e44;color:#fff}.driver-status-refused{background:#f2ad16;color:#17202a}.driver-status-pending{background:#d63b32;color:#fff}
      .driver-list{display:grid;gap:8px;margin-top:12px}.driver-stop{border:1px solid #d8e1e5;border-radius:12px;padding:11px;background:white;display:grid;grid-template-columns:36px 1fr;gap:9px}
      .driver-stop.current{border:2px solid #f59f00;background:#fff9db}.driver-stop.refused{border:2px solid #f2ad16;background:#fff3bf}.driver-stop.completed{opacity:.78;background:#f1f3f5}
      .driver-num{width:32px;height:32px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:#e9ecef;font-weight:900}.driver-stop.current .driver-num,.driver-stop.refused .driver-num{background:#f59f00;color:#17202a}.driver-stop.completed .driver-num{background:#2f9e44;color:white}
      .driver-stop-status{display:grid;grid-template-columns:1fr;gap:5px;margin-top:8px}.driver-stop-status button{border:0;border-radius:8px;padding:8px 9px;font-size:12px;font-weight:900;cursor:pointer}.driver-stop-status button.active{outline:3px solid rgba(11,114,133,.18)}
      .driver-install{display:none;border:2px solid #7048e8}.driver-install-row{display:flex;gap:10px;align-items:center;justify-content:space-between;flex-wrap:wrap}.driver-install-btn{border:0;border-radius:11px;background:#7048e8;color:#fff;padding:12px 16px;font-weight:900;cursor:pointer}.driver-install-note{font-size:12px;color:#68757d;margin-top:6px}
      .driver-small{font-size:12px;color:#68757d}.driver-empty{padding:22px;text-align:center;border:1px dashed #adb5bd;border-radius:14px;background:#fff}.driver-jobs{display:grid;gap:10px;margin:12px 0}.driver-job{border:1px solid #d8e1e5;border-radius:14px;padding:12px;background:#fff}.driver-job.next{border-left:5px solid #1971c2}.driver-job-title{font-weight:900;font-size:16px}.driver-job-meta{font-size:12px;color:#68757d;margin-top:4px}.driver-job-actions{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:9px}.driver-job-open,.driver-job-nav{display:flex;align-items:center;justify-content:center;min-height:42px;border:0;border-radius:9px;padding:8px 11px;font-weight:900;cursor:pointer;text-decoration:none}.driver-job-open{background:#e7f5ff;color:#0b4f6c}.driver-job-nav{background:#0b7285;color:#fff}.driver-back-active{display:none;background:#495057!important}.driver-stop-nav{display:inline-flex;margin-top:8px;padding:7px 9px;border-radius:8px;background:#0b7285;color:#fff!important;text-decoration:none;font-size:12px;font-weight:900}.driver-iq{display:none}.driver-iq-card{border:2px solid #7048e8;border-radius:16px;padding:16px;background:#f8f7ff}.driver-mic{width:100%;min-height:68px;border:0;border-radius:14px;background:#7048e8;color:#fff;font-size:20px;font-weight:900;cursor:pointer}.driver-mic.listening{background:#c2255c}.driver-iq-box{margin-top:12px;padding:12px;border-radius:12px;background:#fff;border:1px solid #ddd}.driver-iq-label{font-size:12px;font-weight:900;color:#68757d;text-transform:uppercase;margin-bottom:5px}.driver-iq-text{font-size:17px;font-weight:800;min-height:24px}.driver-iq-action{margin-top:10px;display:flex;gap:8px;flex-wrap:wrap}.driver-iq-action a,.driver-iq-action button{border:0;border-radius:10px;padding:10px 12px;background:#0b7285;color:#fff;text-decoration:none;font-weight:900;cursor:pointer}
      .driver-tacho{display:none}.driver-tacho-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.driver-tacho-item{border:1px solid #d8e1e5;border-radius:12px;padding:12px;background:#fff}.driver-tacho-value{font-size:20px;font-weight:900;margin-top:4px}.driver-tacho-warn{margin-top:10px;padding:10px;border-radius:10px;background:#fff3bf;font-weight:800}.driver-tacho-ok{margin-top:10px;padding:10px;border-radius:10px;background:#d3f9d8;font-weight:800}
      @media(max-width:520px){.driver-tabs{grid-template-columns:1fr 1fr}.driver-actions{grid-template-columns:1fr}.driver-address{font-size:19px}.driver-tacho-grid{grid-template-columns:1fr}}
    

</style>
    <div class="driver-shell">
      <div id="driverInstallCard" class="card driver-install">
        <div class="driver-install-row">
          <div><strong id="driverInstallTitle"></strong><div id="driverInstallNote" class="driver-install-note"></div></div>
          <button id="driverInstallButton" class="driver-install-btn" type="button"></button>
        </div>
      </div>
      <div class="card">
        <div class="driver-head">
          <div><div class="driver-kicker">KIEROWCA · TRASA NA ŻYWO</div><div class="driver-plate">__PLATE__</div></div>
          <div id="driverLive" class="driver-live">● synchronizacja</div>
        </div>
        <div class="driver-small">Trasa wspólna z dyrektorem i logistykiem. Zmiany pojawią się automatycznie.</div>
      </div>

      <div class="driver-tabs"><button id="driverRouteTab" class="driver-tab active" type="button">🗺️ TRASA</button><button id="driverTachoTab" class="driver-tab" type="button">⏱️ TACHOGRAF</button><button id="driverIqTab" class="driver-tab" type="button">🎙️ IQ</button><button id="driverMsgTab" class="driver-tab" type="button">💬 WIADOMOŚCI <span id="driverUnread" class="driver-unread" style="display:none">0</span></button></div>
      <div id="driverRoutePane">
      <div class="card" style="margin:10px 0"><div class="driver-kicker">TWOJE ZLECENIA</div><div id="driverJobs" class="driver-jobs"></div></div>
      <div class="card" style="margin:10px 0"><a href="/documents" class="driver-btn driver-nav" style="width:100%;background:#7048e8;color:#fff">📷 SKANUJ DOKUMENT</a><div class="driver-small" style="margin-top:8px;text-align:center">CMR · Lieferschein · paragon paliwowy · inny dokument</div></div>
      <div class="card" style="margin:10px 0"><button id="driverMapTab" class="driver-btn driver-nav" type="button" style="width:100%">📍 MAPA GPS POJAZDÓW</button></div>
      <div id="driverNext" class="driver-next" style="display:none">
        <div id="driverRouteKicker" class="driver-kicker">NASTĘPNY PUNKT</div>
        <div id="driverNextAddress" class="driver-address"></div>
        <div id="driverNextWindow" class="driver-window"></div>
        <div class="driver-actions">
          <a id="driverNavigate" class="driver-btn driver-nav" href="#" target="_blank" rel="noopener">🧭 NAWIGUJ</a>
          <button id="driverBackActive" class="driver-btn driver-back-active" type="button">↩ AKTUALNA TRASA</button>
        </div>
        <div id="driverStatusActions" class="driver-status-actions">
          <button id="driverComplete" class="driver-status-choice driver-status-done" type="button"></button>
          <button id="driverRefused" class="driver-status-choice driver-status-refused" type="button"></button>
          <button id="driverPending" class="driver-status-choice driver-status-pending" type="button"></button>
        </div>
      </div>

      <div id="driverEmpty" class="driver-empty">Czekam na aktywną trasę dla __PLATE__…</div>
      <div id="driverStops" class="driver-list"></div>
      </div>
      <div id="driverMapPane">
        <div class="driver-map-note" id="driverMapNote">Ładowanie pozycji GPS…</div>
        <div id="driverFleetMap" class="driver-map"></div>
      </div>
      <div id="driverTachoPane" class="driver-tacho">
        <div class="card">
          <div class="driver-kicker">ТАХОГРАФ · __PLATE__</div>
          <div id="driverTachoStatus" class="driver-small">Отримання даних тахографа…</div>
          <div class="driver-tacho-grid" style="margin-top:10px">
            <div class="driver-tacho-item"><div class="driver-small">До наступної перерви</div><div id="tachoBreak" class="driver-tacho-value">—</div></div>
            <div class="driver-tacho-item"><div class="driver-small">Залишок денного часу керування</div><div id="tachoDaily" class="driver-tacho-value">—</div></div>
            <div class="driver-tacho-item"><div class="driver-small">Залишок поточного періоду керування</div><div id="tachoCurrent" class="driver-tacho-value">—</div></div>
            <div class="driver-tacho-item"><div class="driver-small">До добового відпочинку</div><div id="tachoRest" class="driver-tacho-value">—</div></div>
            <div class="driver-tacho-item"><div class="driver-small">Залишок тижневого часу керування</div><div id="tachoWeekly" class="driver-tacho-value">—</div></div>
            <div class="driver-tacho-item"><div class="driver-small">Карта водія</div><div id="tachoCard" class="driver-tacho-value">—</div></div>
          </div>
          <div id="driverTachoNotice" class="driver-tacho-warn">Показуються останні підтверджені дані Navirec. Тимчасово порожній пакет не стирає попередні значення.</div>
        </div>
      </div>
      <div id="driverIqPane" class="driver-iq">
        <div class="driver-iq-card">
          <div class="driver-kicker">IQ · ASYSTENT GŁOSOWY</div>
          <button id="driverMic" class="driver-mic" type="button">🎙 NACIŚNIJ I MÓW</button>
          <div class="driver-iq-box"><div class="driver-iq-label">USŁYSZAŁEM</div><div id="driverTranscript" class="driver-iq-text">—</div></div>
          <div class="driver-iq-box"><div class="driver-iq-label">IQ ZROZUMIAŁ</div><div id="driverIqResult" class="driver-iq-text">Najpierw naciśnij mikrofon i powiedz polecenie.</div><div id="driverIqAction" class="driver-iq-action"></div></div>
          <div class="driver-small" style="margin-top:10px">Test: „Jaki jest następny adres?”, „Nawiguj do DXF”, „Ile mam czasu do pauzy?”, „Ile mogę jeszcze dzisiaj jechać?”.</div>
        </div>
      </div>
      <div id="driverMsgPane" class="driver-msg">
        <div class="card">
          <div class="driver-kicker">WIADOMOŚCI · TRANVIQ</div>
          <div class="driver-msg-compose">
            <select id="driverMsgRecipient">
              <option value="">Wybierz odbiorcę…</option>
              <option value="role:director">Dyrektor</option>
              <option value="role:dispatcher">Logistyk</option>
              <option value="vehicle:cbb121b6-34dd-41c6-974b-5b7aa3d9a1cb">Kierowca DX 9043F</option>
              <option value="vehicle:f016af91-dee6-4e72-9f86-4b2e27a253c1">Kierowca DX 5405A</option>
            </select>
            <textarea id="driverMsgText" rows="3" placeholder="Napisz wiadomość albo użyj mikrofonu…"></textarea>
            <div id="driverMsgHeard" class="driver-msg-heard"><div class="driver-msg-heard-label">USŁYSZAŁEM</div><div id="driverMsgTranscript">—</div></div>
            <div class="driver-msg-actions"><button id="driverMsgMic" class="driver-msg-voice" type="button">🎙 MÓW</button><button id="driverMsgClear" class="driver-msg-clear" type="button">✕ WYCZYŚĆ</button><button id="driverMsgSend" class="driver-msg-send" type="button">WYŚLIJ</button></div>
          </div>
          <div id="driverMsgStatus" class="driver-small" style="margin-top:8px"></div>
          <div id="driverMsgList" class="driver-msg-list"></div>
        </div>
      </div>
    </div>

    <script>
    (function(){
      const vehicleId = __VEHICLE_ID__;
      const live = document.getElementById('driverLive');
      const nextBox = document.getElementById('driverNext');
      const emptyBox = document.getElementById('driverEmpty');
      const nextAddress = document.getElementById('driverNextAddress');
      const nextWindow = document.getElementById('driverNextWindow');
      const routeKicker = document.getElementById('driverRouteKicker');
      const navigate = document.getElementById('driverNavigate');
      const complete = document.getElementById('driverComplete');
      const refusedButton = document.getElementById('driverRefused');
      const pendingButton = document.getElementById('driverPending');
      const statusActions = document.getElementById('driverStatusActions');
      const backActive = document.getElementById('driverBackActive');
      const stopsBox = document.getElementById('driverStops');
      const pwaUi = __PWA_UI__;
      const installCard = document.getElementById('driverInstallCard');
      const installButton = document.getElementById('driverInstallButton');
      const installTitle = document.getElementById('driverInstallTitle');
      const installNote = document.getElementById('driverInstallNote');
      let deferredInstallPrompt = null;
      const jobsBox = document.getElementById('driverJobs');
      const routeUi = __ROUTE_UI__;
      let savedRoute = null;
      let routeQueue = [];
      let previewRoute = null;
      let previewQueueIndex = null;
      let lastStamp = '';
      let busy = false;
      const ownVehicleId = vehicleId;
      const routePane=document.getElementById('driverRoutePane');
      const mapPane=document.getElementById('driverMapPane');
      const routeTab=document.getElementById('driverRouteTab');
      const mapTab=document.getElementById('driverMapTab');
      const iqTab=document.getElementById('driverIqTab');
      const iqPane=document.getElementById('driverIqPane');
      const tachoTab=document.getElementById('driverTachoTab');
      const tachoPane=document.getElementById('driverTachoPane');
      const msgTab=document.getElementById('driverMsgTab');
      const msgPane=document.getElementById('driverMsgPane');
      const msgRecipient=document.getElementById('driverMsgRecipient');
      const msgText=document.getElementById('driverMsgText');
      const msgMic=document.getElementById('driverMsgMic');
      const msgClear=document.getElementById('driverMsgClear');
      const msgHeard=document.getElementById('driverMsgHeard');
      const msgTranscript=document.getElementById('driverMsgTranscript');
      const msgSend=document.getElementById('driverMsgSend');
      const msgList=document.getElementById('driverMsgList');
      const msgStatus=document.getElementById('driverMsgStatus');
      const unreadBadge=document.getElementById('driverUnread');
      let messageCursor='';
      let knownMessageIds=new Set();
      let audioUnlocked=false;
      const tachoStatus=document.getElementById('driverTachoStatus');
      let latestTacho=null;

      function setupDriverInstall(){
        const standalone = window.matchMedia && window.matchMedia('(display-mode: standalone)').matches;
        const iosStandalone = window.navigator.standalone === true;
        if (standalone || iosStandalone) return;

        installTitle.textContent = pwaUi.title;
        installButton.textContent = pwaUi.install;
        installNote.textContent = pwaUi.browser_help;

        const ua = navigator.userAgent || '';
        const isIos = /iphone|ipad|ipod/i.test(ua);
        if (isIos) {
          installCard.style.display='block';
          installButton.textContent=pwaUi.how_to;
          installButton.addEventListener('click',function(){
            installNote.textContent=pwaUi.ios_help;
          });
        }

        window.addEventListener('beforeinstallprompt',function(event){
          event.preventDefault();
          deferredInstallPrompt=event;
          installCard.style.display='block';
          installButton.textContent=pwaUi.install;
          installNote.textContent=pwaUi.install_note;
        });

        installButton.addEventListener('click',async function(){
          if(!deferredInstallPrompt) return;
          const prompt=deferredInstallPrompt;
          deferredInstallPrompt=null;
          await prompt.prompt();
          try{await prompt.userChoice;}catch(e){}
        });

        window.addEventListener('appinstalled',function(){
          installCard.style.display='none';
          deferredInstallPrompt=null;
        });
      }
      setupDriverInstall();

      const micBtn=document.getElementById('driverMic');
      const transcriptBox=document.getElementById('driverTranscript');
      const iqResult=document.getElementById('driverIqResult');
      const iqAction=document.getElementById('driverIqAction');
      const mapNote=document.getElementById('driverMapNote');
      let fleetMap=null;
      let fleetMarkers={};
      function setActiveTab(which){[routeTab,mapTab,tachoTab,iqTab].forEach(function(x){x.classList.remove('active');});which.classList.add('active');}
      
      function hideDriverPanes(){
        routePane.style.display='none'; mapPane.style.display='none'; tachoPane.style.display='none'; iqPane.style.display='none'; msgPane.style.display='none';
        [routeTab,mapTab,tachoTab,iqTab,msgTab].forEach(function(x){if(x)x.classList.remove('active');});
      }
      function openMessagesTab(){hideDriverPanes();msgPane.style.display='block';msgTab.classList.add('active');loadMessages(true);}
      function beep(){
        try{const C=window.AudioContext||window.webkitAudioContext;if(!C)return;const c=new C();const o=c.createOscillator();const g=c.createGain();o.connect(g);g.connect(c.destination);o.frequency.value=880;g.gain.value=.06;o.start();o.stop(c.currentTime+.16);}catch(e){}
      }
      async function loadRecipients(){
        // The select already contains a server-rendered fallback list, so recipients
        // are visible even if the API is temporarily unavailable.
        try{
          const r=await fetch('/api/messages/recipients?ts='+Date.now(),{cache:'no-store'});
          const d=await r.json();
          if(!r.ok||!d.ok||!Array.isArray(d.recipients)||!d.recipients.length) return;
          const rows=d.recipients.filter(function(x){return x && x.id && x.id!=='vehicle:'+ownVehicleId;});
          if(!rows.length) return;
          msgRecipient.innerHTML='<option value="">Wybierz odbiorcę…</option>';
          rows.forEach(function(x){
            const o=document.createElement('option');o.value=x.id;
            o.textContent=(x.kind==='driver'?'Kierowca ':'')+(x.label||x.id);
            msgRecipient.appendChild(o);
          });
        }catch(e){
          // Keep the fallback list already rendered in HTML.
        }
      }
      function renderMessages(items){
        msgList.innerHTML='';(items||[]).forEach(function(m){const d=document.createElement('div');d.className='driver-msg-item '+(m.sender==='vehicle:'+ownVehicleId?'mine':'');const meta=document.createElement('div');meta.className='driver-msg-meta';meta.textContent=(m.sender_label||m.sender)+' · '+String(m.created_at||'').replace('T',' ').slice(0,16);const txt=document.createElement('div');txt.textContent=m.text||'';d.appendChild(meta);d.appendChild(txt);msgList.appendChild(d);});msgList.scrollTop=msgList.scrollHeight;
      }
      async function loadMessages(markRead){
        try{const r=await fetch('/api/messages',{cache:'no-store'});const d=await r.json();if(!r.ok||!d.ok)return;const items=d.messages||[];let fresh=[];items.forEach(function(m){if(!knownMessageIds.has(m.id)&&m.recipient==='vehicle:'+ownVehicleId)fresh.push(m);knownMessageIds.add(m.id);});renderMessages(items);if(fresh.length&&msgPane.style.display==='none')beep();const unread=items.filter(function(m){return m.recipient==='vehicle:'+ownVehicleId && !(m.read_by||[]).includes('vehicle:'+ownVehicleId);});unreadBadge.textContent=unread.length;unreadBadge.style.display=unread.length?'inline-flex':'none';if(markRead&&unread.length){await fetch('/api/messages/read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids:unread.map(x=>x.id)})});unreadBadge.style.display='none';}}
        catch(e){}
      }
      function startMessageVoice(){
        if(!msgRecipient.value){msgStatus.textContent='Najpierw wybierz odbiorcę.';return;}
        const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
        if(!SR){msgStatus.textContent='Ta przeglądarka nie obsługuje rozpoznawania mowy. Spróbuj w Chrome na telefonie.';return;}
        const rec=new SR();
        // For now use Ukrainian recognition for the SH test driver. Later this value
        // will come from the individual language setting of each user profile.
        rec.lang=__SPEECH_LANG__; rec.interimResults=true; rec.continuous=false;
        msgMic.classList.add('listening'); msgMic.textContent='🔴 SŁUCHAM…';
        msgHeard.classList.add('show'); msgTranscript.textContent='…'; msgStatus.textContent='Rozpoznaję wiadomość…';
        rec.onresult=function(e){
          let text='';
          for(let i=e.resultIndex;i<e.results.length;i++){text+=e.results[i][0].transcript;}
          text=String(text||'').trim();
          msgTranscript.textContent=text||'…';
          // Dictation never sends automatically. Put the transcript into the editable
          // text box so the driver can visually verify/correct it before WYŚLIJ.
          if(text) msgText.value=text;
          if(e.results[e.results.length-1].isFinal) msgStatus.textContent='Sprawdź tekst. Wiadomość nie została jeszcze wysłana.';
        };
        rec.onerror=function(e){msgStatus.textContent='Błąd mikrofonu/rozpoznawania: '+(e.error||'nieznany');};
        rec.onend=function(){msgMic.classList.remove('listening');msgMic.textContent='🎙 MÓW';};
        try{rec.start();}catch(e){msgStatus.textContent='Nie udało się uruchomić mikrofonu.';}
      }
      msgMic.addEventListener('click',startMessageVoice);
      msgClear.addEventListener('click',function(){msgText.value='';msgTranscript.textContent='—';msgHeard.classList.remove('show');msgStatus.textContent='';msgText.focus();});
      msgSend.addEventListener('click',async function(){const recipient=msgRecipient.value;const text=msgText.value.trim();if(!recipient||!text){msgStatus.textContent='Wybierz odbiorcę i wpisz wiadomość.';return;}msgSend.disabled=true;msgStatus.textContent='Wysyłanie…';try{const r=await fetch('/api/messages',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({recipient:recipient,text:text})});const d=await r.json();if(!r.ok||!d.ok)throw new Error('send');msgText.value='';msgStatus.textContent='Wysłano.';await loadMessages(false);}catch(e){msgStatus.textContent='Nie udało się wysłać wiadomości.';}finally{msgSend.disabled=false;}});
      msgTab.addEventListener('click',openMessagesTab);
      document.addEventListener('pointerdown',function(){audioUnlocked=true;},{once:true});
      loadRecipients();loadMessages(false);setInterval(function(){loadMessages(msgPane.style.display!=='none');},5000);
function openRouteTab(){routePane.style.display='block';mapPane.style.display='none';tachoPane.style.display='none';iqPane.style.display='none';setActiveTab(routeTab);}
      function openMapTab(){routePane.style.display='none';mapPane.style.display='block';tachoPane.style.display='none';iqPane.style.display='none';setActiveTab(mapTab);if(!fleetMap){
        fleetMap=L.map('driverFleetMap').setView([51.5,10.5],5);
        const streetLayer=L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; OpenStreetMap'});
        const satelliteLayer=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{maxZoom:19,attribution:'Tiles &copy; Esri'});
        const terrainLayer=L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',{maxZoom:17,attribution:'Map data &copy; OpenStreetMap contributors, SRTM | Map style &copy; OpenTopoMap'});
        const baseMaps={'Карта':streetLayer,'Супутник':satelliteLayer,'Рельєф':terrainLayer};
        let saved='Карта';try{saved=localStorage.getItem('oo_map_layer')||'Карта';}catch(e){}
        (baseMaps[saved]||streetLayer).addTo(fleetMap);
        const FleetMapTypeControl=L.Control.extend({
          options:{position:'topright'},
          onAdd:function(){
            const wrap=L.DomUtil.create('div','oo-map-type-control');
            wrap.innerHTML='<button type="button" class="oo-map-type-button" title="Вигляд карти">🗺️</button><div class="oo-map-type-menu" style="display:none"><button type="button" data-layer="Карта">Карта</button><button type="button" data-layer="Супутник">Супутник</button><button type="button" data-layer="Рельєф">Рельєф</button></div>';
            L.DomEvent.disableClickPropagation(wrap);L.DomEvent.disableScrollPropagation(wrap);
            const b=wrap.querySelector('.oo-map-type-button'),m=wrap.querySelector('.oo-map-type-menu');
            const lang=(document.documentElement.lang||'uk').toLowerCase().slice(0,2);
            const names={
              uk:{title:'Вигляд карти',items:['Карта','Супутник','Рельєф']},
              pl:{title:'Widok mapy',items:['Mapa','Satelita','Teren']},
              en:{title:'Map view',items:['Map','Satellite','Terrain']},
              de:{title:'Kartenansicht',items:['Karte','Satellit','Gelände']}
            }[lang]||{title:'Вигляд карти',items:['Карта','Супутник','Рельєф']};
            b.title=names.title;
            m.querySelectorAll('button[data-layer]').forEach(function(x,i){if(names.items[i])x.textContent=names.items[i];});
            wrap.style.position='relative';wrap.style.marginTop='42px';b.style.width='36px';b.style.height='36px';b.style.background='#fff';b.style.border='2px solid rgba(0,0,0,.2)';b.style.borderRadius='6px';b.style.cursor='pointer';m.style.position='absolute';m.style.right='0';m.style.top='40px';m.style.minWidth='112px';m.style.background='#fff';m.style.padding='5px';m.style.zIndex='1000';m.querySelectorAll('button').forEach(function(x){x.style.display='block';x.style.width='100%';x.style.border='0';x.style.background='#fff';x.style.padding='7px 10px';x.style.textAlign='left';x.style.cursor='pointer';});
            b.addEventListener('click',function(e){e.preventDefault();e.stopPropagation();m.style.display=m.style.display==='none'?'block':'none';});
            m.querySelectorAll('button[data-layer]').forEach(function(x){x.addEventListener('click',function(e){e.preventDefault();e.stopPropagation();const n=x.dataset.layer;Object.values(baseMaps).forEach(function(l){if(fleetMap.hasLayer(l))fleetMap.removeLayer(l);});(baseMaps[n]||streetLayer).addTo(fleetMap);try{localStorage.setItem('oo_map_layer',n);}catch(err){}m.style.display='none';});});
            fleetMap.on('click',function(){m.style.display='none';});
            return wrap;
          }
        });
        new FleetMapTypeControl().addTo(fleetMap);
        fleetMap.on('baselayerchange',function(e){
          try{localStorage.setItem('oo_map_layer',e.name);}catch(err){}
          const c=fleetMap.getContainer().querySelector('.leaflet-control-layers');
          if(c)c.classList.remove('leaflet-control-layers-expanded');
        });
      }setTimeout(function(){fleetMap.invalidateSize();loadFleet();},80);}
      function openTachoTab(){routePane.style.display='none';mapPane.style.display='none';tachoPane.style.display='block';iqPane.style.display='none';setActiveTab(tachoTab);loadTacho();}
      function openIqTab(){routePane.style.display='none';mapPane.style.display='none';tachoPane.style.display='none';iqPane.style.display='block';setActiveTab(iqTab);loadTacho();}
      routeTab.addEventListener('click',function(){history.replaceState(null,'',location.pathname);openRouteTab();});
      mapTab.addEventListener('click',function(){history.replaceState(null,'','#gps');openMapTab();});
      tachoTab.addEventListener('click',function(){history.replaceState(null,'','#tachograf');openTachoTab();});
      iqTab.addEventListener('click',function(){history.replaceState(null,'','#iq');openIqTab();});
      if(location.hash==='#gps'){openMapTab();}else if(location.hash==='#tachograf'){openTachoTab();}else if(location.hash==='#iq'){openIqTab();}
      window.addEventListener('hashchange',function(){if(location.hash==='#gps')openMapTab();else if(location.hash==='#tachograf')openTachoTab();else if(location.hash==='#iq')openIqTab();else openRouteTab();});
      function navToCoords(lat,lon){return 'https://www.google.com/maps/dir/?api=1&destination='+encodeURIComponent(lat+','+lon)+'&travelmode=driving';}
      async function loadFleet(){
        if(!fleetMap) return;
        try{
          const r=await fetch('/api/live-vehicle-states?driver_map='+Date.now(),{cache:'no-store'});if(!r.ok) throw new Error('HTTP '+r.status);
          const data=await r.json();const vehicles=Array.isArray(data.vehicles)?data.vehicles:[];const alive={};const bounds=[];
          vehicles.forEach(function(v){const lat=Number(v.latitude),lon=Number(v.longitude);if(!Number.isFinite(lat)||!Number.isFinite(lon))return;alive[v.id]=true;bounds.push([lat,lon]);const label=esc(v.plate||v.name||'Pojazd');const age=v.activity||'';const popup='<div class="driver-vehicle-card"><strong>'+label+'</strong><br>'+esc(age)+(v.id===ownVehicleId?'<br>Twój pojazd':'')+'<br><a class="driver-to-vehicle" target="_blank" rel="noopener" href="'+navToCoords(lat,lon)+'">🧭 NAWIGUJ DO POJAZDU</a></div>';if(!fleetMarkers[v.id]){fleetMarkers[v.id]=L.marker([lat,lon]).addTo(fleetMap);}else{fleetMarkers[v.id].setLatLng([lat,lon]);}fleetMarkers[v.id]._tranviqVehicle=String(v.name||v.plate||'').toUpperCase().includes('DXF')?'DXF':(String(v.name||v.plate||'').toUpperCase().includes('DXA')?'DXA':(String(v.name||v.plate||'').toUpperCase().includes('SH')?'SH':''));fleetMarkers[v.id].bindPopup(popup);});
          Object.keys(fleetMarkers).forEach(function(id){if(!alive[id]){fleetMap.removeLayer(fleetMarkers[id]);delete fleetMarkers[id];}});
          if(bounds.length && !fleetMap._driverFitted){fleetMap.fitBounds(bounds,{padding:[30,30],maxZoom:11});fleetMap._driverFitted=true;}
          const vis=await fetch('/api/driver-fleet-visibility?ts='+Date.now(),{cache:'no-store'}).then(x=>x.ok?x.json():null).catch(()=>null);
          mapNote.textContent=(vis&&vis.show_other_vehicles)?'Widzisz pojazdy firmy. Dotknij pojazdu i wybierz NAWIGUJ DO POJAZDU.':'Widoczność innych pojazdów jest wyłączona przez dyrektora.';
        }catch(e){mapNote.textContent='Nie udało się pobrać aktualnych pozycji GPS.';}
      }

      function esc(value){
        return String(value == null ? '' : value)
          .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
          .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
      }
      function stamp(route){
        if(!route) return '';
        try{return JSON.stringify({input_text:route.input_text||'',saved_at:route.saved_at||'',delivery_route:route.delivery_route||null});}
        catch(e){return String(Date.now());}
      }
      function googleMapsUrl(address){
        return 'https://www.google.com/maps/dir/?api=1&destination=' + encodeURIComponent(address) + '&travelmode=driving';
      }
      function currentIndex(stops){
        const idx = stops.findIndex(function(stop){
          const status=String(stop.manual_status||'').toLowerCase();
          return status!=='completed' && status!=='refused';
        });
        return idx < 0 ? -1 : idx;
      }
      function routeStops(item){
        const r=item&&item.delivery_route;
        return r&&Array.isArray(r.stops)?r.stops:[];
      }
      function routeSummary(item){
        const stops=routeStops(item);
        if(!stops.length) return routeUi.no_points;
        const first=stops[0]&&stops[0].address||'';
        const last=stops[stops.length-1]&&stops[stops.length-1].address||'';
        return first+(stops.length>1?' → '+last:'')+' · '+stops.length+' '+routeUi.points;
      }
      function routeNavAddress(item,isActive){
        const stops=routeStops(item);
        if(!stops.length) return '';
        if(isActive){
          const idx=currentIndex(stops);
          return (idx>=0?stops[idx]:stops[stops.length-1]).address||'';
        }
        return (stops[0]&&stops[0].address)||'';
      }
      function scrollRouteDetails(){
        window.setTimeout(function(){
          if(nextBox) nextBox.scrollIntoView({behavior:'smooth',block:'start'});
        },30);
      }
      function showActiveRoute(){
        previewQueueIndex=null;
        previewRoute=null;
        render();
        scrollRouteDetails();
      }
      function showQueuedRoute(index){
        const item=routeQueue[index]||null;
        if(!item) return;
        previewQueueIndex=index;
        previewRoute=item;
        renderPreview();
        scrollRouteDetails();
      }
      function jobActionsHtml(item,index,isActive){
        const address=routeNavAddress(item,isActive);
        const previewAttr=isActive?'data-active-preview="1"':'data-q="'+index+'"';
        const nav=address
          ? '<a class="driver-job-nav" target="_blank" rel="noopener" href="'+googleMapsUrl(address)+'">'+routeUi.navigate+'</a>'
          : '';
        return '<div class="driver-job-actions"><button class="driver-job-open" type="button" '+previewAttr+'>'+routeUi.preview+'</button>'+nav+'</div>';
      }
      function renderJobs(){
        if(!jobsBox) return;
        let html='';
        html+='<div class="driver-job"><div class="driver-job-title">'+routeUi.route+' 1 · '+routeUi.in_progress+'</div><div class="driver-job-meta">'+esc(savedRoute?routeSummary(savedRoute):routeUi.no_active)+'</div>';
        if(savedRoute) html+=jobActionsHtml(savedRoute,0,true);
        html+='</div>';

        routeQueue.forEach(function(item,i){
          html+='<div class="driver-job next"><div class="driver-job-title">'+routeUi.route+' '+(i+2)+' · '+routeUi.next+'</div><div class="driver-job-meta">'+esc(routeSummary(item))+'</div>'+jobActionsHtml(item,i,false)+'</div>';
        });

        jobsBox.innerHTML=html;
        jobsBox.querySelectorAll('[data-active-preview]').forEach(function(btn){
          btn.addEventListener('click',showActiveRoute);
        });
        jobsBox.querySelectorAll('[data-q]').forEach(function(btn){
          btn.addEventListener('click',function(){
            showQueuedRoute(Number(this.dataset.q));
          });
        });
      }
      function renderPreview(){
        if(previewQueueIndex!==null && routeQueue[previewQueueIndex]){
          previewRoute=routeQueue[previewQueueIndex];
        }
        if(!previewRoute){
          showActiveRoute();
          return;
        }
        const stops=routeStops(previewRoute);
        if(!stops.length) return;

        renderJobs();
        nextBox.style.display='block';
        emptyBox.style.display='none';
        routeKicker.textContent=routeUi.preview_next;
        nextAddress.textContent=stops[0].address||'';
        nextWindow.textContent=routeUi.route+' '+(previewQueueIndex===null?'':(previewQueueIndex+2))+' · '+stops.length+' '+routeUi.points;
        navigate.href=googleMapsUrl(stops[0].address||'');

        statusActions.style.display='none';
        complete.disabled=true;
        refusedButton.disabled=true;
        pendingButton.disabled=true;
        backActive.style.display='flex';

        stopsBox.innerHTML=stops.map(function(stop,i){
          const time=(stop.window_start&&stop.window_end)
            ? (routeUi.window+': '+stop.window_start+'–'+stop.window_end)
            : routeUi.no_window;
          const nav=stop.address
            ? '<a class="driver-stop-nav" target="_blank" rel="noopener" href="'+googleMapsUrl(stop.address)+'">'+routeUi.navigate+'</a>'
            : '';
          return '<div class="driver-stop"><div class="driver-num">'+(i+1)+'</div><div><strong>'+esc(stop.address||'')+'</strong><div class="driver-small">'+esc(time)+'</div>'+nav+'</div></div>';
        }).join('');
      }
      function render(){
        renderJobs();

        // Keep the driver's chosen next-route preview open even while the page
        // synchronizes every 5 seconds.
        if(previewQueueIndex!==null && routeQueue[previewQueueIndex]){
          previewRoute=routeQueue[previewQueueIndex];
          renderPreview();
          return;
        }
        if(previewQueueIndex!==null && !routeQueue[previewQueueIndex]){
          previewQueueIndex=null;
          previewRoute=null;
        }

        const route = savedRoute && savedRoute.delivery_route;
        const stops = route && Array.isArray(route.stops) ? route.stops : [];

        backActive.style.display='none';
        statusActions.style.display='grid';
        routeKicker.textContent=routeUi.next_point;
        complete.textContent=routeUi.status_unloaded;
        refusedButton.textContent=routeUi.status_refused;
        pendingButton.textContent=routeUi.status_pending;

        if(!stops.length){
          nextBox.style.display='none';
          emptyBox.style.display='block';
          stopsBox.innerHTML='';
          return;
        }

        emptyBox.style.display='none';
        const idx=currentIndex(stops);
        if(idx >= 0){
          const stop=stops[idx];
          nextBox.style.display='block';
          nextAddress.textContent=stop.address||'';
          nextWindow.textContent=(stop.window_start&&stop.window_end)
            ? (routeUi.window+': '+stop.window_start+'–'+stop.window_end)
            : routeUi.no_window;
          navigate.href=googleMapsUrl(stop.address||'');
          complete.disabled=false;
          refusedButton.disabled=false;
          pendingButton.disabled=false;
        }else{
          nextBox.style.display='block';
          nextAddress.textContent=routeUi.route_finished;
          nextWindow.textContent=routeUi.all_done;
          navigate.href='#';
          complete.disabled=true;
          refusedButton.disabled=true;
          pendingButton.disabled=true;
        }

        stopsBox.innerHTML=stops.map(function(stop,i){
          const manual=String(stop.manual_status||'').toLowerCase();
          const done=manual==='completed';
          const refused=manual==='refused';
          const pending=!done&&!refused;
          const current=i===idx;
          const cls=done?' completed':(refused?' refused':(current?' current':''));
          const time=(stop.window_start&&stop.window_end)?(stop.window_start+'–'+stop.window_end):routeUi.no_window;
          const statusText=done?routeUi.unloaded_label:(refused?routeUi.refused_label:routeUi.pending_label);
          const buttons='<div class="driver-stop-status">'+
            '<button type="button" class="driver-status-done '+(done?'active':'')+'" data-stop-status-index="'+i+'" data-stop-status="completed">'+routeUi.status_unloaded+'</button>'+
            '<button type="button" class="driver-status-refused '+(refused?'active':'')+'" data-stop-status-index="'+i+'" data-stop-status="refused">'+routeUi.status_refused+'</button>'+
            '<button type="button" class="driver-status-pending '+(pending?'active':'')+'" data-stop-status-index="'+i+'" data-stop-status="pending">'+routeUi.status_pending+'</button>'+
            '</div>';
          return '<div class="driver-stop'+cls+'"><div class="driver-num">'+(done?'✓':(refused?'!':(i+1)))+'</div><div><strong>'+esc(stop.address||'')+'</strong><div class="driver-small">'+esc(time)+(current?' · '+routeUi.next_point:'')+'</div><div class="driver-small"><strong>'+esc(statusText)+'</strong></div>'+buttons+'</div></div>';
        }).join('');
      }
      backActive.addEventListener('click',showActiveRoute);
      async function loadRoute(){
        if(busy) return; busy=true;
        try{
          const r=await fetch('/api/delivery-route/'+encodeURIComponent(vehicleId)+'?driver_sync='+Date.now(),{cache:'no-store'});
          if(!r.ok) throw new Error('HTTP '+r.status);
          const data=await r.json();
          let candidate=data.route||null;

          // The active-route object also carries route_queue. Read it here first
          // so the driver still sees the next job even if the dedicated queue
          // request is temporarily unavailable.
          routeQueue=(candidate && Array.isArray(candidate.route_queue))
            ? candidate.route_queue
            : [];

          // Fallback dla kierowcy: jeżeli bezpośredni odczyt dla pojazdu
          // nie zwrócił trasy, sprawdź wspólną listę aktywnych tras.
          // Chroni to przed starszymi zapisami, które mogły zostać
          // zapisane pod inną postacią identyfikatora pojazdu.
          if(!candidate){
            try{
              const allResponse=await fetch('/api/delivery-routes?driver_sync='+Date.now(),{cache:'no-store'});
              if(allResponse.ok){
                const allData=await allResponse.json();
                const routes=(allData&&allData.routes)||{};
                candidate=routes[vehicleId]||null;
                if(!candidate){
                  Object.keys(routes).some(function(key){
                    const item=routes[key];
                    if(item && String(item.vehicle_id||'')===String(vehicleId)){ candidate=item; return true; }
                    return false;
                  });
                }
              }
            }catch(ignore){}
          }

          if(candidate && Array.isArray(candidate.route_queue) && !routeQueue.length){
            routeQueue=candidate.route_queue;
          }

          try{
            const qr=await fetch('/api/delivery-route/'+encodeURIComponent(vehicleId)+'/queue?ts='+Date.now(),{cache:'no-store'});
            if(qr.ok){
              const qd=await qr.json();
              if(Array.isArray(qd.queue)) routeQueue=qd.queue;
            }
          }catch(ignore){}
          const s=stamp(candidate)+'|q:'+JSON.stringify(routeQueue.map(function(x){return x.saved_at||x.queued_at||'';}));
          if(s!==lastStamp){ savedRoute=candidate; lastStamp=s; render(); }
          live.textContent=candidate?'● online':'● online · brak trasy na serwerze';
          live.style.color=candidate?'#087f5b':'#b26a00';
        }catch(e){ live.textContent='● brak synchronizacji'; live.style.color='#c92a2a'; }
        finally{busy=false;}
      }
      function fmtSeconds(value){
        const n=Number(value); if(!Number.isFinite(n)||n<0) return 'Немає даних';
        const total=Math.round(n/60),h=Math.floor(total/60),m=total%60;
        return (h?h+' год. ':'')+m+' хв';
      }
      function setTachoText(id,value){const el=document.getElementById(id);if(el)el.textContent=value;}
      const confirmedTacho = {};
      let confirmedTachoAt = null;
      function keepConfirmed(target, source, key){
        const v=source ? source[key] : null;
        if(v!==null && v!==undefined && v!=='') target[key]=v;
      }
      async function loadTacho(){
        try{
          const r=await fetch('/api/driver-tachograph/'+encodeURIComponent(vehicleId)+'?ts='+Date.now(),{cache:'no-store'});
          if(!r.ok) throw new Error('HTTP '+r.status);
          const data=await r.json();
          const incoming=data.tachograph||{};
          [
            'time_until_break_s','remaining_daily_driving_s',
            'remaining_current_driving_s','time_until_daily_rest_s',
            'remaining_weekly_driving_s','driver_name',
            'working_state','time_state','updated_at','age_seconds'
          ].forEach(function(k){keepConfirmed(confirmedTacho,incoming,k);});
          // Card presence is sticky once positively confirmed. A transient false/null
          // packet must not make an inserted card blink off.
          if(incoming.card_present===true) confirmedTacho.card_present=true;
          if(Object.keys(confirmedTacho).length) confirmedTachoAt=new Date();

          latestTacho=Object.keys(confirmedTacho).length ? Object.assign({},confirmedTacho) : null;
          const t=latestTacho||{};
          setTachoText('tachoBreak',fmtSeconds(t.time_until_break_s));
          setTachoText('tachoDaily',fmtSeconds(t.remaining_daily_driving_s));
          setTachoText('tachoCurrent',fmtSeconds(t.remaining_current_driving_s));
          setTachoText('tachoRest',fmtSeconds(t.time_until_daily_rest_s));
          setTachoText('tachoWeekly',fmtSeconds(t.remaining_weekly_driving_s));
          setTachoText('tachoCard',t.card_present===true?'Карта вставлена':'Немає даних');

          let status=[];
          if(t.driver_name && t.driver_name!=='Водія не визначено') status.push('Водій: '+t.driver_name);
          if(confirmedTachoAt) status.push('останнє підтвердження '+confirmedTachoAt.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'}));
          tachoStatus.textContent=status.length?status.join(' · '):'Очікую підтверджені дані тахографа…';
        }catch(e){
          // Network/API refresh failure must never blank already confirmed values.
          if(latestTacho){
            tachoStatus.textContent=(latestTacho.driver_name&&latestTacho.driver_name!=='Водія не визначено'?'Водій: '+latestTacho.driver_name+' · ':'')+'показано останні підтверджені дані';
          }else{
            tachoStatus.textContent='Тимчасово немає зв’язку з тахографом.';
          }
        }
      }
      function iqTachoAnswer(kind){
        const t=latestTacho; if(!t){iqResult.textContent='Nie mam teraz aktualnych danych tachografu lub karty kierowcy. GPS pojazdu działa, ale bez danych tachografu nie podam dokładnego pozostałego czasu jazdy.';return;}
        if(kind==='break'){const v=fmtSeconds(t.time_until_break_s);iqResult.textContent=v==='Немає даних'?'Tachograf nie podał czasu do następnej przerwy.':'Do następnej wymaganej przerwy pozostało '+v+'.';return;}
        if(kind==='daily'){const v=fmtSeconds(t.remaining_daily_driving_s);iqResult.textContent=v==='Немає даних'?'Tachograf nie podał pozostałego dziennego czasu jazdy.':'Pozostały dzienny czas jazdy: '+v+'.';return;}
        if(kind==='rest'){const v=fmtSeconds(t.time_until_daily_rest_s);iqResult.textContent=v==='Немає даних'?'Tachograf nie podał czasu do odpoczynku dobowego.':'До добового відпочинку pozostało '+v+'.';return;}
        const vals=[]; if(fmtSeconds(t.remaining_daily_driving_s)!=='Немає даних') vals.push('jazda dzienna '+fmtSeconds(t.remaining_daily_driving_s)); if(fmtSeconds(t.time_until_break_s)!=='Немає даних') vals.push('do przerwy '+fmtSeconds(t.time_until_break_s)); if(fmtSeconds(t.time_until_daily_rest_s)!=='Немає даних') vals.push('do odpoczynku dobowego '+fmtSeconds(t.time_until_daily_rest_s));
        iqResult.textContent=vals.length?('Tachograf SH: '+vals.join(', ')+'.'):'Navirec nie podał jeszcze wartości czasu, które mogę bezpiecznie odczytać.';
      }
      function iqCurrentStop(){
        const route=savedRoute&&savedRoute.delivery_route; const stops=route&&Array.isArray(route.stops)?route.stops:[]; const idx=currentIndex(stops); return idx>=0?stops[idx]:null;
      }
      function iqStopPlan(unloading){
        const route=savedRoute&&savedRoute.delivery_route;
        const stops=route&&Array.isArray(route.stops)?route.stops:[];
        const start=currentIndex(stops);
        if(start<0) return null;
        let target=start;
        if(unloading){
          const next=stops.findIndex(function(s,i){
            return i>=start && String(s.stop_type||'').toLowerCase()==='unloading' &&
              !['completed','refused'].includes(String(s.manual_status||'').toLowerCase());
          });
          if(next>=0) target=next;
        }
        return {stop:stops[target],via:stops.slice(start,target)};
      }
      function iqCoordinates(latitude,longitude){
        // Number(null) and Number('') both equal zero. Neither means a GPS fix.
        if(latitude==null||longitude==null||String(latitude).trim()===''||String(longitude).trim()==='') return null;
        const lat=Number(latitude),lon=Number(longitude);
        if(!Number.isFinite(lat)||!Number.isFinite(lon)||Math.abs(lat)>90||Math.abs(lon)>180||(lat===0&&lon===0)) return null;
        return {latitude:lat,longitude:lon};
      }
      function iqFormatDriveTime(seconds){
        const n=Number(seconds); if(!Number.isFinite(n)||n<0) return 'Немає даних';
        const mins=Math.max(1,Math.round(n/60)),h=Math.floor(mins/60),m=mins%60;
        if(h&&m) return h+' godz. '+m+' min';
        if(h) return h+' godz.';
        return m+' min';
      }
      function iqFormatEta(seconds){
        const n=Number(seconds); if(!Number.isFinite(n)||n<0) return '';
        const d=new Date(Date.now()+n*1000);
        return d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
      }
      async function iqNextStopEstimate(unloading){
        const plan=iqStopPlan(Boolean(unloading));
        const stop=plan&&plan.stop;
        iqAction.innerHTML='';
        if(!stop||!stop.address){iqResult.textContent='Brak aktywnego następnego punktu trasy.';return;}
        iqResult.textContent='Sprawdzam aktualną pozycję pojazdu i trasę do następnego punktu…';
        try{
          const statesResp=await fetch('/api/driver-gps/'+encodeURIComponent(ownVehicleId)+'?iq_eta='+Date.now(),{cache:'no-store'});
          if(!statesResp.ok) throw new Error('gps');
          const own=await statesResp.json();
          const origin=iqCoordinates(own.latitude,own.longitude);
          if(!origin) throw new Error('gps');

          async function pointFor(routeStop){
            const saved=iqCoordinates(routeStop.latitude,routeStop.longitude);
            if(saved) return saved;
            const geoResp=await fetch('/api/geocode?mode=address&purpose=delivery&consent=addresses_only&lat='+encodeURIComponent(origin.latitude)+'&lon='+encodeURIComponent(origin.longitude)+'&q='+encodeURIComponent(routeStop.address),{cache:'no-store'});
            if(!geoResp.ok) throw new Error('geocode');
            const geo=await geoResp.json();
            const results=Array.isArray(geo.results)?geo.results:[];
            const resolved=results.length?iqCoordinates(results[0].latitude,results[0].longitude):null;
            if(!resolved) throw new Error('geocode');
            return resolved;
          }
          const waypoints=[];
          for(const viaStop of plan.via) waypoints.push(await pointFor(viaStop));
          const destination=await pointFor(stop);
          const routeResp=await fetch('/api/route',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({origin:origin,destination:destination,waypoints:waypoints,avoid_tolls:false})});
          const route=await routeResp.json();
          if(!routeResp.ok) throw new Error('route: '+(route.error||('HTTP '+routeResp.status)));
          if(route.distance_m==null||route.duration_s==null) throw new Error('route');
          const km=Number(route.distance_m)/1000,secs=Number(route.duration_s);
          if(!Number.isFinite(km)||km<0||!Number.isFinite(secs)||secs<0) throw new Error('route');
          const eta=iqFormatEta(secs);
          const windowText=(stop.window_start&&stop.window_end)?(' Okno punktu: '+stop.window_start+'–'+stop.window_end+'.'):'';
          iqResult.textContent=(unloading?'Następny rozładunek: ':'Następny punkt: ')+stop.address+'. Zostało '+km.toFixed(1)+' km, około '+iqFormatDriveTime(secs)+(eta?'. Przewidywany przyjazd: '+eta:'')+'.'+windowText;
          const a=document.createElement('a');a.href=googleMapsUrl(stop.address);a.target='_blank';a.rel='noopener';a.textContent='🧭 NAWIGUJ';iqAction.appendChild(a);
        }catch(e){
          const reason=String((e&&e.message)||e||'unknown');
          iqResult.textContent=reason==='gps'
            ? 'Nie ma aktualnych współrzędnych GPS tego pojazdu. Sprawdź pozycję i spróbuj ponownie.'
            : (reason==='geocode'
              ? 'Nie udało się ustalić współrzędnych punktu trasy. Sprawdź adres.'
              : 'Serwis tras nie obliczył drogi. Nie mogę podać dokładnych kilometrów ani czasu jazdy.');
          const a=document.createElement('a');a.href=googleMapsUrl(stop.address);a.target='_blank';a.rel='noopener';a.textContent='🧭 NAWIGUJ';iqAction.appendChild(a);
          console.error('TRANVIQ IQ ETA error:',e);
        }
      }
      function iqInterpret(text){
        const raw=String(text||''); const q=raw.toLowerCase(); iqAction.innerHTML='';
        const stop=iqCurrentStop();
        const has=function(parts){return parts.some(function(x){return q.includes(x);});};
        const vehicle=(q.match(/d\\s*x\\s*f|д\\s*х\\s*ф|dxef|deixef/i)?'DXF':(q.match(/d\\s*x\\s*a|д\\s*х\\s*а|dxa/i)?'DXA':(q.match(/s\\s*h|ш\\s*х|sh/i)?'SH':null)));
        const navWord=has(['навіг','навига','nawig','prowadź','веди','їхати до','їхать до','дорогу до']);
        const tachoWord=has(['тахо','tach','часу','час ','час?','їхати','ехать','jazd','пау','przerw','відпоч','odpocz']);
        const nextPointWord=has(['наступ','następ','вигруз','вигруж','вивантаж','вивантажк','розвантаж','розгруз','rozład','достав','punkt','точк']);
        const distanceTimeWord=has(['скільки','скiльки','ile','далеко','zosta','залиш','лишил','ще їх','ще їхати','час','czas','кілом','kilometr','км','godzin','хвилин','minut','коли буду','kiedy będę','доїх','dojad']);
        const asksNextEta=(nextPointWord&&distanceTimeWord) || (has(['скільки','ile','далеко','залиш','zosta'])&&has(['вигруз','вивантаж','розвантаж','rozład','точк','punkt']));
        if(asksNextEta){iqResult.textContent='IQ zrozumiał: odległość i czas do następnego punktu. Obliczam…';iqNextStopEstimate(has(['вигруз','вигруж','вивантаж','розвантаж','розгруз','rozład']));return;}
        if(vehicle && (navWord || has(['до '+vehicle.toLowerCase(),'do '+vehicle.toLowerCase()]))){
          iqResult.textContent='IQ zrozumiał: nawigować do pojazdu '+vehicle+'.';
          const target=Object.values(fleetMarkers).find(function(m){return m&&m._tranviqVehicle===vehicle;});
          if(target){const p=target.getLatLng();const a=document.createElement('a');a.href=navToCoords(p.lat,p.lng);a.target='_blank';a.rel='noopener';a.textContent='🧭 NAWIGUJ DO '+vehicle;iqAction.appendChild(a);}
          else{const b=document.createElement('button');b.type='button';b.textContent='POBIERZ GPS '+vehicle;b.onclick=async function(){await loadFleet();iqInterpret(raw);};iqAction.appendChild(b);iqResult.textContent+=' Pobiorę najnowszą pozycję GPS.';}
          return;
        }
        if(tachoWord && has(['пау','przerw'])){iqTachoAnswer('break');return;}
        if(tachoWord && has(['відпоч','odpocz'])){iqTachoAnswer('rest');return;}
        if(tachoWord && has(['скільки','ile','ще','jeszcze','можу','mogę','сьогодні','dzisiaj','зміні','zmian'])){iqTachoAnswer('daily');return;}
        if(has(['тахо','tachograf'])){iqTachoAnswer('all');return;}
        if((has(['наступн','następn'])) && has(['адрес','adres'])){iqResult.textContent=stop?('IQ zrozumiał: następny adres. '+(stop.address||'')):'Brak aktywnego następnego punktu.';return;}
        if(navWord && has(['наступ','następ'])){if(!stop){iqResult.textContent='Brak aktywnego następnego punktu.';return;}iqResult.textContent='IQ zrozumiał: nawigacja do następnego punktu.';const a=document.createElement('a');a.href=googleMapsUrl(stop.address||'');a.target='_blank';a.rel='noopener';a.textContent='🧭 NAWIGUJ';iqAction.appendChild(a);return;}
        if(has(['наступн','następn']) && has(['рейс','маршрут','tras'])){if(routeQueue.length){iqResult.textContent='IQ zrozumiał: pokaż następną trasę. '+routeSummary(routeQueue[0]);const b=document.createElement('button');b.type='button';b.textContent='POKAŻ TRASĘ 2';b.onclick=function(){previewRoute=routeQueue[0];openRouteTab();renderPreview();};iqAction.appendChild(b);}else iqResult.textContent='Nie ma jeszcze następnej trasy.';return;}
        iqResult.textContent='Nie mam pewności, jakie polecenie robocze miałeś na myśli. Niczego nie wykonałem. Spróbuj: „Nawiguj do DXF”, „Ile mam czasu do pauzy?” albo „Jaki jest następny adres?”.';
      }
      function startDriverVoice(){
        const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
        if(!SR){iqResult.textContent='Ta przeglądarka nie obsługuje rozpoznawania mowy. Spróbuj w Chrome na telefonie.';return;}
        const rec=new SR(); rec.lang='uk-UA'; rec.interimResults=true; rec.continuous=false;
        let fullText=''; let interpreted=false;
        micBtn.classList.add('listening'); micBtn.textContent='🔴 SŁUCHAM…'; transcriptBox.textContent='…'; iqResult.textContent='Rozpoznaję mowę…'; iqAction.innerHTML='';
        rec.onresult=function(e){
          let all='';
          for(let i=0;i<e.results.length;i++){all+=String(e.results[i][0].transcript||'')+' ';}
          fullText=all.trim();
          transcriptBox.textContent=fullText||'…';
          const last=e.results[e.results.length-1];
          if(last&&last.isFinal&&fullText){interpreted=true;iqResult.textContent='IQ analizuje polecenie…';iqInterpret(fullText);}
        };
        rec.onerror=function(e){iqResult.textContent='Błąd mikrofonu/rozpoznawania: '+(e.error||'nieznany');};
        rec.onend=function(){
          micBtn.classList.remove('listening');micBtn.textContent='🎙 NACIŚNIJ I MÓW';
          if(!interpreted&&fullText){interpreted=true;iqResult.textContent='IQ analizuje polecenie…';iqInterpret(fullText);}
          else if(!fullText&&iqResult.textContent==='Rozpoznaję mowę…'){iqResult.textContent='Nie usłyszałem polecenia. Spróbuj jeszcze raz.';}
        };
        try{rec.start();}catch(e){iqResult.textContent='Nie udało się uruchomić mikrofonu.';}
      }
      micBtn.addEventListener('click',startDriverVoice);

      async function setDriverStopStatus(index,status){
        if(!savedRoute || !savedRoute.delivery_route || !Array.isArray(savedRoute.delivery_route.stops)) return;
        if(!['completed','refused','pending'].includes(status)) return;
        const stops=savedRoute.delivery_route.stops;
        const stop=stops[index];
        if(!stop) return;
        const previous=stop.manual_status||'';
        stop.manual_status=status;
        savedRoute.saved_at=new Date().toISOString();
        statusActions.querySelectorAll('button').forEach(function(btn){btn.disabled=true;});
        try{
          const r=await fetch('/api/delivery-route/'+encodeURIComponent(vehicleId),{
            method:'PUT',headers:{'Content-Type':'application/json'},cache:'no-store',body:JSON.stringify({route:savedRoute})
          });
          if(!r.ok) throw new Error('HTTP '+r.status);
          lastStamp=stamp(savedRoute);
          render();
        }catch(e){
          stop.manual_status=previous;
          alert(routeUi.status_save_failed);
          render();
        }
      }
      function setCurrentDriverStatus(status){
        if(!savedRoute || !savedRoute.delivery_route || !Array.isArray(savedRoute.delivery_route.stops)) return;
        const idx=currentIndex(savedRoute.delivery_route.stops);
        if(idx<0) return;
        setDriverStopStatus(idx,status);
      }
      complete.addEventListener('click',function(){setCurrentDriverStatus('completed');});
      refusedButton.addEventListener('click',function(){setCurrentDriverStatus('refused');});
      pendingButton.addEventListener('click',function(){setCurrentDriverStatus('pending');});
      stopsBox.addEventListener('click',function(event){
        const button=event.target.closest('[data-stop-status-index]');
        if(!button || !stopsBox.contains(button)) return;
        const index=Number(button.dataset.stopStatusIndex);
        const status=button.dataset.stopStatus;
        if(!Number.isInteger(index)) return;
        setDriverStopStatus(index,status);
      });
      loadRoute();
      window.setInterval(loadRoute,5000);
      window.setInterval(function(){if(mapPane.style.display!=='none')loadFleet();if(tachoPane.style.display!=='none'||iqPane.style.display!=='none')loadTacho();},10000);
    })();
    </script>
    """.replace("__VEHICLE_ID__", json.dumps(vehicle_id)).replace("__PLATE__", escape(vehicle_plate)).replace("__PLATE_JSON__", json.dumps(vehicle_plate))

    # Driver UI has its own complete language layer because this page contains
    # both visible HTML and runtime JavaScript messages. Never mix languages.
    driver_lang = current_language()
    speech_lang = {"uk": "uk-UA", "pl": "pl-PL", "en": "en-US", "de": "de-DE"}.get(driver_lang, "uk-UA")
    body = body.replace("__SPEECH_LANG__", json.dumps(speech_lang))

    route_ui_by_lang = {
        "uk": {
            "route": "РЕЙС",
            "in_progress": "В РОБОТІ",
            "next": "НАСТУПНИЙ",
            "preview": "ПЕРЕГЛЯД РЕЙСУ",
            "navigate": "🧭 НАВІГУВАТИ",
            "no_active": "Немає активного рейсу",
            "no_points": "Немає точок",
            "points": "точок",
            "preview_next": "ПЕРЕГЛЯД НАСТУПНОГО РЕЙСУ",
            "next_point": "НАСТУПНА ТОЧКА",
            "window": "Часове вікно",
            "no_window": "без часового вікна",
            "route_finished": "Рейс завершено",
            "all_done": "Усі точки виконано",
            "status_unloaded": "✓ РОЗВАНТАЖЕНО",
            "status_refused": "⚠ НЕ ПРИЙНЯЛИ · ТОВАР У МАШИНІ",
            "status_pending": "✕ НЕ РОЗВАНТАЖЕНО",
            "unloaded_label": "Розвантажено",
            "refused_label": "Не прийняли — товар залишився в машині",
            "pending_label": "Не розвантажено",
            "status_save_failed": "Не вдалося зберегти статус точки. Спробуй ще раз."
        },
        "pl": {
            "route": "TRASA",
            "in_progress": "W TRAKCIE",
            "next": "NASTĘPNA",
            "preview": "PODGLĄD TRASY",
            "navigate": "🧭 NAWIGUJ",
            "no_active": "Brak aktywnej trasy",
            "no_points": "Brak punktów",
            "points": "pkt.",
            "preview_next": "PODGLĄD NASTĘPNEJ TRASY",
            "next_point": "NASTĘPNY PUNKT",
            "window": "Okno",
            "no_window": "bez okna czasowego",
            "route_finished": "Trasa zakończona",
            "all_done": "Wszystkie punkty wykonane",
            "status_unloaded": "✓ ROZŁADOWANO",
            "status_refused": "⚠ NIE PRZYJĘTO · TOWAR W AUCIE",
            "status_pending": "✕ NIE ROZŁADOWANO",
            "unloaded_label": "Rozładowano",
            "refused_label": "Nie przyjęto — towar został w pojeździe",
            "pending_label": "Nie rozładowano",
            "status_save_failed": "Nie udało się zapisać statusu punktu. Spróbuj ponownie."
        },
        "en": {
            "route": "ROUTE",
            "in_progress": "IN PROGRESS",
            "next": "NEXT",
            "preview": "VIEW ROUTE",
            "navigate": "🧭 NAVIGATE",
            "no_active": "No active route",
            "no_points": "No stops",
            "points": "stops",
            "preview_next": "NEXT ROUTE PREVIEW",
            "next_point": "NEXT STOP",
            "window": "Time window",
            "no_window": "no time window",
            "route_finished": "Route completed",
            "all_done": "All stops completed",
            "status_unloaded": "✓ UNLOADED",
            "status_refused": "⚠ NOT ACCEPTED · CARGO ON BOARD",
            "status_pending": "✕ NOT UNLOADED",
            "unloaded_label": "Unloaded",
            "refused_label": "Not accepted — cargo remains in vehicle",
            "pending_label": "Not unloaded",
            "status_save_failed": "Could not save the stop status. Try again."
        },
        "de": {
            "route": "ROUTE",
            "in_progress": "AKTIV",
            "next": "NÄCHSTE",
            "preview": "ROUTE ANSEHEN",
            "navigate": "🧭 NAVIGIEREN",
            "no_active": "Keine aktive Route",
            "no_points": "Keine Stopps",
            "points": "Stopps",
            "preview_next": "VORSCHAU NÄCHSTE ROUTE",
            "next_point": "NÄCHSTER STOPP",
            "window": "Zeitfenster",
            "no_window": "kein Zeitfenster",
            "route_finished": "Route beendet",
            "all_done": "Alle Stopps erledigt",
            "status_unloaded": "✓ ENTLADEN",
            "status_refused": "⚠ NICHT ANGENOMMEN · WARE IM FAHRZEUG",
            "status_pending": "✕ NICHT ENTLADEN",
            "unloaded_label": "Entladen",
            "refused_label": "Nicht angenommen — Ware bleibt im Fahrzeug",
            "pending_label": "Nicht entladen",
            "status_save_failed": "Der Status konnte nicht gespeichert werden. Bitte erneut versuchen."
        }
    }
    route_ui = route_ui_by_lang.get(driver_lang, route_ui_by_lang["uk"])
    body = body.replace("__ROUTE_UI__", json.dumps(route_ui, ensure_ascii=False))
    driver_ui = {
        "uk": {
            "KIEROWCA · TRASA NA ŻYWO": "ВОДІЙ · МАРШРУТ НАЖИВО",
            "● synchronizacja": "● синхронізація",
            "Trasa wspólna z dyrektorem i logistykiem. Zmiany pojawią się automatycznie.": "Спільний маршрут із директором і логістом. Зміни з’являються автоматично.",
            "🗺️ TRASA": "🗺️ МАРШРУТ", "⏱️ TACHOGRAF": "⏱️ ТАХОГРАФ", "💬 WIADOMOŚCI": "💬 ПОВІДОМЛЕННЯ",
            "TWOJE ZLECENIA": "ТВОЇ РЕЙСИ", "📍 MAPA GPS POJAZDÓW": "📍 GPS-КАРТА МАШИН",
            "NASTĘPNY PUNKT": "НАСТУПНА ТОЧКА", "🧭 NAWIGUJ": "🧭 НАВІГУВАТИ", "✓ ZAKOŃCZONO": "✓ ВИКОНАНО",
            "↩ AKTUALNA TRASA": "↩ АКТУАЛЬНИЙ РЕЙС",
            "📷 SKANUJ DOKUMENT": "📷 СКАНУВАТИ ДОКУМЕНТ",
            "CMR · Lieferschein · paragon paliwowy · inny dokument": "CMR · Lieferschein · паливний чек · інший документ",
            "Czekam na aktywną trasę dla": "Очікую активний маршрут для", "Ładowanie pozycji GPS…": "Завантажую GPS-позиції…",
            "ТАХОГРАФ · SH 9203G": "ТАХОГРАФ · SH 9203G", "Отримання даних тахографа…": "Отримую дані тахографа…",
            "До наступної перерви": "До наступної перерви", "Залишок денного часу керування": "Денне водіння — залишилось",
            "Залишок поточного періоду керування": "Поточний період водіння — залишилось", "До добового відпочинку": "До добового відпочинку",
            "Залишок тижневого часу керування": "Тижневе водіння — залишилось", "Карта водія": "Картка водія",
            "Показуються останні підтверджені дані Navirec. Тимчасово порожній пакет не стирає попередні значення.": "IQ показує лише підтверджені дані Navirec. Відсутні значення не вгадуються.",
            "IQ · ASYSTENT GŁOSOWY": "IQ · ГОЛОСОВИЙ ПОМІЧНИК", "🎙 NACIŚNIJ I MÓW": "🎙 НАТИСНИ І ГОВОРИ", "🔴 SŁUCHAM…": "🔴 СЛУХАЮ…",
            "USŁYSZAŁEM": "Я ПОЧУВ", "IQ ZROZUMIAŁ": "IQ ЗРОЗУМІВ", "Najpierw naciśnij mikrofon i powiedz polecenie.": "Натисни мікрофон і скажи команду.",
            "Test:": "Тест:", "WIADOMOŚCI · TRANVIQ": "ПОВІДОМЛЕННЯ · TRANVIQ", "Wybierz odbiorcę…": "Вибери одержувача…",
            "Dyrektor": "Директор", "Logistyk": "Логіст", "Kierowca DX": "Водій DX", "Napisz wiadomość albo użyj mikrofonu…": "Напиши повідомлення або скористайся мікрофоном…",
            "🎙 MÓW": "🎙 ГОВОРИТИ", "✕ WYCZYŚĆ": "✕ ОЧИСТИТИ", "WYŚLIJ": "ВІДПРАВИТИ",
            "Najpierw wybierz odbiorcę.": "Спочатку вибери одержувача.", "Rozpoznaję wiadomość…": "Розпізнаю повідомлення…",
            "Sprawdź tekst. Wiadomość nie została jeszcze wysłana.": "Перевір текст. Повідомлення ще не відправлено.",
            "Wybierz odbiorcę i wpisz wiadomość.": "Вибери одержувача і введи повідомлення.", "Wysyłanie…": "Відправляю…", "Wysłano.": "Відправлено.",
            "Nie udało się wysłać wiadomości.": "Не вдалося відправити повідомлення.", "Rozpoznaję mowę…": "Розпізнаю мову…",
            "IQ analizuje polecenie…": "IQ аналізує команду…", "Nie usłyszałem polecenia. Spróbuj jeszcze raz.": "Я не почув команди. Спробуй ще раз.",
            "Nie udało się uruchomić mikrofonu.": "Не вдалося запустити мікрофон.", "Błąd mikrofonu/rozpoznawania:": "Помилка мікрофона/розпізнавання:",
            "Ta przeglądarka nie obsługuje rozpoznawania mowy. Spróbuj w Chrome na telefonie.": "Цей браузер не підтримує розпізнавання мови. Спробуй Chrome на телефоні.",
            "IQ zrozumiał: odległość i czas do następnego punktu. Obliczam…": "IQ зрозумів: відстань і час до наступної точки. Розраховую…",
            "Sprawdzam aktualną pozycję pojazdu i trasę do następnego punktu…": "Перевіряю актуальну GPS-позицію машини та маршрут до наступної точки…",
            "Brak aktywnego następnego punktu trasy.": "Немає активної наступної точки маршруту.",
            "Nie udało się teraz policzyć drogi do następnego punktu z aktualnej pozycji GPS. Spróbuj ponownie za chwilę.": "Зараз не вдалося розрахувати дорогу до наступної точки з актуальної GPS-позиції. Спробуй ще раз за хвилину.",
            "Nie ma aktualnych współrzędnych GPS tego pojazdu. Sprawdź pozycję i spróbuj ponownie.": "Немає актуальних GPS-координат цієї машини. Перевір позицію та спробуй ще раз.",
            "Nie udało się ustalić współrzędnych punktu trasy. Sprawdź adres.": "Не вдалося визначити координати точки маршруту. Перевір адресу.",
            "Serwis tras chwilowo nie obliczył drogi. Możesz otworzyć nawigację do punktu.": "Сервіс маршрутів зараз не розрахував дорогу. Можеш відкрити навігацію до точки.",
            "Serwis tras nie obliczył drogi. Nie mogę podać dokładnych kilometrów ani czasu jazdy.": "Сервіс маршрутів не розрахував дорогу. Не можу назвати точні кілометри й час їзди.",
            "Następny rozładunek:": "Наступна вигрузка:",
            "Następny punkt:": "Наступна точка:", "Zostało": "Залишилось", "około": "приблизно", "Przewidywany przyjazd:": "Орієнтовне прибуття:", "Okno punktu:": "Часове вікно:",
            "godz.": "год", "min": "хв", "Немає даних": "немає даних", "Brak aktywnego następnego punktu.": "Немає активної наступної точки.",
            "IQ zrozumiał: następny adres.": "IQ зрозумів: наступна адреса.", "IQ zrozumiał: nawigacja do następnego punktu.": "IQ зрозумів: навігація до наступної точки.",
            "Nie ma jeszcze następnej trasy.": "Наступного рейсу ще немає.", "POKAŻ TRASĘ 2": "ПОКАЗАТИ РЕЙС 2", "POBIERZ GPS": "ОНОВИТИ GPS",
            "Pobiorę najnowszą pozycję GPS.": "Отримаю найсвіжішу GPS-позицію.", "IQ zrozumiał: nawigować do pojazdu": "IQ зрозумів: навігувати до машини",
            "Nie mam pewności, jakie polecenie robocze miałeś na myśli. Niczego nie wykonałem.": "Не впевнений, яку робочу команду ти мав на увазі. Нічого не виконано.",
            "Spróbuj:": "Спробуй:", "Zapisywanie…": "Зберігаю…", "Nie udało się zapisać wykonania punktu. Spróbuj ponownie.": "Не вдалося зберегти виконання точки. Спробуй ще раз."
        },
        "en": {"KIEROWCA · TRASA NA ŻYWO":"DRIVER · LIVE ROUTE","🗺️ TRASA":"🗺️ ROUTE","⏱️ TACHOGRAF":"⏱️ TACHOGRAPH","💬 WIADOMOŚCI":"💬 MESSAGES","🎙 NACIŚNIJ I MÓW":"🎙 TAP AND SPEAK","USŁYSZAŁEM":"I HEARD","IQ ZROZUMIAŁ":"IQ UNDERSTOOD","🧭 NAWIGUJ":"🧭 NAVIGATE"},
        "de": {"KIEROWCA · TRASA NA ŻYWO":"FAHRER · LIVE-ROUTE","🗺️ TRASA":"🗺️ ROUTE","⏱️ TACHOGRAF":"⏱️ TACHOGRAF","💬 WIADOMOŚCI":"💬 NACHRICHTEN","🎙 NACIŚNIJ I MÓW":"🎙 DRÜCKEN UND SPRECHEN","USŁYSZAŁEM":"ICH HABE GEHÖRT","IQ ZROZUMIAŁ":"IQ HAT VERSTANDEN","🧭 NAWIGUJ":"🧭 NAVIGIEREN"}
    }.get(driver_lang, {})
    for source, target in sorted(driver_ui.items(), key=lambda item: len(item[0]), reverse=True):
        body = body.replace(source, target)

    pwa_ui_by_lang = {
        "uk": {
            "title": "TRANVIQ Driver на телефон",
            "install": "📲 ВСТАНОВИТИ ДОДАТОК",
            "how_to": "📲 ЯК ВСТАНОВИТИ",
            "install_note": "Встановиться як окремий додаток. Повторно шукати TRANVIQ через Google не потрібно.",
            "browser_help": "У Chrome можна встановити TRANVIQ Driver на головний екран.",
            "ios_help": "На iPhone відкрий у Safari: Поділитися → На початковий екран."
        },
        "pl": {
            "title": "TRANVIQ Driver na telefon",
            "install": "📲 ZAINSTALUJ APLIKACJĘ",
            "how_to": "📲 JAK ZAINSTALOWAĆ",
            "install_note": "Aplikacja pojawi się jako osobna ikona. Nie trzeba za każdym razem szukać TRANVIQ w Google.",
            "browser_help": "W Chrome możesz zainstalować TRANVIQ Driver na ekranie głównym.",
            "ios_help": "Na iPhone otwórz w Safari: Udostępnij → Do ekranu początkowego."
        },
        "en": {
            "title": "TRANVIQ Driver on your phone",
            "install": "📲 INSTALL APP",
            "how_to": "📲 HOW TO INSTALL",
            "install_note": "It installs as a separate app icon. No need to find TRANVIQ through Google each time.",
            "browser_help": "In Chrome you can install TRANVIQ Driver on the home screen.",
            "ios_help": "On iPhone open in Safari: Share → Add to Home Screen."
        },
        "de": {
            "title": "TRANVIQ Driver auf dem Handy",
            "install": "📲 APP INSTALLIEREN",
            "how_to": "📲 INSTALLATION",
            "install_note": "Die App erscheint als eigenes Symbol. TRANVIQ muss nicht jedes Mal über Google gesucht werden.",
            "browser_help": "In Chrome kann TRANVIQ Driver zum Startbildschirm hinzugefügt werden.",
            "ios_help": "Auf dem iPhone in Safari: Teilen → Zum Home-Bildschirm."
        }
    }
    body = body.replace(
        "__PWA_UI__",
        json.dumps(pwa_ui_by_lang.get(driver_lang, pwa_ui_by_lang["uk"]), ensure_ascii=False)
    )

    driver_title = {"uk": "Водій", "pl": "Kierowca", "en": "Driver", "de": "Fahrer"}.get(driver_lang, "Водій")
    return page(
        driver_title + " · " + vehicle_plate,
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

    lang = current_language()
    vehicle_labels = {
        "uk": {"speed": "Швидкість", "fuel": "Паливо", "heading": "Напрямок", "engine": "Оберти двигуна", "distance": "Загальна відстань", "ignition": "Запалювання", "history": "Історія маршруту"},
        "pl": {"speed": "Prędkość", "fuel": "Paliwo", "heading": "Kierunek", "engine": "Obroty silnika", "distance": "Całkowity przebieg", "ignition": "Zapłon", "history": "Historia trasy"},
        "en": {"speed": "Speed", "fuel": "Fuel", "heading": "Heading", "engine": "Engine RPM", "distance": "Total distance", "ignition": "Ignition", "history": "Route history"},
        "de": {"speed": "Geschwindigkeit", "fuel": "Kraftstoff", "heading": "Fahrtrichtung", "engine": "Motordrehzahl", "distance": "Gesamtstrecke", "ignition": "Zündung", "history": "Routenverlauf"},
    }.get(lang, {})

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

            status_text = "Є позиція GPS" if tenancy.company() else "Є дані Navirec"
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

    lang = current_language()
    vehicle_labels = {
        "uk": {"speed": "Швидкість", "fuel": "Паливо", "heading": "Напрямок", "engine": "Оберти двигуна", "distance": "Загальна відстань", "ignition": "Запалювання", "history": "Історія маршруту"},
        "pl": {"speed": "Prędkość", "fuel": "Paliwo", "heading": "Kierunek", "engine": "Obroty silnika", "distance": "Całkowity przebieg", "ignition": "Zapłon", "history": "Historia trasy"},
        "en": {"speed": "Speed", "fuel": "Fuel", "heading": "Heading", "engine": "Engine RPM", "distance": "Total distance", "ignition": "Ignition", "history": "Route history"},
        "de": {"speed": "Geschwindigkeit", "fuel": "Kraftstoff", "heading": "Fahrtrichtung", "engine": "Motordrehzahl", "distance": "Gesamtstrecke", "ignition": "Zündung", "history": "Routenverlauf"},
    }.get(lang, {})

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
    if tenancy.company(): return company_vehicles()
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

    lang = current_language()
    vehicle_labels = {
        "uk": {"speed": "Швидкість", "fuel": "Паливо", "heading": "Напрямок", "engine": "Оберти двигуна", "distance": "Загальна відстань", "ignition": "Запалювання", "history": "Історія маршруту"},
        "pl": {"speed": "Prędkość", "fuel": "Paliwo", "heading": "Kierunek", "engine": "Obroty silnika", "distance": "Całkowity przebieg", "ignition": "Zapłon", "history": "Historia trasy"},
        "en": {"speed": "Speed", "fuel": "Fuel", "heading": "Heading", "engine": "Engine RPM", "distance": "Total distance", "ignition": "Ignition", "history": "Route history"},
        "de": {"speed": "Geschwindigkeit", "fuel": "Kraftstoff", "heading": "Fahrtrichtung", "engine": "Motordrehzahl", "distance": "Gesamtstrecke", "ignition": "Zündung", "history": "Routenverlauf"},
    }.get(lang, {})

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

        lang = current_language()

        if speed is not None:
            speed_unit = "km/h" if lang in {"en", "de", "pl"} else "км/год"
            speed_text = format_number(speed, 0) + " " + speed_unit
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
            engine_unit = "rpm" if lang in {"en", "de", "pl"} else "об/хв"
            engine_text = format_number(engine_speed, 0) + " " + engine_unit
        else:
            engine_text = "—"

        if total_distance is not None:
            # Navirec total_distance is returned in metres; display kilometres.
            distance_text = format_number(total_distance / 1000.0, 1) + " km"
        else:
            distance_text = "—"

        if ignition is not None:
            if lang == "en":
                ignition_text = "On" if bool(ignition) else "Off"
            elif lang == "pl":
                ignition_text = "Włączony" if bool(ignition) else "Wyłączony"
            elif lang == "de":
                ignition_text = "Ein" if bool(ignition) else "Aus"
            else:
                ignition_text = "Увімкнено" if bool(ignition) else "Вимкнено"
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

    lang = current_language()
    vehicle_labels = {
        "uk": {"speed": "Швидкість", "fuel": "Паливо", "heading": "Напрямок", "engine": "Оберти двигуна", "distance": "Загальна відстань", "ignition": "Запалювання", "history": "Історія маршруту"},
        "pl": {"speed": "Prędkość", "fuel": "Paliwo", "heading": "Kierunek", "engine": "Obroty silnika", "distance": "Całkowity przebieg", "ignition": "Zapłon", "history": "Historia trasy"},
        "en": {"speed": "Speed", "fuel": "Fuel", "heading": "Heading", "engine": "Engine RPM", "distance": "Total distance", "ignition": "Ignition", "history": "Route history"},
        "de": {"speed": "Geschwindigkeit", "fuel": "Kraftstoff", "heading": "Fahrtrichtung", "engine": "Motordrehzahl", "distance": "Gesamtstrecke", "ignition": "Zündung", "history": "Routenverlauf"},
    }.get(lang, {})

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
            <div class="label">{label_speed}</div>
            <div class="value">{speed}</div>
        </div>

        <div class="stat">
            <div class="label">{label_fuel}</div>
            <div class="value">{fuel}</div>
        </div>

        <div class="stat">
            <div class="label">{label_heading}</div>
            <div class="value">{heading}</div>
        </div>

        <div class="stat">
            <div class="label">{label_engine}</div>
            <div class="value">{engine}</div>
        </div>

        <div class="stat">
            <div class="label">{label_distance}</div>
            <div class="value">{distance}</div>
        </div>

        <div class="stat">
            <div class="label">{label_ignition}</div>
            <div class="value">{ignition}</div>
        </div>

    </div>

    {map_block}

    <div class="card">

        <a
            class="button"
            href="/history?vehicle={id}"
        >
            {label_history}
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
        label_speed=vehicle_labels.get("speed", "Speed"),
        label_fuel=vehicle_labels.get("fuel", "Fuel"),
        label_heading=vehicle_labels.get("heading", "Heading"),
        label_engine=vehicle_labels.get("engine", "Engine RPM"),
        label_distance=vehicle_labels.get("distance", "Total distance"),
        label_ignition=vehicle_labels.get("ignition", "Ignition"),
        label_history=vehicle_labels.get("history", "Route history"),
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
    search_mode = request.args.get("mode", "address").strip().lower()
    selected_city = request.args.get("city", "").strip()[:100]
    delivery_consent = (
        request.args.get("purpose") == "delivery"
        and request.args.get("consent") == "addresses_only"
    )

    if len(query) < 3:
        return jsonify({"results": []})

    if search_mode not in {"city", "address"}:
        search_mode = "address"

    query = query[:180]

    # Dla adresów budujemy kilka bezpiecznych wariantów wyszukiwania.
    # Oryginał zawsze jest pierwszy; kolejne warianty pomagają geokoderom
    # z niemieckimi znakami i skrótami spotykanymi na listach dostaw.
    address_queries = [query]
    if search_mode == "address":
        replacements = (
            ("ß", "ss"), ("ẞ", "SS"),
            ("ä", "ae"), ("ö", "oe"), ("ü", "ue"),
            ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue"),
        )
        ascii_query = query
        for old, new in replacements:
            ascii_query = ascii_query.replace(old, new)
        if ascii_query.casefold() != query.casefold():
            address_queries.append(ascii_query)

        street_query = query
        street_query = re.sub(r"\bstr\.(?=\s|$)", "straße", street_query, flags=re.I)
        street_query = re.sub(r"\bstrasse\b", "straße", street_query, flags=re.I)
        if street_query.casefold() not in {q.casefold() for q in address_queries}:
            address_queries.append(street_query)

        # Geokodery czasem lepiej rozpoznają kod pocztowy + miasto bez
        # dopisku dzielnicy w nawiasie, np. Verden (Aller) -> Verden.
        simple_query = re.sub(r"\s*\([^)]{2,40}\)", "", query)
        simple_query = re.sub(r"\s+", " ", simple_query).strip()
        if simple_query.casefold() not in {q.casefold() for q in address_queries}:
            address_queries.append(simple_query)

    try:
        bias_latitude = float(request.args.get("lat", ""))
        bias_longitude = float(request.args.get("lon", ""))
    except (TypeError, ValueError):
        bias_latitude = None
        bias_longitude = None

    cache_key = (
        search_mode,
        query.casefold(),
        selected_city.casefold(),
        delivery_consent,
        round(bias_latitude, 3) if bias_latitude is not None else None,
        round(bias_longitude, 3) if bias_longitude is not None else None
    )
    now = time.monotonic()
    cached = GEOCODE_CACHE.get(cache_key)

    if cached and now - cached[0] < GEOCODE_CACHE_TTL:
        return jsonify({"results": cached[1]})

    # Для адрес розвізки Google Routes має пріоритет над Photon.
    # Photon часто повертає центр вулиці/населеного пункту, навіть коли
    # номер приватного будинку в запиті правильний. Routes натомість
    # повертає кінцеву точку автомобільного під'їзду до заданої адреси.
    if (
        search_mode == "address"
        and delivery_consent
        and GOOGLE_MAPS_API_KEY
    ):
        try:
            google_response = requests.post(
                "https://routes.googleapis.com/directions/v2:computeRoutes",
                headers={
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
                    "X-Goog-FieldMask": "routes.legs.endLocation"
                },
                json={
                    "origin": {
                        "location": {
                            "latLng": {
                                "latitude": bias_latitude or 52.5,
                                "longitude": bias_longitude or 10.0
                            }
                        }
                    },
                    "destination": {"address": query},
                    "travelMode": "DRIVE",
                    "languageCode": current_language(),
                    "units": "METRIC"
                },
                timeout=20
            )
            google_response.raise_for_status()
            google_routes = google_response.json().get("routes") or []
            if google_routes:
                google_legs = google_routes[0].get("legs") or []
                if google_legs:
                    end_location = (
                        google_legs[-1].get("endLocation", {})
                        .get("latLng", {})
                    )
                    try:
                        google_latitude = float(
                            end_location.get("latitude")
                        )
                        google_longitude = float(
                            end_location.get("longitude")
                        )
                    except (TypeError, ValueError):
                        pass
                    else:
                        results = [{
                            "name": query,
                            "short_name": query,
                            "latitude": google_latitude,
                            "longitude": google_longitude,
                            "type": "route_destination",
                            "city": selected_city,
                            "country_code": "",
                            "source": "google_routes"
                        }]
                        GEOCODE_CACHE[cache_key] = (
                            time.monotonic(),
                            results
                        )
                        return jsonify({"results": results})
        except (requests.RequestException, ValueError):
            # Google недоступний або не зміг розпізнати адресу — тоді
            # спокійно переходимо до Photon як резервного геокодера.
            pass

    photon_failed = False
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

            raw_results = []
            photon_failed = False
            for search_query in address_queries:
                photon_params = {
                    "q": search_query,
                    "limit": 15
                }
                if bias_latitude is not None and bias_longitude is not None:
                    photon_params["lat"] = bias_latitude
                    photon_params["lon"] = bias_longitude

                try:
                    response = requests.get(
                        "https://photon.komoot.io/api/",
                        params=photon_params,
                        headers={
                            "User-Agent": (
                                "TRANVIQ/1.0 "
                                "(transport route planner)"
                            )
                        },
                        timeout=20
                    )
                    response.raise_for_status()
                    raw_results = response.json().get("features") or []
                    if raw_results:
                        break
                except (requests.RequestException, ValueError):
                    photon_failed = True
                finally:
                    GEOCODE_LAST_REQUEST_AT = time.monotonic()

                # Szanujemy limit publicznego geokodera także między
                # wariantami tego samego adresu.
                time.sleep(1.05)

        results = []
        seen = set()
        for item in raw_results:
            properties = item.get("properties") or {}
            geometry = item.get("geometry") or {}
            coordinates = geometry.get("coordinates") or []
            try:
                longitude = float(coordinates[0])
                latitude = float(coordinates[1])
            except (IndexError, TypeError, ValueError):
                continue

            result_type = str(properties.get("type") or "").lower()
            short_name = str(properties.get("name") or "").strip()
            city_name = str(
                properties.get("city")
                or properties.get("town")
                or properties.get("village")
                or ""
            ).strip()
            country = str(properties.get("country") or "").strip()
            state = str(properties.get("state") or "").strip()
            country_code = str(
                properties.get("countrycode") or ""
            ).lower()

            if not short_name:
                continue

            if search_mode == "city":
                if result_type not in {
                    "city", "town", "village", "hamlet"
                }:
                    continue
                label_parts = [short_name, state, country]
                dedupe_key = (
                    short_name.casefold(),
                    state.casefold(),
                    country.casefold()
                )
            else:
                if selected_city:
                    locality_values = {
                        city_name.casefold(),
                        str(properties.get("district") or "").casefold(),
                        str(properties.get("county") or "").casefold()
                    }
                    if selected_city.casefold() not in locality_values:
                        continue

                house_number = str(
                    properties.get("housenumber") or ""
                ).strip()
                street_name = str(
                    properties.get("street") or ""
                ).strip()
                if result_type == "house" and street_name:
                    first_part = street_name
                    if house_number:
                        first_part += " " + house_number
                else:
                    first_part = short_name

                postcode = str(
                    properties.get("postcode") or ""
                ).strip()
                locality = city_name or selected_city
                locality_part = " ".join(
                    part for part in (postcode, locality) if part
                )
                label_parts = [first_part, locality_part, country]
                dedupe_key = (
                    first_part.casefold(),
                    locality.casefold(),
                    country.casefold()
                )

            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            display_name = ", ".join(
                dict.fromkeys(
                    part for part in label_parts if part
                )
            )
            results.append({
                "name": display_name,
                "short_name": short_name,
                "latitude": latitude,
                "longitude": longitude,
                "type": result_type,
                "city": city_name or short_name,
                "country_code": country_code,
                "source": "photon"
            })

            if len(results) >= 7:
                break

        # Якщо Photon не знайшов точну адресу, пробуємо Nominatim (OSM).
        # Це важливо для приватних адрес/номерів будинків, які є на карті,
        # але інколи відсутні в індексі Photon.
        if search_mode == "address" and not results:
            try:
                with GEOCODE_LOCK:
                    now = time.monotonic()
                    wait_seconds = 1.05 - (now - GEOCODE_LAST_REQUEST_AT)
                    if wait_seconds > 0:
                        time.sleep(wait_seconds)

                    nominatim_raw = []
                    for search_query in address_queries:
                        nominatim_response = requests.get(
                            "https://nominatim.openstreetmap.org/search",
                            params={
                                "q": search_query,
                                "format": "jsonv2",
                                "addressdetails": 1,
                                "limit": 5,
                                "countrycodes": "de" if "germany" in query.casefold() else ""
                            },
                            headers={
                                "User-Agent": (
                                    "TRANVIQ/1.0 (O&O TRANS route planner; "
                                    "contact via application owner)"
                                ),
                                "Accept-Language": "de,en,pl,uk"
                            },
                            timeout=20
                        )
                        nominatim_response.raise_for_status()
                        nominatim_raw = nominatim_response.json() or []
                        GEOCODE_LAST_REQUEST_AT = time.monotonic()
                        if nominatim_raw:
                            break
                        time.sleep(1.05)

                for item in nominatim_raw:
                    try:
                        latitude = float(item.get("lat"))
                        longitude = float(item.get("lon"))
                    except (TypeError, ValueError):
                        continue

                    address = item.get("address") or {}
                    city_name = str(
                        address.get("city")
                        or address.get("town")
                        or address.get("village")
                        or address.get("municipality")
                        or ""
                    ).strip()
                    display_name = str(item.get("display_name") or query).strip()
                    results.append({
                        "name": display_name,
                        "short_name": str(item.get("name") or query).strip(),
                        "latitude": latitude,
                        "longitude": longitude,
                        "type": str(item.get("type") or "address"),
                        "city": city_name,
                        "country_code": str(address.get("country_code") or "").lower(),
                        "source": "nominatim"
                    })
                    if len(results) >= 5:
                        break
            except (requests.RequestException, ValueError):
                # Nominatim jest tylko rezerwą. Jeśli też nie odpowie,
                # zwracamy normalny brak wyników zamiast psuć trasę.
                pass

        if photon_failed and not results:
            return jsonify({
                "results": [],
                "error": "Пошук адреси тимчасово недоступний."
            }), 503

        # Nie zapisujemy pustego wyniku do cache. Dzięki temu ponowna próba
        # naprawdę ponawia geokodowanie zamiast trzy razy zwracać ten sam
        # pusty wynik z pamięci.
        if results:
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


A2_CATEGORY_1_STATIONS = [
    ("Konin Modła", 18.25),
    ("Sługocin", 18.10),
    ("Słupca", 17.87),
    ("Września", 17.58),
    ("Poznań Wschód", 17.20),
    ("Poznań Krzesiny", 17.02),
    ("Poznań Luboń", 16.93),
    ("Poznań Komorniki", 16.82),
    ("Poznań Zachód", 16.72),
    ("Buk", 16.52),
    ("Nowy Tomyśl", 16.13),
    ("Trzciel", 15.87),
    ("Jordanowo", 15.54),
    ("Torzym", 15.12),
    ("Rzepin", 14.79),
    ("Świecko", 14.59)
]

A2_CATEGORY_1_PRICES = [
    [0, 0, 41, 41, 82, 82, 82, 82, 82, 97, 123, 126, 131, 138, 141, 141],
    [0, 0, 41, 41, 82, 82, 82, 82, 82, 97, 123, 126, 131, 138, 141, 141],
    [41, 41, 0, 17, 58, 58, 58, 58, 58, 73, 99, 102, 107, 114, 117, 117],
    [41, 41, 17, 0, 41, 41, 41, 41, 41, 56, 82, 85, 90, 97, 100, 100],
    [82, 82, 58, 41, 0, 0, 0, 0, 0, 15, 41, 44, 49, 56, 59, 59],
    [82, 82, 58, 41, 0, 0, 0, 0, 0, 15, 41, 44, 49, 56, 59, 59],
    [82, 82, 58, 41, 0, 0, 0, 0, 0, 15, 41, 44, 49, 56, 59, 59],
    [82, 82, 58, 41, 0, 0, 0, 0, 0, 15, 41, 44, 49, 56, 59, 59],
    [82, 82, 58, 41, 0, 0, 0, 0, 0, 15, 41, 44, 49, 56, 59, 59],
    [97, 97, 73, 56, 15, 15, 15, 15, 15, 0, 26, 29, 34, 41, 44, 44],
    [123, 123, 99, 82, 41, 41, 41, 41, 41, 26, 0, 3, 8, 15, 18, 18],
    [126, 126, 102, 85, 44, 44, 44, 44, 44, 29, 3, 0, 5, 12, 15, 15],
    [131, 131, 107, 90, 49, 49, 49, 49, 49, 34, 8, 5, 0, 7, 10, 10],
    [138, 138, 114, 97, 56, 56, 56, 56, 56, 41, 15, 12, 7, 0, 3, 3],
    [141, 141, 117, 100, 59, 59, 59, 59, 59, 44, 18, 15, 10, 3, 0, 0],
    [141, 141, 117, 100, 59, 59, 59, 59, 59, 44, 18, 15, 10, 3, 0, 0]
]


def nearest_a2_station(longitude):
    return min(
        range(len(A2_CATEGORY_1_STATIONS)),
        key=lambda index: abs(
            A2_CATEGORY_1_STATIONS[index][1] - longitude
        )
    )


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
    a2_longitudes = []
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
                if max(point[1] for point in locations) < 14.5:
                    continue
                a2_longitudes.extend(
                    point[1]
                    for point in locations
                    if 14.45 <= point[1] <= 18.35
                )

            try:
                distance_m += float(step.get("distanceMeters") or 0)
            except (TypeError, ValueError):
                continue

    if distance_m < 1000:
        return None

    distance_km = distance_m / 1000
    amount = None
    segment = ""
    method = "official_average_rate"

    if len(a2_longitudes) >= 2:
        west_index = nearest_a2_station(min(a2_longitudes))
        east_index = nearest_a2_station(max(a2_longitudes))
        amount = A2_CATEGORY_1_PRICES[west_index][east_index]
        west_name = A2_CATEGORY_1_STATIONS[west_index][0]
        east_name = A2_CATEGORY_1_STATIONS[east_index][0]
        segment = f"{west_name} – {east_name}"
        method = "official_entry_exit_table"

    if amount is None:
        rate_pln_per_km = 141 / 255
        amount = max(3, round(distance_km * rate_pln_per_km))

    return {
        "road": "A2",
        "amount": amount,
        "currency": "PLN",
        "distance_km": round(distance_km, 1),
        "segment": segment,
        "method": method,
        "tariff_date": "2026-09-11",
        "source_url": "https://www.autostrada-a2.pl/oplaty/"
    }


def google_route(
    origin,
    destination,
    avoid_tolls,
    vehicle_profile,
    waypoints=None
):
    waypoints = waypoints or []
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
                "routes.legs.distanceMeters,"
                "routes.legs.duration,"
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
            "intermediates": [
                {
                    "location": {
                        "latLng": {
                            "latitude": waypoint[0],
                            "longitude": waypoint[1]
                        }
                    },
                    "vehicleStopover": True
                }
                for waypoint in waypoints
            ],
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

    route_legs = []
    for leg in route.get("legs") or []:
        duration_text = str(leg.get("duration") or "0s")
        try:
            leg_duration = float(
                duration_text.removesuffix("s") or 0
            )
        except (TypeError, ValueError):
            leg_duration = 0
        route_legs.append({
            "distance_m": float(leg.get("distanceMeters") or 0),
            "duration_s": leg_duration
        })

    return {
        "provider": "google",
        "distance_m": float(route.get("distanceMeters") or 0),
        "duration_s": duration_seconds,
        "points": decode_google_polyline(encoded_polyline),
        "avoid_tolls": avoid_tolls,
        "has_tolls": bool(toll_info),
        "toll_prices": toll_prices,
        "toll_estimate": toll_estimate,
        "legs": route_legs,
        "vehicle_profile": vehicle_profile
    }


def osrm_route(origin, destination, waypoints=None):
    route_points_input = [
        origin,
        *(waypoints or []),
        destination
    ]
    coordinates = ";".join(
        f"{point[1]},{point[0]}"
        for point in route_points_input
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

    route_legs = []
    for leg in route.get("legs") or []:
        route_legs.append({
            "distance_m": float(leg.get("distance") or 0),
            "duration_s": float(leg.get("duration") or 0)
        })

    return {
        "provider": "osrm",
        "distance_m": float(route.get("distance") or 0),
        "duration_s": float(route.get("duration") or 0),
        "points": route_points,
        "avoid_tolls": False,
        "has_tolls": None,
        "toll_prices": [],
        "legs": route_legs
    }


@app.route("/api/route", methods=["POST"])
def route_calculate():
    payload = request.get_json(silent=True) or {}
    origin = parse_route_point(payload.get("origin"))
    destination = parse_route_point(payload.get("destination"))
    raw_waypoints = payload.get("waypoints") or []
    waypoints = []

    if isinstance(raw_waypoints, list):
        for raw_waypoint in raw_waypoints[:23]:
            waypoint = parse_route_point(raw_waypoint)
            if waypoint:
                waypoints.append(waypoint)
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
        tuple(
            (round(point[0], 5), round(point[1], 5))
            for point in waypoints
        ),
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
                vehicle_profile,
                waypoints
            )
        else:
            route_data = osrm_route(
                origin,
                destination,
                waypoints
            )

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
                route_data = osrm_route(
                    origin,
                    destination,
                    waypoints
                )
                return jsonify(route_data)
            except (requests.RequestException, ValueError):
                pass

        return jsonify({
            "error": "Маршрутний сервіс тимчасово недоступний."
        }), 503


@app.route("/api/live-vehicle-states")
def api_live_vehicle_states():
    """Fresh Navirec positions for the live GPS map."""
    states = get_vehicle_states()
    snapshots = build_tachograph_snapshots(states)
    vehicles = []

    for vehicle in VEHICLES:
        state = state_for_vehicle(vehicle["id"], states)
        if not state:
            continue
        latitude, longitude = extract_coordinates(state.get("location"))
        if latitude is None or longitude is None:
            continue
        item = {
            "id": vehicle["id"],
            "name": vehicle["name"],
            "plate": vehicle.get("plate") or vehicle["name"],
            "latitude": latitude,
            "longitude": longitude,
            "speed": safe_float(state.get("speed")),
            "ignition": bool(state.get("ignition")),
            "activity": get_activity(state),
            "activity_started_at": state.get("activity_started_at"),
            "fuel": safe_float(state.get("fuel_level")),
            "fuel_consumption": get_vehicle_average_consumption(vehicle["id"]),
        }
        item.update(snapshots.get(vehicle["id"], {}))
        vehicles.append(item)

    if current_role() == "driver" and not driver_can_see_other_vehicles():
        own_id = current_driver_vehicle()["id"]
        vehicles = [item for item in vehicles if item.get("id") == own_id]

    return jsonify({"ok": True, "vehicles": vehicles})


@app.route("/api/driver-gps/<vehicle_id>")
def api_driver_gps(vehicle_id):
    """Return Navirec coordinates for the exact vehicle selected by driver IQ."""
    vehicle = vehicle_by_id(vehicle_id)
    if not vehicle:
        return jsonify({"ok": False, "error": "vehicle_not_found"}), 404
    if current_role() == "driver" and vehicle_id != current_driver_vehicle()["id"]:
        return jsonify({"ok": False, "error": "vehicle_not_allowed"}), 403
    state = state_for_vehicle(vehicle_id)
    if not state:
        return jsonify({"ok": False, "error": "state_unavailable"}), 503
    latitude, longitude = extract_coordinates(state.get("location"))
    if latitude is None or longitude is None:
        return jsonify({"ok": False, "error": "coordinates_unavailable"}), 503
    return jsonify({"ok": True, "vehicle_id": vehicle_id,
                    "latitude": latitude, "longitude": longitude})


@app.route("/gps")
def gps():
    selected_id = normalize_vehicle_id(
        request.args.get("vehicle", "")
    )

    states = get_vehicle_states()
    tachograph_snapshots = build_tachograph_snapshots(states)

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

        marker = {
            "id": vehicle["id"],
            "name": vehicle["name"],
            "plate": vehicle.get("plate") or vehicle["name"],
            "latitude": latitude,
            "longitude": longitude,
            "speed": speed,
            "ignition": bool(state.get("ignition")),
            "activity": get_activity(state),
            "activity_started_at": state.get("activity_started_at"),
            "fuel": fuel,
            "fuel_consumption": fuel_consumption
        }
        marker.update(
            tachograph_snapshots.get(vehicle["id"], {})
        )
        markers.append(marker)

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

        <div class="gps-status-legend">
            <span>
                <i class="gps-status-dot moving"></i> <span id="gps-legend-moving">Їде</span>
            </span>
            <span>
                <i class="gps-status-dot idling"></i> <span id="gps-legend-idling">Заведена</span>
            </span>
            <span>
                <i class="gps-status-dot stopped"></i> <span id="gps-legend-stopped">Стоїть</span>
            </span>
        </div>

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

                <label for="city-search">
                    Місто
                </label>
                <div class="gps-address-row city-only">
                    <input
                        type="search"
                        id="city-search"
                        placeholder="Почніть вводити назву міста"
                        autocomplete="off"
                    >
                </div>
                <div
                    class="gps-address-results"
                    id="city-search-results"
                    hidden
                ></div>

                <label for="destination-search">
                    Вулиця або точна адреса
                </label>
                <div class="gps-address-row">
                    <input
                        type="search"
                        id="destination-search"
                        placeholder="Спочатку виберіть місто"
                        autocomplete="off"
                        disabled
                    >
                    <button
                        type="button"
                        id="address-search-button"
                        disabled
                    >
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
                <div class="gps-delivery-planner">
                    <div class="gps-delivery-planner-title">
                        Розвізний маршрут
                    </div>
                    <label for="delivery-route-date">
                        Дата доставок
                        <input
                            type="date"
                            id="delivery-route-date"
                        >
                    </label>
                    <label for="delivery-stops-input">
                        Адреси й часові вікна
                    </label>
                    <textarea
                        id="delivery-stops-input"
                        rows="6"
                        placeholder="Кожна точка з нового рядка: адреса | 08:00 | 10:00"
                    ></textarea>
                    <div
                        id="delivery-stop-order-list"
                        style="margin-top:6px; display:none;"
                    ></div>
                    <button
                        type="button"
                        id="clear-delivery-stops-button"
                        class="secondary-button"
                        style="margin-top:6px; width:100%;"
                    >✕ Очистити всі адреси</button>
                    <button
                        type="button"
                        id="delete-vehicle-route-button"
                        class="secondary-button"
                        style="margin-top:6px; width:100%;"
                    >🗑 Видалити маршрут автомобіля</button>
                    <div class="gps-delivery-settings">
                        <label for="delivery-service-minutes">
                            Розвантаження, хв
                            <input
                                type="number"
                                id="delivery-service-minutes"
                                min="5"
                                max="180"
                                step="5"
                                value="25"
                            >
                        </label>
                        <label for="delivery-daily-rest-hours">
                            Добовий відпочинок, год
                            <input
                                type="number"
                                id="delivery-daily-rest-hours"
                                min="9"
                                max="11"
                                step="1"
                                value="11"
                            >
                        </label>
                    </div>
                    <label class="gps-privacy-consent">
                        <input
                            type="checkbox"
                            id="delivery-map-consent"
                        >
                        <span>
                            Дозволяю передати картографічним сервісам
                            лише адреси цього маршруту
                        </span>
                    </label>
                    <button
                        type="button"
                        id="build-delivery-route-button"
                        onclick="buildDeliveryRoute()"
                    >
                        Прорахувати всі доставки
                    </button>
                    <button
                        type="button"
                        id="queue-delivery-route-button"
                        onclick="buildNextDeliveryRoute()"
                        class="secondary-button"
                        style="margin-top:7px;width:100%;"
                    >
                        + Додати як наступний маршрут
                    </button>
                    <div class="gps-delivery-privacy">
                        Для карти використовуються лише адреси й часові
                        вікна. Імена та телефони не передаються.
                    </div>
                </div>
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
    const gpsUiLanguage = {ui_lang};
    const gpsUserRole = {user_role};
    const gpsLegendMoving = document.getElementById('gps-legend-moving');
    const gpsLegendIdling = document.getElementById('gps-legend-idling');
    const gpsLegendStopped = document.getElementById('gps-legend-stopped');
    if (gpsUiLanguage === 'en') {{
        if (gpsLegendMoving) gpsLegendMoving.textContent = 'Driving';
        if (gpsLegendIdling) gpsLegendIdling.textContent = 'Engine on';
        if (gpsLegendStopped) gpsLegendStopped.textContent = 'Stopped';
    }} else if (gpsUiLanguage === 'pl') {{
        if (gpsLegendMoving) gpsLegendMoving.textContent = 'Jedzie';
        if (gpsLegendIdling) gpsLegendIdling.textContent = 'Silnik włączony';
        if (gpsLegendStopped) gpsLegendStopped.textContent = 'Stoi';
    }} else if (gpsUiLanguage === 'de') {{
        if (gpsLegendMoving) gpsLegendMoving.textContent = 'Fährt';
        if (gpsLegendIdling) gpsLegendIdling.textContent = 'Motor an';
        if (gpsLegendStopped) gpsLegendStopped.textContent = 'Steht';
    }}

    // Translate previously saved Ukrainian route summaries after a language switch.
    // Source phrases are written as Unicode escapes on purpose: the server-side
    // Polish body replacements must not rewrite these lookup keys before JS runs.
    function localizeSavedRouteSummary(html) {{
        if (!html || (gpsUiLanguage !== 'pl' && gpsUiLanguage !== 'en' && gpsUiLanguage !== 'de')) return html || '';
        const englishReplacements = [
            ['\\u0420\\u043e\\u0437\\u0432\\u0456\\u0437\\u043a\\u0430', 'Delivery route'],
            ['\\u0410\\u0432\\u0442\\u043e\\u043c\\u043e\\u0431\\u0456\\u043b\\u044c:', 'Vehicle:'],
            ['\\u0412\\u043e\\u0434\\u0456\\u0439:', 'Driver:'],
            ['\\u0412\\u0456\\u0434\\u0441\\u0442\\u0430\\u043d\\u044c:', 'Distance:'],
            ['\\u0427\\u0438\\u0441\\u0442\\u0438\\u0439 \\u0447\\u0430\\u0441 \\u043a\\u0435\\u0440\\u0443\\u0432\\u0430\\u043d\\u043d\\u044f:', 'Driving time:'],
            ['\\u041f\\u043b\\u0430\\u043d\\u043e\\u0432\\u0430\\u043d\\u0438\\u0439 \\u0432\\u0438\\u0457\\u0437\\u0434:', 'Planned departure:'],
            ['\\u041f\\u043e\\u0447\\u0430\\u0442\\u043e\\u043a \\u0441\\u044c\\u043e\\u0433\\u043e\\u0434\\u043d\\u0456\\u0448\\u043d\\u044c\\u043e\\u0457 \\u0440\\u043e\\u0431\\u043e\\u0442\\u0438:', "Start of today's work:"],
            ['\\u0421\\u044c\\u043e\\u0433\\u043e\\u0434\\u043d\\u0456 \\u0432\\u0436\\u0435 \\u043f\\u0440\\u043e\\u0439\\u0434\\u0435\\u043d\\u043e:', 'Distance today:'],
            ['\\u043a\\u0435\\u0440\\u0443\\u0432\\u0430\\u043d\\u043d\\u044f:', 'driving:'],
            ['\\u041f\\u0430\\u043b\\u0438\\u0432\\u043e:', 'Fuel:'],
            ['\\u041f\\u0435\\u0440\\u0435\\u0440\\u0432 45 \\u0445\\u0432:', '45-minute breaks:'],
            ['\\u0434\\u043e\\u0431\\u043e\\u0432\\u0438\\u0445 \\u0432\\u0456\\u0434\\u043f\\u043e\\u0447\\u0438\\u043d\\u043a\\u0456\\u0432:', 'daily rests:'],
            ['\\u0424\\u0456\\u0437\\u0438\\u0447\\u043d\\u043e \\u0432\\u0456\\u043b\\u044c\\u043d\\u0438\\u0439:', 'Available from:'],
            ['\\u041d\\u0430\\u0441\\u0442\\u0443\\u043f\\u043d\\u0435 \\u0437\\u0430\\u0432\\u0430\\u043d\\u0442\\u0430\\u0436\\u0435\\u043d\\u043d\\u044f \\u043c\\u043e\\u0436\\u043d\\u0430 \\u043f\\u043b\\u0430\\u043d\\u0443\\u0432\\u0430\\u0442\\u0438:', 'Next loading can be planned from:'],
            ['\\u0420\\u0435\\u043a\\u043e\\u043c\\u0435\\u043d\\u0434\\u043e\\u0432\\u0430\\u043d\\u0438\\u0439 \\u043d\\u0430\\u0441\\u0442\\u0443\\u043f\\u043d\\u0438\\u0439 \\u0432\\u0438\\u0457\\u0437\\u0434:', 'Recommended next departure:'],
            ['\\u041f\\u0456\\u0441\\u043b\\u044f \\u0437\\u0430\\u0432\\u0435\\u0440\\u0448\\u0435\\u043d\\u043d\\u044f \\u0437\\u0430\\u043b\\u0438\\u0448\\u0430\\u0454\\u0442\\u044c\\u0441\\u044f \\u0449\\u043e\\u043d\\u0430\\u0439\\u043c\\u0435\\u043d\\u0448\\u0435', 'After completion, at least'],
            ['\\u0447\\u0430\\u0441\\u0443 \\u043a\\u0435\\u0440\\u0443\\u0432\\u0430\\u043d\\u043d\\u044f.', 'of driving time remains.'],
            [' \\u043a\\u0435\\u0440\\u0443\\u0432\\u0430\\u043d\\u043d\\u044f.', ' of driving time remains.'],
            ['\\u0414\\u043b\\u044f \\u043d\\u0430\\u0441\\u0442\\u0443\\u043f\\u043d\\u043e\\u0433\\u043e \\u0440\\u0435\\u0439\\u0441\\u0443 \\u043f\\u043e\\u0442\\u0440\\u0456\\u0431\\u0435\\u043d \\u0434\\u043e\\u0431\\u043e\\u0432\\u0438\\u0439 \\u0432\\u0456\\u0434\\u043f\\u043e\\u0447\\u0438\\u043d\\u043e\\u043a.', 'A daily rest is required before the next trip.'],
            ['\\u041c\\u0430\\u0440\\u0448\\u0440\\u0443\\u0442 \\u0443\\u0437\\u0433\\u043e\\u0434\\u0436\\u0435\\u043d\\u043e \\u0437 \\u0430\\u043a\\u0442\\u0443\\u0430\\u043b\\u044c\\u043d\\u0438\\u043c \\u0442\\u0430\\u0445\\u043e\\u0433\\u0440\\u0430\\u0444\\u043e\\u043c.', 'The route is consistent with the latest tachograph data.'],
            ['\\u0404 \\u0440\\u0438\\u0437\\u0438\\u043a \\u0437\\u0430\\u043f\\u0456\\u0437\\u043d\\u0435\\u043d\\u043d\\u044f:', 'Risk of delay:'],
            ['\\u0442\\u043e\\u0447\\u043e\\u043a \\u043f\\u043e\\u0437\\u0430 \\u0432\\u0456\\u043a\\u043d\\u043e\\u043c.', 'unloadings outside the time window.'],
            ['\\u0440\\u043e\\u0437\\u0432\\u0430\\u043d\\u0442\\u0430\\u0436\\u0435\\u043d\\u044c \\u043f\\u043e\\u0437\\u0430 \\u0447\\u0430\\u0441\\u043e\\u0432\\u0438\\u043c \\u0432\\u0456\\u043a\\u043d\\u043e\\u043c.', 'unloadings outside the time window.'],
            ['\\u0417\\u0410\\u041f\\u0406\\u0417\\u041d\\u0415\\u041d\\u041d\\u042f', 'DELAY'],
            ['\\u0431\\u0435\\u0437 \\u0447\\u0430\\u0441\\u043e\\u0432\\u043e\\u0433\\u043e \\u0432\\u0456\\u043a\\u043d\\u0430', 'no time window'],
            ['\\u0432\\u0438\\u0457\\u0437\\u0434', 'departure'],
            ['\\u0432\\u0456\\u0434 \\u043f\\u043e\\u043f\\u0435\\u0440\\u0435\\u0434\\u043d\\u044c\\u043e\\u0457 \\u0442\\u043e\\u0447\\u043a\\u0438', 'from previous stop'],
            ['\\u041e\\u043f\\u043b\\u0430\\u0442\\u0430 \\u0434\\u043e\\u0440\\u0456\\u0433:', 'Road tolls:'],
            ['\\u0434\\u0430\\u043d\\u0438\\u0445 \\u043f\\u0440\\u043e \\u043f\\u043b\\u0430\\u0442\\u043d\\u0456 \\u0434\\u0456\\u043b\\u044f\\u043d\\u043a\\u0438 \\u043d\\u0435\\u043c\\u0430\\u0454. \\u0426\\u0435 \\u043d\\u0435 \\u043e\\u0437\\u043d\\u0430\\u0447\\u0430\\u0454, \\u0449\\u043e \\u043c\\u0430\\u0440\\u0448\\u0440\\u0443\\u0442 \\u0431\\u0435\\u0437\\u043f\\u043b\\u0430\\u0442\\u043d\\u0438\\u0439.', 'no toll-section data is available. This does not mean the route is toll-free.'],
            [' \\u0433\\u043e\\u0434 ', ' h '], [' \\u0445\\u0432', ' min'], [' \\u043a\\u043c', ' km'], [' \\u043b \\u2248', ' l ≈']
        ];
        const polishExtraReplacements = [
            ['\u0414\u043e \u0432\u0438\u0457\u0437\u0434\u0443 \u0432\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e \u0441\u0442\u043e\u044f\u043d\u043a\u0443 \u0437 \u0432\u0438\u043c\u043a\u043d\u0435\u043d\u0438\u043c \u0437\u0430\u043f\u0430\u043b\u044e\u0432\u0430\u043d\u043d\u044f\u043c \u044f\u043a \u0440\u043e\u0437\u0440\u0430\u0445\u0443\u043d\u043a\u043e\u0432\u0443 \u043f\u0430\u0443\u0437\u0443. \u041f\u0456\u0441\u043b\u044f \u0437\u0430\u043f\u0443\u0441\u043a\u0443 \u0437\u0432\u0456\u0440\u0438\u0442\u0438 \u0437 \u0442\u0430\u0445\u043e\u0433\u0440\u0430\u0444\u043e\u043c.',
             'Do wyjazdu postój z wyłączonym zapłonem został uwzględniony jako szacunkowa przerwa. Po uruchomieniu pojazdu należy zweryfikować ją z danymi tachografu.']
        ];
        if (gpsUiLanguage === 'pl') {{
            polishExtraReplacements.forEach(function(pair) {{
                html = html.split(pair[0]).join(pair[1]);
            }});
        }}

        const germanReplacements = [
            ['\u0420\u043e\u0437\u0432\u0456\u0437\u043a\u0430', 'Ausliefertour'],
            ['\u0410\u0432\u0442\u043e\u043c\u043e\u0431\u0456\u043b\u044c:', 'Fahrzeug:'],
            ['\u0412\u043e\u0434\u0456\u0439:', 'Fahrer:'],
            ['\u0412\u0456\u0434\u0441\u0442\u0430\u043d\u044c:', 'Entfernung:'],
            ['\u0427\u0438\u0441\u0442\u0438\u0439 \u0447\u0430\u0441 \u043a\u0435\u0440\u0443\u0432\u0430\u043d\u043d\u044f:', 'Reine Fahrzeit:'],
            ['\u041f\u043b\u0430\u043d\u043e\u0432\u0430\u043d\u0438\u0439 \u0432\u0438\u0457\u0437\u0434:', 'Geplante Abfahrt:'],
            ['\u041f\u043e\u0447\u0430\u0442\u043e\u043a \u0441\u044c\u043e\u0433\u043e\u0434\u043d\u0456\u0448\u043d\u044c\u043e\u0457 \u0440\u043e\u0431\u043e\u0442\u0438:', 'Beginn der heutigen Arbeit:'],
            ['\u0421\u044c\u043e\u0433\u043e\u0434\u043d\u0456 \u0432\u0436\u0435 \u043f\u0440\u043e\u0439\u0434\u0435\u043d\u043e:', 'Heute bereits gefahren:'],
            ['\u043a\u0435\u0440\u0443\u0432\u0430\u043d\u043d\u044f:', 'Fahrzeit:'],
            ['\u041f\u0430\u043b\u0438\u0432\u043e:', 'Kraftstoff:'],
            ['\u041f\u0435\u0440\u0435\u0440\u0432 45 \u0445\u0432:', '45-Min.-Pausen:'],
            ['\u0434\u043e\u0431\u043e\u0432\u0438\u0445 \u0432\u0456\u0434\u043f\u043e\u0447\u0438\u043d\u043a\u0456\u0432:', 'tägliche Ruhezeiten:'],
            ['\u0424\u0456\u0437\u0438\u0447\u043d\u043e \u0432\u0456\u043b\u044c\u043d\u0438\u0439:', 'Physisch verfügbar:'],
            ['\u041d\u0430\u0441\u0442\u0443\u043f\u043d\u0435 \u0437\u0430\u0432\u0430\u043d\u0442\u0430\u0436\u0435\u043d\u043d\u044f \u043c\u043e\u0436\u043d\u0430 \u043f\u043b\u0430\u043d\u0443\u0432\u0430\u0442\u0438:', 'Nächste Beladung planbar:'],
            ['\u0420\u0435\u043a\u043e\u043c\u0435\u043d\u0434\u043e\u0432\u0430\u043d\u0438\u0439 \u043d\u0430\u0441\u0442\u0443\u043f\u043d\u0438\u0439 \u0432\u0438\u0457\u0437\u0434:', 'Empfohlene nächste Abfahrt:'],
            ['\u041f\u0456\u0441\u043b\u044f \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u043d\u044f \u0437\u0430\u043b\u0438\u0448\u0430\u0454\u0442\u044c\u0441\u044f \u0449\u043e\u043d\u0430\u0439\u043c\u0435\u043d\u0448\u0435', 'Nach Abschluss verbleiben mindestens'],
            ['\u0447\u0430\u0441\u0443 \u043a\u0435\u0440\u0443\u0432\u0430\u043d\u043d\u044f.', 'Fahrzeit.'],
            [' \u043a\u0435\u0440\u0443\u0432\u0430\u043d\u043d\u044f.', ' Fahrzeit.'],
            ['\u0414\u043b\u044f \u043d\u0430\u0441\u0442\u0443\u043f\u043d\u043e\u0433\u043e \u0440\u0435\u0439\u0441\u0443 \u043f\u043e\u0442\u0440\u0456\u0431\u0435\u043d \u0434\u043e\u0431\u043e\u0432\u0438\u0439 \u0432\u0456\u0434\u043f\u043e\u0447\u0438\u043d\u043e\u043a.', 'Vor der nächsten Tour ist eine tägliche Ruhezeit erforderlich.'],
            ['\u041c\u0430\u0440\u0448\u0440\u0443\u0442 \u0443\u0437\u0433\u043e\u0434\u0436\u0435\u043d\u043e \u0437 \u0430\u043a\u0442\u0443\u0430\u043b\u044c\u043d\u0438\u043c \u0442\u0430\u0445\u043e\u0433\u0440\u0430\u0444\u043e\u043c.', 'Die Route stimmt mit den aktuellen Tachographendaten überein.'],
            ['\u0404 \u0440\u0438\u0437\u0438\u043a \u0437\u0430\u043f\u0456\u0437\u043d\u0435\u043d\u043d\u044f:', 'Verspätungsrisiko:'],
            ['\u0442\u043e\u0447\u043e\u043a \u043f\u043e\u0437\u0430 \u0432\u0456\u043a\u043d\u043e\u043c.', 'Entladungen außerhalb des Zeitfensters.'],
            ['\u0440\u043e\u0437\u0432\u0430\u043d\u0442\u0430\u0436\u0435\u043d\u044c \u043f\u043e\u0437\u0430 \u0447\u0430\u0441\u043e\u0432\u0438\u043c \u0432\u0456\u043a\u043d\u043e\u043c.', 'Entladungen außerhalb des Zeitfensters.'],
            ['\u0417\u0410\u041f\u0406\u0417\u041d\u0415\u041d\u041d\u042f', 'VERSPÄTUNG'],
            ['\u0431\u0435\u0437 \u0447\u0430\u0441\u043e\u0432\u043e\u0433\u043e \u0432\u0456\u043a\u043d\u0430', 'ohne Zeitfenster'],
            ['\u0432\u0438\u0457\u0437\u0434', 'Abfahrt'],
            ['\u0432\u0456\u0434 \u043f\u043e\u043f\u0435\u0440\u0435\u0434\u043d\u044c\u043e\u0457 \u0442\u043e\u0447\u043a\u0438', 'vom vorherigen Stopp'],
            ['\u041e\u043f\u043b\u0430\u0442\u0430 \u0434\u043e\u0440\u0456\u0433:', 'Maut:'],
            ['\u0434\u0430\u043d\u0438\u0445 \u043f\u0440\u043e \u043f\u043b\u0430\u0442\u043d\u0456 \u0434\u0456\u043b\u044f\u043d\u043a\u0438 \u043d\u0435\u043c\u0430\u0454. \u0426\u0435 \u043d\u0435 \u043e\u0437\u043d\u0430\u0447\u0430\u0454, \u0449\u043e \u043c\u0430\u0440\u0448\u0440\u0443\u0442 \u0431\u0435\u0437\u043f\u043b\u0430\u0442\u043d\u0438\u0439.', 'Es liegen keine Daten zu mautpflichtigen Streckenabschnitten vor. Das bedeutet nicht, dass die Route mautfrei ist.'],
            [' \u0433\u043e\u0434 ', ' Std. '],
            [' \u0445\u0432', ' Min.'],
            [' \u043a\u043c', ' km'],
            [' \u043b \u2248', ' l ≈'],
        ];
        const replacements = gpsUiLanguage === 'en' ? englishReplacements : (gpsUiLanguage === 'de' ? germanReplacements : [
            ['\\u0420\\u043e\\u0437\\u0432\\u0456\\u0437\\u043a\\u0430', 'Trasa dostaw'],
            ['\\u0410\\u0432\\u0442\\u043e\\u043c\\u043e\\u0431\\u0456\\u043b\\u044c:', 'Pojazd:'],
            ['\\u0412\\u043e\\u0434\\u0456\\u0439:', 'Kierowca:'],
            ['\\u0412\\u0456\\u0434\\u0441\\u0442\\u0430\\u043d\\u044c:', 'Odległość:'],
            ['\\u0427\\u0438\\u0441\\u0442\\u0438\\u0439 \\u0447\\u0430\\u0441 \\u043a\\u0435\\u0440\\u0443\\u0432\\u0430\\u043d\\u043d\\u044f:', 'Czysty czas jazdy:'],
            ['\\u041f\\u043b\\u0430\\u043d\\u043e\\u0432\\u0430\\u043d\\u0438\\u0439 \\u0432\\u0438\\u0457\\u0437\\u0434:', 'Planowany wyjazd:'],
            ['\\u041f\\u043e\\u0447\\u0430\\u0442\\u043e\\u043a \\u0441\\u044c\\u043e\\u0433\\u043e\\u0434\\u043d\\u0456\\u0448\\u043d\\u044c\\u043e\\u0457 \\u0440\\u043e\\u0431\\u043e\\u0442\\u0438:', 'Początek dzisiejszej pracy:'],
            ['\\u0421\\u044c\\u043e\\u0433\\u043e\\u0434\\u043d\\u0456 \\u0432\\u0436\\u0435 \\u043f\\u0440\\u043e\\u0439\\u0434\\u0435\\u043d\\u043e:', 'Dzisiaj już przejechano:'],
            ['\\u043a\\u0435\\u0440\\u0443\\u0432\\u0430\\u043d\\u043d\\u044f:', 'czas jazdy:'],
            ['\\u041f\\u0430\\u043b\\u0438\\u0432\\u043e:', 'Paliwo:'],
            ['\\u041f\\u0435\\u0440\\u0435\\u0440\\u0432 45 \\u0445\\u0432:', 'Przerwy 45 min:'],
            ['\\u0434\\u043e\\u0431\\u043e\\u0432\\u0438\\u0445 \\u0432\\u0456\\u0434\\u043f\\u043e\\u0447\\u0438\\u043d\\u043a\\u0456\\u0432:', 'odpoczynki dobowe:'],
            ['\\u0424\\u0456\\u0437\\u0438\\u0447\\u043d\\u043e \\u0432\\u0456\\u043b\\u044c\\u043d\\u0438\\u0439:', 'Fizycznie wolny:'],
            ['\\u041d\\u0430\\u0441\\u0442\\u0443\\u043f\\u043d\\u0435 \\u0437\\u0430\\u0432\\u0430\\u043d\\u0442\\u0430\\u0436\\u0435\\u043d\\u043d\\u044f \\u043c\\u043e\\u0436\\u043d\\u0430 \\u043f\\u043b\\u0430\\u043d\\u0443\\u0432\\u0430\\u0442\\u0438:', 'Następny załadunek można planować:'],
            ['\\u0420\\u0435\\u043a\\u043e\\u043c\\u0435\\u043d\\u0434\\u043e\\u0432\\u0430\\u043d\\u0438\\u0439 \\u043d\\u0430\\u0441\\u0442\\u0443\\u043f\\u043d\\u0438\\u0439 \\u0432\\u0438\\u0457\\u0437\\u0434:', 'Zalecany następny wyjazd:'],
            ['\\u041f\\u0456\\u0441\\u043b\\u044f \\u0437\\u0430\\u0432\\u0435\\u0440\\u0448\\u0435\\u043d\\u043d\\u044f \\u0437\\u0430\\u043b\\u0438\\u0448\\u0430\\u0454\\u0442\\u044c\\u0441\\u044f \\u0449\\u043e\\u043d\\u0430\\u0439\\u043c\\u0435\\u043d\\u0448\\u0435', 'Po zakończeniu pozostaje co najmniej'],
            ['\\u0447\\u0430\\u0441\\u0443 \\u043a\\u0435\\u0440\\u0443\\u0432\\u0430\\u043d\\u043d\\u044f.', 'czasu jazdy.'],
            ['\\u0414\\u043b\\u044f \\u043d\\u0430\\u0441\\u0442\\u0443\\u043f\\u043d\\u043e\\u0433\\u043e \\u0440\\u0435\\u0439\\u0441\\u0443 \\u043f\\u043e\\u0442\\u0440\\u0456\\u0431\\u0435\\u043d \\u0434\\u043e\\u0431\\u043e\\u0432\\u0438\\u0439 \\u0432\\u0456\\u0434\\u043f\\u043e\\u0447\\u0438\\u043d\\u043e\\u043a.', 'Przed następną trasą wymagany jest odpoczynek dobowy.'],
            ['\\u043a\\u0435\\u0440\\u0443\\u0432\\u0430\\u043d\\u043d\\u044f.', 'jazdy.'],
            ['\\u041c\\u0430\\u0440\\u0448\\u0440\\u0443\\u0442 \\u0443\\u0437\\u0433\\u043e\\u0434\\u0436\\u0435\\u043d\\u043e \\u0437 \\u0430\\u043a\\u0442\\u0443\\u0430\\u043b\\u044c\\u043d\\u0438\\u043c \\u0442\\u0430\\u0445\\u043e\\u0433\\u0440\\u0430\\u0444\\u043e\\u043c.', 'Trasa jest zgodna z aktualnymi danymi tachografu.'],
            ['\\u0414\\u043e \\u0432\\u0438\\u0457\\u0437\\u0434\\u0443 \\u0432\\u0440\\u0430\\u0445\\u043e\\u0432\\u0430\\u043d\\u043e \\u0441\\u0442\\u043e\\u044f\\u043d\\u043a\\u0443 \\u0437 \\u0432\\u0438\\u043c\\u043a\\u043d\\u0435\\u043d\\u0438\\u043c \\u0437\\u0430\\u043f\\u0430\\u043b\\u044e\\u0432\\u0430\\u043d\\u043d\\u044f\\u043c \\u044f\\u043a \\u0440\\u043e\\u0437\\u0440\\u0430\\u0445\\u0443\\u043d\\u043a\\u043e\\u0432\\u0443 \\u043f\\u0430\\u0443\\u0437\\u0443. \\u041f\\u0456\\u0441\\u043b\\u044f \\u0437\\u0430\\u043f\\u0443\\u0441\\u043a\\u0443 \\u0437\\u0432\\u0456\\u0440\\u0438\\u0442\\u0438 \\u0437 \\u0442\\u0430\\u0445\\u043e\\u0433\\u0440\\u0430\\u0444\\u043e\\u043c.', 'Do wyjazdu postój z wyłączonym zapłonem został uwzględniony jako szacunkowa przerwa. Po uruchomieniu pojazdu należy zweryfikować ją z danymi tachografu.'],
            ['\\u0404 \\u0440\\u0438\\u0437\\u0438\\u043a \\u0437\\u0430\\u043f\\u0456\\u0437\\u043d\\u0435\\u043d\\u043d\\u044f:', 'Ryzyko opóźnienia:'],
            ['\\u0442\\u043e\\u0447\\u043e\\u043a \\u043f\\u043e\\u0437\\u0430 \\u0432\\u0456\\u043a\\u043d\\u043e\\u043c.', 'rozładunków poza oknem czasowym.'],
            ['\\u0440\\u043e\\u0437\\u0432\\u0430\\u043d\\u0442\\u0430\\u0436\\u0435\\u043d\\u044c \\u043f\\u043e\\u0437\\u0430 \\u0447\\u0430\\u0441\\u043e\\u0432\\u0438\\u043c \\u0432\\u0456\\u043a\\u043d\\u043e\\u043c.', 'rozładunków poza oknem czasowym.'],
            ['\\u0417\\u0410\\u041f\\u0406\\u0417\\u041d\\u0415\\u041d\\u041d\\u042f', 'OPÓŹNIENIE'],
            ['\\u0431\\u0435\\u0437 \\u0447\\u0430\\u0441\\u043e\\u0432\\u043e\\u0433\\u043e \\u0432\\u0456\\u043a\\u043d\\u0430', 'bez okna czasowego'],
            ['\\u0432\\u0438\\u0457\\u0437\\u0434', 'wyjazd'],
            ['\\u0432\\u0456\\u0434 \\u043f\\u043e\\u043f\\u0435\\u0440\\u0435\\u0434\\u043d\\u044c\\u043e\\u0457 \\u0442\\u043e\\u0447\\u043a\\u0438', 'od poprzedniego punktu'],
            ['\\u041e\\u043f\\u043b\\u0430\\u0442\\u0430 \\u0434\\u043e\\u0440\\u0456\\u0433:', 'Opłaty drogowe:'],
            ['\\u0434\\u0430\\u043d\\u0438\\u0445 \\u043f\\u0440\\u043e \\u043f\\u043b\\u0430\\u0442\\u043d\\u0456 \\u0434\\u0456\\u043b\\u044f\\u043d\\u043a\\u0438 \\u043d\\u0435\\u043c\\u0430\\u0454.', 'Немає даних o płatnych odcinkach.'],
            [' \\u0433\\u043e\\u0434 ', ' godz. '],
            [' \\u0445\\u0432', ' min'],
            [' \\u043a\\u043c', ' km'],
            [' \\u043b \\u2248', ' l ≈']
        ]);
        let result = html;
        replacements.forEach(function(pair) {{
            result = result.split(pair[0]).join(pair[1]);
        }});
        return result;
    }}

    const map = L.map('map').setView(
        [{lat}, {lon}],
        6
    );

    const streetLayer = L.tileLayer(
        'https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
        {{
            maxZoom: 19,
            attribution: '&copy; OpenStreetMap'
        }}
    );

    const satelliteLayer = L.tileLayer(
        'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}',
        {{
            maxZoom: 19,
            attribution: 'Tiles &copy; Esri'
        }}
    );

    const terrainLayer = L.tileLayer(
        'https://{{s}}.tile.opentopomap.org/{{z}}/{{x}}/{{y}}.png',
        {{
            maxZoom: 17,
            attribution: 'Map data &copy; OpenStreetMap contributors, SRTM | Map style &copy; OpenTopoMap'
        }}
    );

    const baseMaps = {{
        'Карта': streetLayer,
        'Супутник': satelliteLayer,
        'Рельєф': terrainLayer
    }};

    let savedMapLayer = 'Карта';
    try {{
        savedMapLayer = localStorage.getItem('oo_map_layer') || 'Карта';
    }} catch (e) {{}}

    const initialLayer = baseMaps[savedMapLayer] || streetLayer;
    initialLayer.addTo(map);
    const layerControl = L.control.layers(
        baseMaps,
        null,
        {{position: 'topright', collapsed: true}}
    ).addTo(map);
    layerControl.getContainer().style.marginTop = '42px';

    (function(control) {{
        const lang = (document.documentElement.lang || 'uk').toLowerCase().slice(0, 2);
        const names = {{
            uk: ['Карта', 'Супутник', 'Рельєф'],
            pl: ['Mapa', 'Satelita', 'Teren'],
            en: ['Map', 'Satellite', 'Terrain'],
            de: ['Karte', 'Satellit', 'Gelände']
        }}[lang] || ['Карта', 'Супутник', 'Рельєф'];

        const labels = control.getContainer().querySelectorAll(
            '.leaflet-control-layers-base label'
        );
        labels.forEach(function(label, index) {{
            const spans = label.querySelectorAll('span');
            const target = spans.length ? spans[spans.length - 1] : null;
            if (target && names[index]) {{
                target.textContent = ' ' + names[index];
            }}
        }});
    }})(layerControl);

    map.on('baselayerchange', function(event) {{
        try {{
            localStorage.setItem('oo_map_layer', event.name);
        }} catch (e) {{}}
    }});

    const bounds = [];
    const vehicleMarkersById = {{}};
    const vehiclePopupBaseById = {{}};

    vehicles.forEach(function(vehicle) {{

        const moving = Number(vehicle.speed || 0) > 1;
        const markerStatus = moving
            ? 'moving'
            : (vehicle.ignition ? 'idling' : 'stopped');
        const markerColorClass =
            'vehicle-marker-' + markerStatus;
        const markerIcon = L.divIcon({{
            className: 'vehicle-marker-icon',
            html: '<div class="vehicle-marker-pin ' +
                markerColorClass + '"></div>',
            iconSize: [28, 36],
            iconAnchor: [14, 34],
            popupAnchor: [0, -31],
            tooltipAnchor: [0, -30]
        }});

        const marker = L.marker([
            vehicle.latitude,
            vehicle.longitude
        ], {{icon: markerIcon}}).addTo(map);

        const speedUnit = gpsUiLanguage === 'en' ? ' km/h' : (gpsUiLanguage === 'uk' ? ' км/год' : ' km/h');
        const speed =
            vehicle.speed === null
            ? '—'
            : vehicle.speed.toFixed(0) + speedUnit;

        const fuel =
            vehicle.fuel === null
            ? '—'
            : vehicle.fuel.toFixed(1) + '%';

        const statusLabel = markerStatus === 'moving'
            ? (gpsUiLanguage === 'en' ? 'Driving' : (gpsUiLanguage === 'pl' ? 'Jedzie' : (gpsUiLanguage === 'de' ? 'Fährt' : 'Їде')))
            : (markerStatus === 'idling'
                ? (gpsUiLanguage === 'en' ? 'Engine on' : (gpsUiLanguage === 'pl' ? 'Silnik włączony' : (gpsUiLanguage === 'de' ? 'Motor an' : 'Заведена')))
                : (gpsUiLanguage === 'en' ? 'Stopped' : (gpsUiLanguage === 'pl' ? 'Stoi' : (gpsUiLanguage === 'de' ? 'Steht' : 'Стоїть'))));

        const numberLabel = document.createElement('span');
        numberLabel.textContent = vehicle.plate || vehicle.name;
        marker.bindTooltip(numberLabel, {{
            permanent: true,
            direction: 'top',
            offset: [0, -4],
            opacity: 1,
            className: 'vehicle-number-label'
        }});

        const popupStatusLabel = gpsUiLanguage === 'en' ? 'Status: ' : (gpsUiLanguage === 'pl' ? 'Status: ' : (gpsUiLanguage === 'de' ? 'Status: ' : 'Статус: '));
        const popupSpeedLabel = gpsUiLanguage === 'en' ? 'Speed: ' : (gpsUiLanguage === 'pl' ? 'Prędkość: ' : (gpsUiLanguage === 'de' ? 'Geschwindigkeit: ' : 'Швидкість: '));
        const popupFuelLabel = gpsUiLanguage === 'en' ? 'Fuel: ' : (gpsUiLanguage === 'pl' ? 'Paliwo: ' : (gpsUiLanguage === 'de' ? 'Kraftstoff: ' : 'Паливо: '));
        const popupCoordinatesLabel = gpsUiLanguage === 'en' ? 'Coordinates: ' : (gpsUiLanguage === 'pl' ? 'Współrzędne: ' : (gpsUiLanguage === 'de' ? 'Koordinaten: ' : 'Координати: '));
        const basePopup =
            '<strong>' + vehicle.name + '</strong><br>' +
            popupStatusLabel + statusLabel + '<br>' +
            popupSpeedLabel + speed + '<br>' +
            popupFuelLabel + fuel + '<br>' +
            popupCoordinatesLabel + vehicle.latitude.toFixed(6) + ', ' +
            vehicle.longitude.toFixed(6);
        vehicleMarkersById[vehicle.id] = marker;
        vehiclePopupBaseById[vehicle.id] = basePopup;
        marker.bindPopup(basePopup);
        marker.on('click', function() {{
            if (vehicleSelect && vehicleSelect.value !== vehicle.id) {{
                rememberManualVehicleSelection(vehicle.id);
                vehicleSelect.value = vehicle.id;
                vehicleSelect.dispatchEvent(new Event('change'));
            }} else {{
                restoreDeliveryRouteForVehicle(vehicle.id);
            }}
        }});

        if (vehicle.id === selectedId) {{
            marker.openPopup();
        }}

        bounds.push([
            vehicle.latitude,
            vehicle.longitude
        ]);
    }});

    function liveVehicleIcon(vehicle) {{
        const moving = Number(vehicle.speed || 0) > 1;
        const status = moving ? 'moving' : (vehicle.ignition ? 'idling' : 'stopped');
        return L.divIcon({{
            className: 'vehicle-marker-icon',
            html: '<div class="vehicle-marker-pin vehicle-marker-' + status + '"></div>',
            iconSize: [28, 36], iconAnchor: [14, 34], popupAnchor: [0, -31], tooltipAnchor: [0, -30]
        }});
    }}

    function liveVehiclePopup(vehicle) {{
        const moving = Number(vehicle.speed || 0) > 1;
        const statusLabel = moving
            ? (gpsUiLanguage === 'en' ? 'Driving' : (gpsUiLanguage === 'pl' ? 'Jedzie' : (gpsUiLanguage === 'de' ? 'Fährt' : 'Їде')))
            : (vehicle.ignition
                ? (gpsUiLanguage === 'en' ? 'Engine on' : (gpsUiLanguage === 'pl' ? 'Silnik włączony' : (gpsUiLanguage === 'de' ? 'Motor an' : 'Заведена')))
                : (gpsUiLanguage === 'en' ? 'Stopped' : (gpsUiLanguage === 'pl' ? 'Stoi' : (gpsUiLanguage === 'de' ? 'Steht' : 'Стоїть'))));
        const speedUnit = gpsUiLanguage === 'en' ? ' km/h' : (gpsUiLanguage === 'uk' ? ' км/год' : ' km/h');
        const speed = vehicle.speed === null ? '—' : Number(vehicle.speed).toFixed(0) + speedUnit;
        const fuel = vehicle.fuel === null ? '—' : Number(vehicle.fuel).toFixed(1) + '%';
        const popupStatusLabel = gpsUiLanguage === 'en' ? 'Status: ' : (gpsUiLanguage === 'pl' ? 'Status: ' : (gpsUiLanguage === 'de' ? 'Status: ' : 'Статус: '));
        const popupSpeedLabel = gpsUiLanguage === 'en' ? 'Speed: ' : (gpsUiLanguage === 'pl' ? 'Prędkość: ' : (gpsUiLanguage === 'de' ? 'Geschwindigkeit: ' : 'Швидкість: '));
        const popupFuelLabel = gpsUiLanguage === 'en' ? 'Fuel: ' : (gpsUiLanguage === 'pl' ? 'Paliwo: ' : (gpsUiLanguage === 'de' ? 'Kraftstoff: ' : 'Паливо: '));
        const popupCoordinatesLabel = gpsUiLanguage === 'en' ? 'Coordinates: ' : (gpsUiLanguage === 'pl' ? 'Współrzędne: ' : (gpsUiLanguage === 'de' ? 'Koordinaten: ' : 'Координати: '));
        return '<strong>' + vehicle.name + '</strong><br>' +
            popupStatusLabel + statusLabel + '<br>' + popupSpeedLabel + speed + '<br>' +
            popupFuelLabel + fuel + '<br>' + popupCoordinatesLabel + Number(vehicle.latitude).toFixed(6) + ', ' + Number(vehicle.longitude).toFixed(6);
    }}

    // LIVE REROUTE:
    // Stops/order stay fixed. Only the road geometry is recalculated from the
    // vehicle's current GPS position when the selected vehicle has moved enough.
    let liveRouteRerouteBusy = false;
    let liveRouteLastOrigin = null;
    let liveRouteLastAt = 0;
    const LIVE_ROUTE_MIN_MOVE_METERS = 1500;
    const LIVE_ROUTE_MIN_INTERVAL_MS = 60000;

    function liveRouteDistanceMeters(aLat, aLon, bLat, bLon) {{
        const R = 6371000;
        const toRad = function(v) {{ return Number(v) * Math.PI / 180; }};
        const dLat = toRad(Number(bLat) - Number(aLat));
        const dLon = toRad(Number(bLon) - Number(aLon));
        const lat1 = toRad(aLat);
        const lat2 = toRad(bLat);
        const h = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
            Math.cos(lat1) * Math.cos(lat2) *
            Math.sin(dLon / 2) * Math.sin(dLon / 2);
        return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
    }}

    function remainingLiveRouteStops(route) {{
        if (!route || !Array.isArray(route.stops)) return [];
        const notCompleted = route.stops.filter(function(stop) {{
            return String(stop.manual_status || '').toLowerCase() !== 'completed';
        }});
        // If status persistence has not yet updated, keep the full planned order.
        return notCompleted.length ? notCompleted : route.stops.slice();
    }}

    async function rerouteSelectedVehicleFromLiveGps(vehicle, force) {{
        if (liveRouteRerouteBusy || document.hidden || !vehicleSelect ||
                !vehicle || vehicleSelect.value !== vehicle.id ||
                !activeDeliveryRoute ||
                activeDeliveryRoute.vehicle_id !== vehicle.id) {{
            return;
        }}

        const now = Date.now();
        if (!force && now - liveRouteLastAt < LIVE_ROUTE_MIN_INTERVAL_MS) return;

        if (!force && liveRouteLastOrigin) {{
            const moved = liveRouteDistanceMeters(
                liveRouteLastOrigin.latitude,
                liveRouteLastOrigin.longitude,
                vehicle.latitude,
                vehicle.longitude
            );
            if (moved < LIVE_ROUTE_MIN_MOVE_METERS) return;
        }}

        const stops = remainingLiveRouteStops(activeDeliveryRoute);
        if (!stops.length) return;

        const destination = stops[stops.length - 1];
        const waypoints = stops.slice(0, -1).map(function(stop) {{
            return {{
                latitude: stop.latitude,
                longitude: stop.longitude
            }};
        }});

        liveRouteRerouteBusy = true;
        try {{
            const response = await fetch('/api/route', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{
                    origin: {{
                        latitude: Number(vehicle.latitude),
                        longitude: Number(vehicle.longitude)
                    }},
                    destination: {{
                        latitude: destination.latitude,
                        longitude: destination.longitude
                    }},
                    waypoints: waypoints,
                    avoid_tolls: selectedRouteAvoidsTolls(),
                    vehicle_profile: selectedVehicleProfile()
                }})
            }});
            const routeData = await response.json();
            if (!response.ok || !routeData.points || !routeData.points.length) return;

            // Replace ONLY the road line. Stop markers/order/status stay untouched.
            if (plannedRouteLayer && map.hasLayer(plannedRouteLayer)) {{
                map.removeLayer(plannedRouteLayer);
            }}
            plannedRouteLayer = L.polyline(routeData.points, {{
                color: '#087f8c',
                weight: 6,
                opacity: .9
            }}).addTo(map);
            plannedRouteLayer._tranviqDeliveryOverlay = true;

            liveRouteLastOrigin = {{
                latitude: Number(vehicle.latitude),
                longitude: Number(vehicle.longitude)
            }};
            liveRouteLastAt = now;
        }} catch (error) {{
            // Keep the last valid line if routing is temporarily unavailable.
        }} finally {{
            liveRouteRerouteBusy = false;
        }}
    }}

    let liveGpsRefreshBusy = false;
    async function refreshLiveVehiclePositions() {{
        if (liveGpsRefreshBusy || document.hidden) return;
        liveGpsRefreshBusy = true;
        try {{
            const response = await fetch('/api/live-vehicle-states', {{cache: 'no-store'}});
            const data = await response.json();
            if (!response.ok || !data.ok || !Array.isArray(data.vehicles)) return;
            for (const fresh of data.vehicles) {{
                let vehicle = vehicles.find(function(item) {{ return item.id === fresh.id; }});

                // Якщо сторінка відкрилась у момент, коли Navirec тимчасово не
                // повернув координати, початкового маркера ще немає. Живе
                // оновлення повинно вміти СТВОРИТИ машину, а не лише рухати
                // вже існуючий маркер.
                if (!vehicle) {{
                    vehicle = Object.assign({{}}, fresh);
                    vehicles.push(vehicle);

                    if (vehicleSelect) {{
                        const placeholder = Array.from(vehicleSelect.options).find(function(option) {{
                            return !option.value;
                        }});
                        if (placeholder) placeholder.remove();

                        const optionExists = Array.from(vehicleSelect.options).some(function(option) {{
                            return option.value === vehicle.id;
                        }});
                        if (!optionExists) {{
                            const option = document.createElement('option');
                            option.value = vehicle.id;
                            option.textContent = vehicle.name;
                            vehicleSelect.appendChild(option);
                        }}

                        // Nie wybieramy tutaj pierwszego auta w ciemno.
                        // Po pobraniu wszystkich pozycji wybierzemy pojazd,
                        // który naprawdę ma najnowszą aktywną trasę.
                    }}
                }} else {{
                    Object.assign(vehicle, fresh);
                }}

                let marker = vehicleMarkersById[fresh.id];
                if (!marker) {{
                    marker = L.marker(
                        [vehicle.latitude, vehicle.longitude],
                        {{icon: liveVehicleIcon(vehicle)}}
                    ).addTo(map);
                    vehicleMarkersById[vehicle.id] = marker;

                    const numberLabel = document.createElement('span');
                    numberLabel.textContent = vehicle.plate || vehicle.name;
                    marker.bindTooltip(numberLabel, {{
                        permanent: true, direction: 'top', offset: [0, -4],
                        opacity: 1, className: 'vehicle-number-label'
                    }});
                    marker.bindPopup(liveVehiclePopup(vehicle));
                    marker.on('click', function() {{
                        if (vehicleSelect && vehicleSelect.value !== vehicle.id) {{
                            rememberManualVehicleSelection(vehicle.id);
                            vehicleSelect.value = vehicle.id;
                            vehicleSelect.dispatchEvent(new Event('change'));
                        }} else {{
                            restoreDeliveryRouteForVehicle(vehicle.id);
                        }}
                    }});
                }} else {{
                    marker.setLatLng([fresh.latitude, fresh.longitude]);
                    marker.setIcon(liveVehicleIcon(vehicle));
                }}

                // The GPS marker may leave the originally calculated road.
                // Recalculate only the selected vehicle's line, at most once/minute
                // and only after ~1.5 km of movement from the previous reroute.
                rerouteSelectedVehicleFromLiveGps(vehicle, false);

                const basePopup = liveVehiclePopup(vehicle);
                vehiclePopupBaseById[vehicle.id] = basePopup;
                marker.setPopupContent(basePopup);

                if (typeof activeDeliveryRoute !== 'undefined' && activeDeliveryRoute && activeDeliveryRoute.vehicle_id === vehicle.id) {{
                    await refreshDeliveryStopStatuses(vehicle, activeDeliveryRoute);
                }}
            }}

            if (vehicles.length && typeof selectAndRestoreBestActiveRoute === 'function') {{
                const restored = await selectAndRestoreBestActiveRoute(false);
                if (!restored && vehicleSelect && !vehicleSelect.value) {{
                    vehicleSelect.value = vehicles[0].id;
                    vehicleSelect.dispatchEvent(new Event('change'));
                }}
            }}
        }} catch (error) {{
            // Тимчасова помилка Navirec не повинна зупиняти живу карту.
        }} finally {{
            liveGpsRefreshBusy = false;
        }}
    }}

    setInterval(refreshLiveVehiclePositions, 15000);
    document.addEventListener('visibilitychange', function() {{
        if (!document.hidden) refreshLiveVehiclePositions();
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
    // Ręczny wybór pojazdu ma zawsze pierwszeństwo przed automatyczną
    // synchronizacją tras/GPS. Zapamiętujemy go w tej przeglądarce.
    let manualSelectedVehicleId = '';
    try {{
        manualSelectedVehicleId = localStorage.getItem(
            'tranviq_manual_selected_vehicle'
        ) || '';
    }} catch (error) {{
        manualSelectedVehicleId = '';
    }}

    function rememberManualVehicleSelection(vehicleId) {{
        manualSelectedVehicleId = vehicleId || '';
        try {{
            if (manualSelectedVehicleId) {{
                localStorage.setItem(
                    'tranviq_manual_selected_vehicle',
                    manualSelectedVehicleId
                );
            }} else {{
                localStorage.removeItem('tranviq_manual_selected_vehicle');
            }}
        }} catch (error) {{
            // localStorage może być niedostępny w trybie prywatnym.
        }}
    }}
    const cityInput = document.getElementById('city-search');
    const cityResults = document.getElementById(
        'city-search-results'
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
    const deliveryRouteDate = document.getElementById(
        'delivery-route-date'
    );
    const deliveryStopsInput = document.getElementById(
        'delivery-stops-input'
    );
    const deliveryStopOrderList = document.getElementById(
        'delivery-stop-order-list'
    );
    const clearDeliveryStopsButton = document.getElementById(
        'clear-delivery-stops-button'
    );
    const deleteVehicleRouteButton = document.getElementById(
        'delete-vehicle-route-button'
    );
    const deliveryServiceMinutes = document.getElementById(
        'delivery-service-minutes'
    );
    const deliveryDailyRestHours = document.getElementById(
        'delivery-daily-rest-hours'
    );
    const buildDeliveryRouteButton = document.getElementById(
        'build-delivery-route-button'
    );
    const queueDeliveryRouteButton = document.getElementById(
        'queue-delivery-route-button'
    );
    const deliveryMapConsent = document.getElementById(
        'delivery-map-consent'
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

    const tomorrow = new Date();
    tomorrow.setDate(tomorrow.getDate() + 1);
    deliveryRouteDate.value = [
        tomorrow.getFullYear(),
        String(tomorrow.getMonth() + 1).padStart(2, '0'),
        String(tomorrow.getDate()).padStart(2, '0')
    ].join('-');
    deliveryMapConsent.addEventListener('change', function() {{
        buildDeliveryRouteButton.disabled =
            !deliveryMapConsent.checked ||
            !deliveryStopsInput.value.trim();
    }});
    deliveryStopsInput.addEventListener('input', function() {{
        buildDeliveryRouteButton.disabled =
            !deliveryMapConsent.checked ||
            !deliveryStopsInput.value.trim();
    }});
    clearDeliveryStopsButton.addEventListener('click', function() {{
        deliveryStopsInput.value = '';
        deliveryStopsInput.dispatchEvent(new Event('input'));
        deliveryStopsInput.focus();
    }});
    deleteVehicleRouteButton.addEventListener('click', async function() {{
        const vehicleId = vehicleSelect.value;
        if (!vehicleId) return;
        const vehicle = vehicles.find(function(item) {{
            return item.id === vehicleId;
        }});
        const vehicleName = vehicle ? vehicle.name : vehicleId;
        if (!window.confirm(
            "Видалити активний маршрут для " + vehicleName + "?\\n" +
            "Він буде стертий і з карти, і з пам'яті TRANVIQ."
        )) return;

        deleteVehicleRouteButton.disabled = true;
        deleteVehicleRouteButton.textContent = 'Видаляю маршрут...';
        try {{
            // Спершу прибираємо локальну копію, щоб старий маршрут не воскрес після Reload.
            try {{
                localStorage.removeItem(deliveryRouteStorageKey(vehicleId));
            }} catch (error) {{}}

            const response = await fetch(
                '/api/delivery-route/' + encodeURIComponent(vehicleId),
                {{method: 'DELETE', cache: 'no-store'}}
            );
            if (!response.ok) {{
                throw new Error('Сервер не підтвердив видалення маршруту.');
            }}

            removePlannedRoute();
            activeDeliveryRoute = null;
            deliveryStopsInput.value = '';
            renderDeliveryStopOrder();
            deliveryStopsInput.dispatchEvent(new Event('input'));
            measureResult.textContent =
                'Активний маршрут для ' + vehicleName + ' видалено.';
        }} catch (error) {{
            measureResult.textContent = error.message ||
                'Не вдалося видалити маршрут автомобіля.';
        }} finally {{
            deleteVehicleRouteButton.disabled = false;
            deleteVehicleRouteButton.textContent =
                '🗑 Видалити маршрут автомобіля';
        }}
    }});

    if (!vehicles.length) {{
        const option = document.createElement('option');
        option.textContent = 'Немає актуальних GPS-координат';
        option.disabled = true;
        option.selected = true;
        vehicleSelect.appendChild(option);
    }}

    // Не чекаємо першого 15-секундного таймера. Після повної ініціалізації
    // елементів карти одразу просимо свіжий стан машин.
    window.setTimeout(refreshLiveVehiclePositions, 250);

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

        window.requestAnimationFrame(resizeGpsMap);
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

    if (manualSelectedVehicleId &&
            Array.from(vehicleSelect.options).some(function(option) {{
                return option.value === manualSelectedVehicleId;
            }})) {{
        vehicleSelect.value = manualSelectedVehicleId;
    }}
    updateFuelConsumption();
    vehicleSelect.addEventListener('change', function(event) {{
        // Zmiana wykonana ręcznie w selektorze blokuje automatyczne
        // przeskakiwanie na pojazd z najnowszą trasą.
        if (event && event.isTrusted) {{
            rememberManualVehicleSelection(vehicleSelect.value);
        }}
        updateFuelConsumption();
        removePlannedRoute();
        restoreDeliveryRouteForVehicle(vehicleSelect.value);
    }});
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

    const gpsScreen = mapElement.closest('.gps-screen');

    function resizeGpsMap() {{
        const mapTop = mapElement.getBoundingClientRect().top;
        const vv = window.visualViewport || null;
        const viewportBottom = vv
            ? vv.offsetTop + vv.height
            : window.innerHeight;

        // Exact space visible below the page header/navigation.
        const availableHeight = Math.max(
            320,
            Math.floor(viewportBottom - mapTop)
        );

        mapElement.style.height = availableHeight + 'px';

        if (gpsScreen) {{
            gpsScreen.style.height = availableHeight + 'px';
            gpsScreen.style.minHeight = '0';
        }}

        // The planner is absolute inside gpsScreen, so constrain it to the
        // same visible area. This works on desktop, tablet and phone.
        const toolbarTop = Math.max(
            0,
            parseFloat(window.getComputedStyle(toolbar).top) || 0
        );
        const toolbarMaxHeight = Math.max(
            180,
            availableHeight - toolbarTop - 10
        );

        toolbar.style.maxHeight = toolbarMaxHeight + 'px';
        toolbar.style.overflowY = toolbar.classList.contains('collapsed')
            ? 'hidden'
            : 'auto';

        map.invalidateSize(false);
    }}

    window.addEventListener('resize', resizeGpsMap);
    window.addEventListener('orientationchange', resizeGpsMap);
    if (window.visualViewport) {{
        window.visualViewport.addEventListener('resize', resizeGpsMap);
        window.visualViewport.addEventListener('scroll', resizeGpsMap);
    }}
    window.requestAnimationFrame(resizeGpsMap);

    let measureMode = false;
    let measurePoints = [];
    let measureMarkers = [];
    let measureLayer = null;
    let selectedCity = null;
    let selectedDestination = null;
    let plannedRouteLayer = null;
    let destinationMarker = null;
    let deliveryMarkers = [];
    let deliveryStatusTimer = null;
    let activeDeliveryRoute = null;
    let citySearchTimer = null;
    let citySearchRequest = 0;
    let addressSearchTimer = null;
    let addressSearchRequest = 0;

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
        if (deliveryStatusTimer) {{
            window.clearInterval(deliveryStatusTimer);
            deliveryStatusTimer = null;
        }}
        const staleDeliveryLayers = [];
        map.eachLayer(function(layer) {{
            if (layer && layer._tranviqDeliveryOverlay) {{
                staleDeliveryLayers.push(layer);
            }}
        }});
        staleDeliveryLayers.forEach(function(layer) {{
            if (map.hasLayer(layer)) map.removeLayer(layer);
        }});
        if (plannedRouteLayer) {{
            if (map.hasLayer(plannedRouteLayer)) map.removeLayer(plannedRouteLayer);
            plannedRouteLayer = null;
        }}
        if (destinationMarker) {{
            if (map.hasLayer(destinationMarker)) map.removeLayer(destinationMarker);
            destinationMarker = null;
        }}
        deliveryMarkers.forEach(function(marker) {{
            if (map.hasLayer(marker)) map.removeLayer(marker);
        }});
        deliveryMarkers = [];
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

        const polishUi = gpsUiLanguage === 'pl';
        const englishUi = gpsUiLanguage === 'en';
        if (hours > 0) {{
            if (polishUi) return hours + ' godz. ' + minutes + ' min';
            if (englishUi) return hours + ' h ' + minutes + ' min';
            return hours + ' год ' + minutes + ' хв';
        }}
        if (polishUi || englishUi) return minutes + ' min';
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

    function formatDateTime(value) {{
        return value.toLocaleString([], {{
            day: '2-digit',
            month: '2-digit',
            hour: '2-digit',
            minute: '2-digit'
        }});
    }}

    function escapeHtml(value) {{
        const node = document.createElement('span');
        node.textContent = String(value || '');
        return node.innerHTML;
    }}

    function numberOrNull(value) {{
        if (value === null || value === undefined || value === '') {{
            return null;
        }}
        const number = Number(value);
        return Number.isFinite(number) ? number : null;
    }}

    function deliveryWindow(routeDate, clock) {{
        return new Date(routeDate + 'T' + clock + ':00');
    }}

    async function loadVehicleDaySummary(vehicleId) {{
        try {{
            const today = new Intl.DateTimeFormat('sv-SE', {{
                timeZone: 'Europe/Warsaw',
                year: 'numeric',
                month: '2-digit',
                day: '2-digit'
            }}).format(new Date());
            const response = await fetch(
                '/api/vehicle-day-summary?vehicle=' +
                encodeURIComponent(vehicleId) +
                '&date=' + encodeURIComponent(today),
                {{headers: {{'Accept': 'application/json'}}}}
            );
            const data = await response.json();
            return response.ok && data.available ? data : null;
        }} catch (error) {{
            return null;
        }}
    }}

    function calculateDeliverySchedule(
        vehicle,
        deliveryRoute,
        routeData,
        serviceMinutes,
        dailyRestHours,
        daySummary
    ) {{
        const now = new Date();
        const standardDailyDriving = 9 * 3600;
        const standardContinuousDriving = 4.5 * 3600;
        const standardShift = 13 * 3600;
        const dailyRestSeconds = dailyRestHours * 3600;
        const serviceSeconds = serviceMinutes * 60;
        const legs = routeData.legs || [];
        const firstLegSeconds = legs.length
            ? Number(legs[0].duration_s) || 0
            : routeData.duration_s /
                Math.max(1, deliveryRoute.stops.length);
        const firstStopHasWindow = Boolean(
            deliveryRoute.stops[0].window_start &&
            deliveryRoute.stops[0].window_end
        );
        let routeStart = new Date(now.getTime());
        if (firstStopHasWindow) {{
            const firstWindowStart = deliveryWindow(
                deliveryRoute.stops[0].date || deliveryRoute.date,
                deliveryRoute.stops[0].window_start
            );
            routeStart = new Date(
                firstWindowStart.getTime() - firstLegSeconds * 1000
            );
            if (routeStart < now) {{
                routeStart = new Date(now.getTime());
            }}
        }}
        let cursor = new Date(routeStart.getTime());

        const parkingStartedAt = vehicle.activity_started_at
            ? new Date(vehicle.activity_started_at)
            : null;
        const parkingActivity = [
            'parking', 'stopped', 'stop'
        ].includes(String(vehicle.activity || '').toLowerCase());
        const validParkingStart = parkingStartedAt &&
            !Number.isNaN(parkingStartedAt.getTime());
        const parkingRestSeconds = (
            !vehicle.ignition &&
            parkingActivity &&
            validParkingStart &&
            routeStart > parkingStartedAt
        )
            ? Math.max(
                0,
                Math.round(
                    (routeStart.getTime() -
                        parkingStartedAt.getTime()) / 1000
                )
            )
            : 0;
        const restBeforeStart =
            parkingRestSeconds >= dailyRestSeconds;
        const shortBreakBeforeStart =
            parkingRestSeconds >= 45 * 60;
        const todayDrivingSeconds = daySummary
            ? Math.max(0, Number(daySummary.driving_seconds) || 0)
            : 0;
        const firstMovementAt = daySummary &&
            daySummary.first_movement_at
            ? new Date(daySummary.first_movement_at)
            : null;
        const validFirstMovement = firstMovementAt &&
            !Number.isNaN(firstMovementAt.getTime());
        const elapsedShiftSeconds = validFirstMovement &&
            routeStart > firstMovementAt
            ? Math.max(
                0,
                Math.round(
                    (routeStart.getTime() -
                        firstMovementAt.getTime()) / 1000
                )
            )
            : 0;
        const pauseType = restBeforeStart
            ? 'daily_rest'
            : (shortBreakBeforeStart ? 'break_45' : 'ordinary_stop');
        const tachographAge = numberOrNull(vehicle.age_seconds);
        const tachoFresh = tachographAge === null || tachographAge <= 1800;
        const hasTachograph = Boolean(
            vehicle.card_present &&
            vehicle.has_remaining_time &&
            vehicle.api_ok &&
            tachoFresh
        );

        const dailyCandidates = [
            numberOrNull(vehicle.remaining_daily_driving_s),
            numberOrNull(vehicle.remaining_shift_driving_s),
            numberOrNull(vehicle.remaining_weekly_driving_s)
        ].filter(function(value) {{
            return value !== null && value >= 0;
        }});

        let dailyRemaining = hasTachograph && dailyCandidates.length
            ? Math.min.apply(null, dailyCandidates)
            : (restBeforeStart
                ? standardDailyDriving
                : Math.max(
                    0,
                    standardDailyDriving - todayDrivingSeconds
                ));
        let continuousRemaining = hasTachograph
            ? numberOrNull(vehicle.time_until_break_s)
            : (shortBreakBeforeStart
                ? standardContinuousDriving
                : Math.max(
                    0,
                    standardContinuousDriving - todayDrivingSeconds
                ));
        if (continuousRemaining === null) {{
            continuousRemaining = hasTachograph
                ? numberOrNull(vehicle.remaining_current_driving_s)
                : standardContinuousDriving;
        }}
        if (continuousRemaining === null) {{
            continuousRemaining = standardContinuousDriving;
        }}
        let shiftRemaining = hasTachograph
            ? numberOrNull(vehicle.time_until_daily_rest_s)
            : (restBeforeStart
                ? standardShift
                : Math.max(0, standardShift - elapsedShiftSeconds));
        if (shiftRemaining === null) {{
            shiftRemaining = standardShift;
        }}

        let breakCount = 0;
        let dailyRestCount = 0;
        let totalWaitSeconds = 0;
        let totalServiceSeconds = 0;
        let totalDrivingSeconds = 0;
        let lateCount = 0;
        const stops = [];

        function advance(seconds, countsAsWork) {{
            cursor = new Date(cursor.getTime() + seconds * 1000);
            if (countsAsWork) {{
                shiftRemaining = Math.max(0, shiftRemaining - seconds);
            }}
        }}

        function takeDailyRest() {{
            advance(dailyRestSeconds, false);
            dailyRestCount += 1;
            dailyRemaining = standardDailyDriving;
            continuousRemaining = standardContinuousDriving;
            shiftRemaining = standardShift;
        }}

        function drive(seconds) {{
            let remaining = Math.max(0, seconds);
            while (remaining > 1) {{
                if (dailyRemaining <= 1 || shiftRemaining <= 1) {{
                    takeDailyRest();
                    continue;
                }}
                if (continuousRemaining <= 1) {{
                    advance(45 * 60, false);
                    breakCount += 1;
                    continuousRemaining = standardContinuousDriving;
                    continue;
                }}

                const part = Math.min(
                    remaining,
                    dailyRemaining,
                    continuousRemaining,
                    shiftRemaining
                );
                advance(part, true);
                remaining -= part;
                totalDrivingSeconds += part;
                dailyRemaining -= part;
                continuousRemaining -= part;
            }}
        }}

        deliveryRoute.stops.forEach(function(stop, index) {{
            const leg = legs[index] || {{
                distance_m: 0,
                duration_s: routeData.duration_s /
                    Math.max(1, deliveryRoute.stops.length)
            }};
            drive(Number(leg.duration_s) || 0);

            const arrival = new Date(cursor.getTime());
            const hasWindow = Boolean(
                stop.window_start && stop.window_end
            );
            const windowStart = hasWindow
                ? deliveryWindow(stop.date || deliveryRoute.date, stop.window_start)
                : null;
            const windowEnd = hasWindow
                ? deliveryWindow(stop.date || deliveryRoute.date, stop.window_end)
                : null;
            let waitSeconds = 0;

            if (hasWindow && cursor < windowStart) {{
                waitSeconds = Math.round(
                    (windowStart.getTime() - cursor.getTime()) / 1000
                );
                totalWaitSeconds += waitSeconds;
                advance(waitSeconds, false);

                if (waitSeconds >= dailyRestSeconds) {{
                    dailyRestCount += 1;
                    dailyRemaining = standardDailyDriving;
                    continuousRemaining = standardContinuousDriving;
                    shiftRemaining = standardShift;
                }} else if (waitSeconds >= 45 * 60) {{
                    continuousRemaining = standardContinuousDriving;
                }}
            }}

            const serviceStart = new Date(cursor.getTime());
            const late = hasWindow && serviceStart > windowEnd;
            if (late) {{
                lateCount += 1;
            }}
            advance(serviceSeconds, true);
            totalServiceSeconds += serviceSeconds;

            stops.push({{
                index: index + 1,
                address: stop.address,
                distance_m: Number(leg.distance_m) || 0,
                arrival: arrival,
                service_start: serviceStart,
                departure: new Date(cursor.getTime()),
                wait_seconds: waitSeconds,
                late: late,
                date: stop.date || deliveryRoute.date,
                window_start: stop.window_start,
                window_end: stop.window_end
            }});
        }});

        const freeAt = new Date(cursor.getTime());
        const canDriveAfter = Math.min(
            dailyRemaining,
            continuousRemaining,
            shiftRemaining
        );
        let nextSafeStart = new Date(freeAt.getTime());
        let nextRecommendation = '';

        if (!hasTachograph && !restBeforeStart) {{
            nextSafeStart = new Date(
                freeAt.getTime() + dailyRestSeconds * 1000
            );
            nextRecommendation =
                (gpsUiLanguage === 'pl'
                    ? 'Bez pełnych danych z tachografu kolejny wyjazd można bezpiecznie planować dopiero po odpoczynku dobowym.'
                    : 'Без повних даних тахографа безпечно планувати новий ' +
                        'виїзд лише після добового відпочинку.');
        }} else if (
            dailyRemaining >= 60 * 60 &&
            shiftRemaining >= 60 * 60 &&
            continuousRemaining < 60 * 60
        ) {{
            nextSafeStart = new Date(
                freeAt.getTime() + 45 * 60 * 1000
            );
            nextRecommendation =
                (gpsUiLanguage === 'pl'
                    ? 'Następny załadunek można wykonać po zakończeniu dostaw, a dalszą jazdę planować po 45-minutowej przerwie. Ostatecznie zweryfikować z tachografem.'
                    : 'Наступне завантаження можна виконувати після ' +
                        'розвізки, а подальший рух планувати після перерви ' +
                        '45 хв. Остаточно звірити з тахографом.');
        }} else if (canDriveAfter >= 60 * 60) {{
            nextRecommendation = restBeforeStart && !hasTachograph
                ? (gpsUiLanguage === 'pl'
                    ? 'Przed porannym wyjazdem podczas postoju zostanie osiągnięty odpoczynek dobowy. Po zakończeniu pozostanie około ' + formatDuration(canDriveAfter) + ' czasu jazdy; po uruchomieniu należy zweryfikować dane z tachografem.'
                    : 'До ранкового виїзду за стоянкою набирається ' +
                        'добовий відпочинок. Після завершення залишається ' +
                        'орієнтовно ' + formatDuration(canDriveAfter) +
                        ' керування; після запуску звірити з тахографом.')
                : (gpsUiLanguage === 'pl'
                    ? 'Po zakończeniu pozostaje co najmniej ' + formatDuration(canDriveAfter) + ' czasu jazdy.'
                    : (gpsUiLanguage === 'en'
                        ? 'After completion, at least ' + formatDuration(canDriveAfter) + ' of driving time remains.'
                        : (gpsUiLanguage === 'de'
                            ? 'Nach Abschluss verbleiben mindestens ' + formatDuration(canDriveAfter) + ' Fahrzeit.'
                            : 'Після завершення залишається щонайменше ' + formatDuration(canDriveAfter) + ' керування.')));
        }} else {{
            nextSafeStart = new Date(
                freeAt.getTime() + dailyRestSeconds * 1000
            );
            nextRecommendation =
                (gpsUiLanguage === 'pl'
                    ? 'Przed następną trasą wymagany jest odpoczynek dobowy.'
                    : 'Для наступного рейсу потрібен добовий відпочинок.');
        }}

        return {{
            has_tachograph: hasTachograph,
            rest_before_start: restBeforeStart,
            parking_rest_s: parkingRestSeconds,
            pause_type: pauseType,
            previous_distance_km: daySummary
                ? Number(daySummary.distance_km) || 0
                : null,
            previous_driving_s: daySummary
                ? todayDrivingSeconds
                : null,
            first_movement_at: validFirstMovement
                ? firstMovementAt
                : null,
            route_start: routeStart,
            free_at: freeAt,
            next_safe_start: nextSafeStart,
            next_recommendation: nextRecommendation,
            remaining_driving_s: Math.max(0, canDriveAfter),
            break_count: breakCount,
            daily_rest_count: dailyRestCount,
            total_wait_s: totalWaitSeconds,
            total_service_s: totalServiceSeconds,
            total_driving_s: totalDrivingSeconds,
            late_count: lateCount,
            stops: stops
        }};
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

    function selectCityResult(result) {{
        selectedCity = result;
        selectedDestination = result;
        cityInput.value = result.name;
        cityResults.hidden = true;
        cityResults.replaceChildren();
        destinationInput.value = '';
        destinationInput.disabled = false;
        destinationInput.placeholder = gpsUiLanguage === 'de'
            ? 'Straße in ' + (result.short_name || result.city || 'der Stadt') + ' eingeben'
            : ('Введіть вулицю у ' + (result.short_name || result.city || 'місті'));
        addressSearchButton.disabled = false;
        addressResults.hidden = true;
        addressResults.replaceChildren();
        buildRouteButton.disabled = !vehicles.length;
        measureResult.textContent =
            'Місто вибрано: ' + result.name +
            '. Можна прокласти маршрут до міста або ввести вулицю.';
        destinationInput.focus();
    }}

    function formatTollInformation(routeData) {{
        if (routeData.avoid_tolls) {{
            return gpsUiLanguage === 'pl'
                ? 'Drogi płatne: trasa próbuje ich unikać. Sprawdź wynik, ponieważ całkowite uniknięcie opłat nie jest gwarantowane.'
                : 'Платні дороги: маршрут намагається їх уникати. ' + 'Перевірте результат, бо повне уникнення не гарантується.';
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
            return (gpsUiLanguage === 'pl' ? 'Szacunkowe opłaty drogowe: ' : 'Орієнтовна оплата доріг: ') + prices.join(' + ');
        }}

        if (routeData.toll_estimate) {{
            const estimate = routeData.toll_estimate;
            const segmentText = estimate.segment
                ? (gpsUiLanguage === 'pl' ? '; odcinek ' : '; ділянка ') + estimate.segment
                : '';
            return (gpsUiLanguage === 'pl' ? 'Szacunkowa opłata ' : 'Орієнтовна оплата ') + estimate.road + ': ≈ ' +
                Number(estimate.amount).toFixed(0) + ' ' +
                estimate.currency + ' (' +
                Number(estimate.distance_km).toFixed(1) +
                (gpsUiLanguage === 'pl' ? ' km drogą płatną' : ' км платною дорогою') + segmentText +
                (gpsUiLanguage === 'pl' ? '; taryfa z 11.09.2026).' : '; тариф від 11.09.2026).');
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

    async function searchCity() {{
        const query = cityInput.value.trim();
        const requestNumber = ++citySearchRequest;

        selectedCity = null;
        selectedDestination = null;
        destinationInput.value = '';
        destinationInput.disabled = true;
        destinationInput.placeholder = gpsUiLanguage === 'en' ? 'Select a city first' : (gpsUiLanguage === 'de' ? 'Zuerst eine Stadt auswählen' : (gpsUiLanguage === 'pl' ? 'Najpierw wybierz miasto' : 'Спочатку виберіть місто'));
        addressSearchButton.disabled = true;
        addressResults.hidden = true;
        buildRouteButton.disabled = true;
        cityResults.replaceChildren();

        if (query.length < 3) {{
            cityResults.hidden = false;
            cityResults.textContent =
                'Введіть щонайменше 3 символи.';
            return;
        }}

        cityResults.hidden = false;
        cityResults.textContent = 'Шукаю міста...';

        try {{
            const response = await fetch(
                '/api/geocode?mode=city&q=' +
                encodeURIComponent(query),
                {{headers: {{'Accept': 'application/json'}}}}
            );
            const data = await response.json();

            if (requestNumber !== citySearchRequest) {{
                return;
            }}
            if (!response.ok) {{
                throw new Error(
                    data.error || 'Пошук тимчасово недоступний.'
                );
            }}

            cityResults.replaceChildren();
            if (!data.results || !data.results.length) {{
                cityResults.textContent =
                    'Місто не знайдено. Введіть ще кілька літер.';
                return;
            }}

            const resultsHint = document.createElement('div');
            resultsHint.className = 'small';
            resultsHint.textContent = 'Виберіть місто:';
            cityResults.appendChild(resultsHint);

            data.results.forEach(function(result) {{
                const resultButton = document.createElement('button');
                resultButton.type = 'button';
                resultButton.className = 'gps-address-result';
                resultButton.textContent = result.name;
                resultButton.addEventListener('click', function() {{
                    selectCityResult(result);
                }});
                cityResults.appendChild(resultButton);
            }});
        }} catch (error) {{
            if (requestNumber !== citySearchRequest) {{
                return;
            }}
            cityResults.textContent =
                error.message || 'Пошук тимчасово недоступний.';
        }}
    }}

    async function searchAddress() {{
        const query = destinationInput.value.trim();
        const requestNumber = ++addressSearchRequest;

        if (!selectedCity) {{
            addressResults.hidden = false;
            addressResults.textContent = gpsUiLanguage === 'en' ? 'Select a city first.' : (gpsUiLanguage === 'de' ? 'Zuerst eine Stadt auswählen.' : (gpsUiLanguage === 'pl' ? 'Najpierw wybierz miasto.' : 'Спочатку виберіть місто.'));
            return;
        }}

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
                '/api/geocode?mode=address&q=' +
                encodeURIComponent(query) +
                '&city=' + encodeURIComponent(
                    selectedCity.short_name || selectedCity.city
                ) +
                '&lat=' + encodeURIComponent(selectedCity.latitude) +
                '&lon=' + encodeURIComponent(selectedCity.longitude),
                {{headers: {{'Accept': 'application/json'}}}}
            );
            const data = await response.json();

            if (requestNumber !== addressSearchRequest) {{
                return;
            }}

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

            const resultsHint = document.createElement('div');
            resultsHint.className = 'small';
            resultsHint.textContent =
                'Виберіть вулицю або адресу:';
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
            if (requestNumber !== addressSearchRequest) {{
                return;
            }}
            addressResults.textContent =
                error.message || 'Пошук тимчасово недоступний.';
        }} finally {{
            if (requestNumber === addressSearchRequest) {{
                addressSearchButton.disabled = false;
                addressSearchButton.textContent = 'Шукати';
            }}
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
                (polishUi ? '<br>Paliwo: <strong>' : '<br>Паливо: <strong>') +
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

    function deliveryRouteStorageKey(vehicleId) {{
        return 'tranviq_delivery_route_' + vehicleId;
    }}

    async function saveDeliveryRouteForVehicle(savedRoute) {{
        if (!savedRoute || !savedRoute.vehicle_id) return false;

        // Спочатку зберігаємо останню версію локально як резервну копію.
        try {{
            localStorage.setItem(
                deliveryRouteStorageKey(savedRoute.vehicle_id),
                JSON.stringify(savedRoute)
            );
        }} catch (error) {{}}

        // Важливо: чекаємо підтвердження сервера. Раніше PUT запускався у фоні,
        // тому швидкий F5 міг відновити попередню версію маршруту.
        const response = await fetch(
            '/api/delivery-route/' + encodeURIComponent(savedRoute.vehicle_id),
            {{
                method: 'PUT',
                headers: {{'Content-Type': 'application/json'}},
                cache: 'no-store',
                body: JSON.stringify({{route: savedRoute}})
            }}
        );
        if (!response.ok) {{
            let message = 'Сервер не зберіг новий маршрут.';
            try {{
                const data = await response.json();
                if (data && data.error) message = data.error;
            }} catch (error) {{}}
            throw new Error(message);
        }}
        return true;
    }}

    function removeSavedDeliveryRoute(vehicleId) {{
        if (!vehicleId) return;
        try {{
            localStorage.removeItem(deliveryRouteStorageKey(vehicleId));
        }} catch (error) {{}}

        fetch('/api/delivery-route/' + encodeURIComponent(vehicleId), {{
            method: 'DELETE'
        }}).catch(function() {{}});
    }}

    async function readSavedDeliveryRoute(vehicleId) {{
        if (!vehicleId) return null;

        // Читаємо ОБИДВІ копії. Сервер не має права затерти новіший маршрут
        // старою версією лише тому, що відповів першим після F5.
        let localSaved = null;
        try {{
            const raw = localStorage.getItem(deliveryRouteStorageKey(vehicleId));
            if (raw) {{
                const candidate = JSON.parse(raw);
                if (candidate && candidate.vehicle_id === vehicleId &&
                        candidate.delivery_route && candidate.route_data) {{
                    localSaved = candidate;
                }}
            }}
        }} catch (error) {{}}

        let serverSaved = null;
        try {{
            const response = await fetch(
                '/api/delivery-route/' + encodeURIComponent(vehicleId) +
                '?_=' + Date.now(),
                {{cache: 'no-store'}}
            );
            if (response.ok) {{
                const data = await response.json();
                const candidate = data.route;
                if (candidate && candidate.vehicle_id === vehicleId &&
                        candidate.delivery_route && candidate.route_data) {{
                    serverSaved = candidate;
                }}
            }}
        }} catch (error) {{}}

        if (gpsUserRole === 'dispatcher') {{
            // Логіст завжди показує тільки активну серверну версію.
            // Локальна копія може бути застарілою і використовується лише
            // директором як аварійний резерв під час створення маршруту.
            if (serverSaved) {{
                try {{
                    localStorage.setItem(
                        deliveryRouteStorageKey(vehicleId),
                        JSON.stringify(serverSaved)
                    );
                }} catch (error) {{}}
            }}
            return serverSaved;
        }}

        if (!localSaved && !serverSaved) return null;

        function savedRouteTime(route) {{
            if (!route || !route.saved_at) return 0;
            const value = Date.parse(route.saved_at);
            return Number.isFinite(value) ? value : 0;
        }}

        // Завжди беремо найновішу реально збережену версію маршруту.
        const saved = (!serverSaved ||
            (localSaved && savedRouteTime(localSaved) > savedRouteTime(serverSaved)))
            ? localSaved
            : serverSaved;

        try {{
            localStorage.setItem(
                deliveryRouteStorageKey(vehicleId),
                JSON.stringify(saved)
            );
        }} catch (error) {{}}

        // Якщо локальна копія новіша за серверну, одразу синхронізуємо сервер.
        if (saved === localSaved &&
                (!serverSaved || savedRouteTime(localSaved) > savedRouteTime(serverSaved))) {{
            try {{
                await saveDeliveryRouteForVehicle(localSaved);
            }} catch (error) {{
                // Для F5 локальна актуальна копія все одно залишається доступною.
            }}
        }}

        return saved;
    }}

    async function refreshVehicleDeliveryPopup(
        vehicle,
        deliveryRoute,
        statuses
    ) {{
        const marker = vehicleMarkersById[vehicle.id];
        if (!marker || !deliveryRoute || !deliveryRoute.stops ||
                !deliveryRoute.stops.length) return;

        const safeStatuses = Array.isArray(statuses) ? statuses : [];
        let nextIndex = safeStatuses.findIndex(function(status) {{
            return status !== 'completed';
        }});
        if (nextIndex < 0) nextIndex = deliveryRoute.stops.length - 1;

        const remainingStops = deliveryRoute.stops.slice(nextIndex);
        const nextStop = remainingStops[0];
        const finalStop = remainingStops[remainingStops.length - 1];
        if (!nextStop || !finalStop) return;

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
                        latitude: finalStop.latitude,
                        longitude: finalStop.longitude
                    }},
                    waypoints: remainingStops.slice(0, -1).map(function(stop) {{
                        return {{
                            latitude: stop.latitude,
                            longitude: stop.longitude
                        }};
                    }}),
                    avoid_tolls: false,
                    vehicle_profile: 'van'
                }})
            }});
            const data = await response.json();
            if (!response.ok) return;

            const legs = Array.isArray(data.legs) ? data.legs : [];
            const firstLeg = legs.length ? legs[0] : null;
            const nextDistance = firstLeg
                ? Number(firstLeg.distance_m || 0)
                : Number(data.distance_m || 0);
            const nextDuration = firstLeg
                ? Number(firstLeg.duration_s || 0)
                : Number(data.duration_s || 0);
            const finalDistance = Number(data.distance_m || 0);
            const finalDuration = Number(data.duration_s || 0);

            const nextDeliveryLabel = gpsUiLanguage === 'pl'
                ? 'Do następnego rozładunku:'
                : gpsUiLanguage === 'en'
                    ? 'To next delivery:'
                    : gpsUiLanguage === 'de'
                        ? 'Bis zur nächsten Entladung:'
                        : 'До наступної вигрузки:';
            const finalDeliveryLabel = gpsUiLanguage === 'pl'
                ? 'Do ostatniego rozładunku:'
                : gpsUiLanguage === 'en'
                    ? 'To final delivery:'
                    : gpsUiLanguage === 'de'
                        ? 'Bis zur letzten Entladung:'
                        : 'До останньої вигрузки:';
            const distanceUnit = gpsUiLanguage === 'uk' ? ' км · ' : ' km · ';
            let extra = '<hr style="margin:7px 0">' +
                '<strong>' + nextDeliveryLabel + '</strong> ' +
                (nextDistance / 1000).toFixed(1) + distanceUnit +
                formatDuration(nextDuration);
            if (remainingStops.length > 1) {{
                extra += '<br><strong>' + finalDeliveryLabel + '</strong> ' +
                    (finalDistance / 1000).toFixed(1) + distanceUnit +
                    formatDuration(finalDuration);
            }}
            marker.setPopupContent(
                vehiclePopupBaseById[vehicle.id] + extra
            );
        }} catch (error) {{
            // Відстані в popup не повинні ламати карту.
        }}
    }}

    function deliveryStopLine(stop) {{
        let line = stop.address;
        if (stop.window_start && stop.window_end) {{
            if (stop.date) {{
                line += ' | ' + stop.date + ' | ' + stop.window_start + ' | ' + stop.window_end;
            }} else {{
                line += ' | ' + stop.window_start + ' | ' + stop.window_end;
            }}
        }}
        return line;
    }}

    function renderDeliveryStopOrder() {{
        if (!deliveryStopOrderList) return;
        const stops = activeDeliveryRoute &&
            Array.isArray(activeDeliveryRoute.stops)
            ? activeDeliveryRoute.stops
            : [];
        if (!stops.length) {{
            deliveryStopOrderList.innerHTML = '';
            deliveryStopOrderList.style.display = 'none';
            return;
        }}
        deliveryStopOrderList.style.display = 'block';
        const rows = stops.map(function(stop, index) {{
            const upDisabled = index === 0 ? ' disabled' : '';
            const downDisabled = index === stops.length - 1 ? ' disabled' : '';
            return '<div style="display:flex;align-items:center;gap:6px;' +
                'padding:7px 8px;border-top:1px solid #e3eaee;">' +
                '<div style="min-width:0;flex:1;font-size:12px;' +
                'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">' +
                '<strong>' + (index + 1) + '.</strong> ' +
                escapeHtml(stop.address) + '</div>' +
                '<button type="button" title="Підняти вище"' + upDisabled +
                ' onclick="reorderActiveDeliveryStops(' + index + ', -1)"' +
                ' style="width:34px;height:30px;">↑</button>' +
                '<button type="button" title="Опустити нижче"' + downDisabled +
                ' onclick="reorderActiveDeliveryStops(' + index + ', 1)"' +
                ' style="width:34px;height:30px;">↓</button>' +
                '<button type="button" title="Видалити точку"' +
                ' onclick="removeActiveDeliveryStop(' + index + ')"' +
                ' style="width:34px;height:30px;">✕</button>' +
                '</div>';
        }}).join('');
        deliveryStopOrderList.innerHTML =
            '<details style="margin:5px 0;border:1px solid #cbd8df;' +
            'border-radius:9px;background:#fff;overflow:hidden;">' +
            '<summary style="cursor:pointer;padding:9px 11px;' +
            'font-size:12px;font-weight:800;">↕ ' +
            (gpsUiLanguage === 'en' ? 'Reorder addresses' : (gpsUiLanguage === 'pl' ? 'Zmień kolejność adresów' : (gpsUiLanguage === 'de' ? 'Adressreihenfolge ändern' : 'Змінити порядок адрес'))) + ' (' +
            stops.length + ')</summary>' + rows + '</details>';
    }}

    function reorderActiveDeliveryStops(index, direction) {{
        if (!activeDeliveryRoute || !Array.isArray(activeDeliveryRoute.stops)) {{
            return;
        }}
        const target = index + direction;
        if (target < 0 || target >= activeDeliveryRoute.stops.length) return;

        const stops = activeDeliveryRoute.stops.slice();
        const moved = stops.splice(index, 1)[0];
        stops.splice(target, 0, moved);
        deliveryStopsInput.value = stops.map(deliveryStopLine).join('\\n');
        activeDeliveryRoute.stops = stops;
        renderDeliveryStopOrder();
        buildDeliveryRoute();
    }}

    function removeActiveDeliveryStop(index) {{
        if (!activeDeliveryRoute || !Array.isArray(activeDeliveryRoute.stops)) {{
            return;
        }}
        const stops = activeDeliveryRoute.stops.slice();
        if (index < 0 || index >= stops.length) return;
        stops.splice(index, 1);
        activeDeliveryRoute.stops = stops;
        deliveryStopsInput.value = stops.map(deliveryStopLine).join('\\n');
        renderDeliveryStopOrder();
        if (stops.length) {{
            buildDeliveryRoute();
        }} else {{
            deliveryStopsInput.dispatchEvent(new Event('input'));
        }}
    }}

    window.reorderActiveDeliveryStops = reorderActiveDeliveryStops;
    window.removeActiveDeliveryStop = removeActiveDeliveryStop;

    function drawDeliveryStopMarkers(deliveryRoute) {{
        deliveryRoute.stops.forEach(function(stop, index) {{
            const marker = L.marker(
                [stop.latitude, stop.longitude],
                {{icon: deliveryStopIcon(index + 1, 'pending')}}
            ).addTo(map);
            marker.bindPopup(deliveryStopPopup(
                stop,
                index,
                stop.manual_status || 'pending'
            ));
            marker._tranviqDeliveryOverlay = true;
            deliveryMarkers.push(marker);
        }});
    }}

    async function restoreDeliveryRouteForVehicle(vehicleId, savedOverride) {{
        liveRouteLastOrigin = null;
        liveRouteLastAt = 0;
        removeMeasurementLayers();
        removePlannedRoute();
        activeDeliveryRoute = null;
        renderDeliveryStopOrder();

        // savedOverride використовується живою синхронізацією карти логіста.
        // У цьому випадку серверна версія є авторитетною і стара localStorage
        // копія в іншому браузері не може повернути карту назад.
        const saved = savedOverride || await readSavedDeliveryRoute(vehicleId);
        if (vehicleSelect && vehicleSelect.value !== vehicleId) {{
            return;
        }}
        if (savedOverride) {{
            try {{
                localStorage.setItem(
                    deliveryRouteStorageKey(vehicleId),
                    JSON.stringify(savedOverride)
                );
            }} catch (error) {{}}
        }}
        if (!saved) {{
            deliveryStopsInput.value = '';
            measureResult.textContent =
                'Для цього автомобіля активного розвізного маршруту немає.';
            buildDeliveryRouteButton.disabled =
                !deliveryMapConsent.checked;
            return;
        }}

        const vehicle = vehicles.find(function(item) {{
            return item.id === vehicleId;
        }});
        if (!vehicle) return;

        activeDeliveryRoute = saved.delivery_route;
        deliveryStopsInput.value = saved.input_text || '';
        renderDeliveryStopOrder();
        deliveryRouteDate.value = saved.delivery_route.date ||
            deliveryRouteDate.value;
        if (saved.service_minutes) {{
            deliveryServiceMinutes.value = saved.service_minutes;
        }}
        if (saved.daily_rest_hours) {{
            deliveryDailyRestHours.value = saved.daily_rest_hours;
        }}
        if (saved.vehicle_profile) {{
            vehicleProfileSelect.value = saved.vehicle_profile;
        }}

        drawDeliveryStopMarkers(saved.delivery_route);
        if (saved.route_data.points && saved.route_data.points.length) {{
            plannedRouteLayer = L.polyline(saved.route_data.points, {{
                color: '#087f8c',
                weight: 6,
                opacity: .9
            }}).addTo(map);
            plannedRouteLayer._tranviqDeliveryOverlay = true;
            map.fitBounds(
                plannedRouteLayer.getBounds(),
                {{padding: [45, 45]}}
            );
        }}
        if (saved.summary_html) {{
            measureResult.innerHTML = localizeSavedRouteSummary(saved.summary_html);
        }} else {{
            measureResult.innerHTML =
                '<strong>' + escapeHtml(saved.delivery_route.label) +
                '</strong><br>' +
                (gpsUiLanguage === 'pl'
                    ? 'Przywrócono zapisaną trasę dla <strong>'
                    : 'Відновлено збережений маршрут для <strong>') +
                escapeHtml(vehicle.name) + '</strong>.';
        }}

        await refreshDeliveryStopStatuses(
            vehicle,
            saved.delivery_route
        );
        deliveryStatusTimer = window.setInterval(function() {{
            refreshDeliveryStopStatuses(vehicle, saved.delivery_route);
        }}, 60000);
        buildDeliveryRouteButton.disabled =
            !deliveryMapConsent.checked ||
            !deliveryStopsInput.value.trim();
    }}

    // ВІДНОВЛЕННЯ СПІЛЬНОГО МАРШРУТУ ПІСЛЯ REDEPLOY RENDER.
    // На безкоштовному Render файл у /tmp може очиститися при новому deploy.
    // Тому директор, у якого є актуальна локальна копія маршруту, автоматично
    // публікує її назад на сервер. Водій і логіст після цього читають той самий маршрут.
    async function republishDirectorLocalRoutes() {{
        if (gpsUserRole !== 'director') return;
        const prefix = 'tranviq_delivery_route_';
        const candidates = [];
        try {{
            for (let i = 0; i < localStorage.length; i += 1) {{
                const key = localStorage.key(i);
                if (!key || !key.startsWith(prefix)) continue;
                const raw = localStorage.getItem(key);
                if (!raw) continue;
                const saved = JSON.parse(raw);
                if (!saved || !saved.vehicle_id || !saved.delivery_route || !saved.route_data) continue;
                candidates.push(saved);
            }}
        }} catch (error) {{
            return;
        }}
        for (const saved of candidates) {{
            try {{
                const response = await fetch(
                    '/api/delivery-route/' + encodeURIComponent(saved.vehicle_id) + '?restore=' + Date.now(),
                    {{cache: 'no-store'}}
                );
                let serverSaved = null;
                if (response.ok) {{
                    const data = await response.json();
                    serverSaved = data && data.route ? data.route : null;
                }}
                const localTime = Date.parse(saved.saved_at || '') || 0;
                const serverTime = Date.parse((serverSaved && serverSaved.saved_at) || '') || 0;
                if (!serverSaved || localTime >= serverTime) {{
                    await saveDeliveryRouteForVehicle(saved);
                }}
            }} catch (error) {{}}
        }}
    }}

    if (gpsUserRole === 'director') {{
        window.setTimeout(republishDirectorLocalRoutes, 1200);
        window.setInterval(republishDirectorLocalRoutes, 10000);
    }}

    // ЖИВА СИНХРОНІЗАЦІЯ КАРТИ ЛОГІСТА МІЖ БРАУЗЕРАМИ/ПРИСТРОЯМИ.
    // Перевіряємо всі активні маршрути, а не тільки випадково вибране авто.
    // Після нового deploy серверний /tmp може бути порожнім, тому локальна
    // копія браузера використовується як резерв і повертається на сервер.
    const lastServerRouteStampByVehicle = {{}};
    let deliveryRouteSyncBusy = false;
    let lastBestRouteCheckAt = 0;

    async function fetchServerDeliveryRoute(vehicleId) {{
        if (!vehicleId) return null;
        const response = await fetch(
            '/api/delivery-route/' + encodeURIComponent(vehicleId) +
            '?sync=' + Date.now(),
            {{cache: 'no-store'}}
        );
        if (!response.ok) return null;
        const data = await response.json();
        const candidate = data.route;
        if (!candidate || candidate.vehicle_id !== vehicleId ||
                !candidate.delivery_route || !candidate.route_data) {{
            return null;
        }}
        return candidate;
    }}

    async function fetchAllServerDeliveryRoutes() {{
        try {{
            const response = await fetch(
                '/api/delivery-routes?sync=' + Date.now(),
                {{cache: 'no-store'}}
            );
            if (!response.ok) return {{}};
            const data = await response.json();
            return data && data.routes && typeof data.routes === 'object'
                ? data.routes
                : {{}};
        }} catch (error) {{
            return {{}};
        }}
    }}

    function serverRouteStamp(saved) {{
        if (!saved) return '';
        try {{
            return JSON.stringify({{
                vehicle_id: saved.vehicle_id || '',
                input_text: saved.input_text || '',
                service_minutes: saved.service_minutes || 0,
                daily_rest_hours: saved.daily_rest_hours || 0,
                delivery_route: saved.delivery_route || null,
                route_data: saved.route_data || null
            }});
        }} catch (error) {{
            return String(saved.saved_at || '') + '|' + Date.now();
        }}
    }}

    function savedRouteTimestamp(saved) {{
        if (!saved || !saved.saved_at) return 0;
        const value = Date.parse(saved.saved_at);
        return Number.isFinite(value) ? value : 0;
    }}

    function localDeliveryRoutesForVisibleVehicles() {{
        const routes = {{}};
        vehicles.forEach(function(vehicle) {{
            try {{
                const raw = localStorage.getItem(
                    deliveryRouteStorageKey(vehicle.id)
                );
                if (!raw) return;
                const saved = JSON.parse(raw);
                if (saved && saved.vehicle_id === vehicle.id &&
                        saved.delivery_route && saved.route_data) {{
                    routes[vehicle.id] = saved;
                }}
            }} catch (error) {{}}
        }});
        return routes;
    }}

    function newestRouteCandidate(serverRoutes, localRoutes) {{
        let best = null;
        function consider(saved, source) {{
            if (!saved || !saved.vehicle_id ||
                    !saved.delivery_route || !saved.route_data) return;
            if (!vehicles.some(function(vehicle) {{
                return vehicle.id === saved.vehicle_id;
            }})) return;
            const candidate = {{
                saved: saved,
                source: source,
                time: savedRouteTimestamp(saved)
            }};
            if (!best || candidate.time > best.time) best = candidate;
        }}
        Object.keys(serverRoutes || {{}}).forEach(function(vehicleId) {{
            consider(serverRoutes[vehicleId], 'server');
        }});
        Object.keys(localRoutes || {{}}).forEach(function(vehicleId) {{
            const localSaved = localRoutes[vehicleId];
            const serverSaved = (serverRoutes || {{}})[vehicleId];
            if (!serverSaved ||
                    savedRouteTimestamp(localSaved) > savedRouteTimestamp(serverSaved)) {{
                consider(localSaved, 'local');
            }}
        }});
        return best;
    }}

    async function selectAndRestoreBestActiveRoute(force) {{
        if (!vehicleSelect || !vehicles.length) return false;
        const now = Date.now();
        if (!force && now - lastBestRouteCheckAt < 1500) {{
            return Boolean(activeDeliveryRoute);
        }}
        lastBestRouteCheckAt = now;

        // Jeśli użytkownik ręcznie wybrał pojazd, synchronizacja może
        // odświeżać jego trasę, ale NIE może zmienić wybranego pojazdu.
        if (manualSelectedVehicleId &&
                vehicles.some(function(item) {{ return item.id === manualSelectedVehicleId; }})) {{
            if (vehicleSelect.value !== manualSelectedVehicleId) {{
                vehicleSelect.value = manualSelectedVehicleId;
                updateFuelConsumption();
            }}
            const manualServerSaved = await fetchServerDeliveryRoute(
                manualSelectedVehicleId
            );
            if (manualServerSaved) {{
                const manualStamp = serverRouteStamp(manualServerSaved);
                const manualCurrentStamp =
                    lastServerRouteStampByVehicle[manualSelectedVehicleId] || '';
                const manualActiveVehicleId = activeDeliveryRoute
                    ? activeDeliveryRoute.vehicle_id
                    : '';
                if (force || manualActiveVehicleId !== manualSelectedVehicleId ||
                        manualStamp !== manualCurrentStamp) {{
                    lastServerRouteStampByVehicle[manualSelectedVehicleId] = manualStamp;
                    await restoreDeliveryRouteForVehicle(
                        manualSelectedVehicleId,
                        manualServerSaved
                    );
                }}
            }}
            // Nawet jeśli wybrany pojazd nie ma jeszcze trasy, zostaje wybrany.
            return true;
        }}

        const serverRoutes = await fetchAllServerDeliveryRoutes();
        // Для логіста сервер є єдиним джерелом активного маршруту.
        // Старий localStorage у браузері логіста не має права "воскресити"
        // маршрут минулого тижня і тим більше записати його назад на сервер.
        const localRoutes = gpsUserRole === 'dispatcher'
            ? {{}}
            : localDeliveryRoutesForVisibleVehicles();
        const best = newestRouteCandidate(serverRoutes, localRoutes);
        if (!best) return false;

        const saved = best.saved;
        const vehicleId = saved.vehicle_id;

        if (best.source === 'local') {{
            try {{
                await saveDeliveryRouteForVehicle(saved);
            }} catch (error) {{
                // Lokalna kopia nadal pozwala odtworzyć trasę na tym urządzeniu.
            }}
        }}

        if (vehicleSelect.value !== vehicleId) {{
            vehicleSelect.value = vehicleId;
            updateFuelConsumption();
        }}

        const stamp = serverRouteStamp(saved);
        const currentStamp = lastServerRouteStampByVehicle[vehicleId] || '';
        const activeVehicleId = activeDeliveryRoute
            ? activeDeliveryRoute.vehicle_id
            : '';
        if (force || activeVehicleId !== vehicleId || stamp !== currentStamp) {{
            lastServerRouteStampByVehicle[vehicleId] = stamp;
            await restoreDeliveryRouteForVehicle(vehicleId, saved);
        }}
        return true;
    }}

    async function syncSelectedDeliveryRouteFromServer() {{
        if (deliveryRouteSyncBusy || document.hidden || !vehicleSelect) return;
        deliveryRouteSyncBusy = true;
        try {{
            const restored = await selectAndRestoreBestActiveRoute(false);
            if (restored) return;

            const vehicleId = vehicleSelect.value;
            if (!vehicleId) return;
            const serverSaved = await fetchServerDeliveryRoute(vehicleId);
            if (!serverSaved) return;
            const stamp = serverRouteStamp(serverSaved);
            const currentStamp = lastServerRouteStampByVehicle[vehicleId] || '';
            if (stamp !== currentStamp) {{
                lastServerRouteStampByVehicle[vehicleId] = stamp;
                await restoreDeliveryRouteForVehicle(vehicleId, serverSaved);
            }}
        }} catch (error) {{
            // Тимчасова мережна помилка не повинна ламати карту.
        }} finally {{
            deliveryRouteSyncBusy = false;
        }}
    }}

    window.setInterval(syncSelectedDeliveryRouteFromServer, 10000);
    window.setTimeout(syncSelectedDeliveryRouteFromServer, 1200);
    document.addEventListener('visibilitychange', function() {{
        if (!document.hidden) syncSelectedDeliveryRouteFromServer();
    }});

    function parseDeliveryStopLines() {{
        const rawText = deliveryStopsInput.value.trim();
        if (!rawText) {{
            throw new Error('Вставте адреси або текст транспортного завдання.');
        }}

        // Заявка у форматі:
        // 2026-10-01 08:00 / COMPANY / STREET / DE 61440 CITY
        // Кожна наступна дата починає нову точку.
        const datedLineRe = /^(\d{{4}}-\d{{2}}-\d{{2}})(?:\s+(\d{{1,2}}:\d{{2}}))?\s*$/;
        const datedLines = rawText.replace(/\\r/g, '').split('\\n')
            .map(function(x) {{ return x.trim(); }})
            .filter(Boolean);
        const datedStarts = [];
        datedLines.forEach(function(line, idx) {{
            if (datedLineRe.test(line)) datedStarts.push(idx);
        }});
        if (datedStarts.length >= 1) {{
            const datedStops = [];
            datedStarts.forEach(function(startIdx, n) {{
                const endIdx = (n + 1 < datedStarts.length) ? datedStarts[n + 1] : datedLines.length;
                const m = datedLines[startIdx].match(datedLineRe);
                const body = datedLines.slice(startIdx + 1, endIdx);
                let addressRows = body.slice();
                if (addressRows.length >= 3) addressRows = addressRows.slice(1);
                const address = addressRows.join(', ')
                    .replace(/\bDE\s+(\d{{5}})\b/ig, '$1')
                    .replace(/\bPL\s+(\d{{2}}-\d{{3}})\b/ig, '$1')
                    .replace(/\s*,\s*/g, ', ')
                    .trim();
                if (address) {{
                    datedStops.push({{
                        address: address,
                        date: m[1] || null,
                        window_start: m[2] || null,
                        window_end: m[2] || null,
                        stop_type: null
                    }});
                }}
            }});
            if (datedStops.length) return datedStops;
        }}

        const validTime = /^([01]\\d|2[0-3]):[0-5]\\d$/;
        const datePattern = /^(?:\\d{{4}}[-./]\\d{{2}}[-./]\\d{{2}}|\\d{{2}}[-./]\\d{{2}}[-./]\\d{{4}})$/;
        function normalizeDeliveryDate(value) {{
            const text = String(value || '').trim();
            let match = text.match(/^(\\d{{4}})[-./](\\d{{2}})[-./](\\d{{2}})$/);
            if (match) return match[1] + '-' + match[2] + '-' + match[3];
            match = text.match(/^(\\d{{2}})[-./](\\d{{2}})[-./](\\d{{4}})$/);
            if (match) return match[3] + '-' + match[2] + '-' + match[1];
            return null;
        }}
        const countryNames = {{
            DE: 'Germany', PL: 'Poland', CZ: 'Czechia', AT: 'Austria',
            NL: 'Netherlands', BE: 'Belgium', FR: 'France', IT: 'Italy',
            ES: 'Spain', PT: 'Portugal', DK: 'Denmark', SE: 'Sweden',
            NO: 'Norway', FI: 'Finland', LT: 'Lithuania', LV: 'Latvia',
            EE: 'Estonia', SK: 'Slovakia', HU: 'Hungary', RO: 'Romania',
            BG: 'Bulgaria', HR: 'Croatia', SI: 'Slovenia', CH: 'Switzerland',
            LU: 'Luxembourg'
        }};

        function stopTypeFromText(text) {{
            const value = String(text || '').toLowerCase();
            if (/za[łl]adunek|loading|laden|beladung|завантаж|загрузка/.test(value)) {{
                return 'loading';
            }}
            if (/roz[łl]adunek|unloading|entladen|entladung|розвантаж|выгруз/.test(value)) {{
                return 'unloading';
            }}
            return null;
        }}

        function cleanLabel(line) {{
            return line.replace(
                /^(?:\\d+[.)]?\\s*)?(?:za[łl]adunek|roz[łl]adunek|loading|unloading|laden|beladung|entladen|entladung|завантаження|розвантаження|загрузка|выгрузка)\\s*:?\\s*/i,
                ''
            ).trim();
        }}

        function addressFromBlock(blockLines) {{
            const useful = blockLines.map(function(line) {{
                return line.trim();
            }}).filter(function(line) {{
                if (!line) return false;
                if (datePattern.test(line)) return false;
                if (stopTypeFromText(line) && !cleanLabel(line)) return false;
                if (/^\\d+\\s+\\d+$/.test(line)) return false;
                return true;
            }});

            let postalIndex = -1;
            let countryCode = '';
            for (let index = 0; index < useful.length; index += 1) {{
                const match = useful[index].match(
                    /^(?:([A-Z]{{2}})[-\\s]*)?(\\d{{4,6}})\\s+(.+)$/i
                );
                if (match) {{
                    postalIndex = index;
                    countryCode = (match[1] || '').toUpperCase();
                    break;
                }}
            }}

            if (postalIndex >= 0) {{
                const postalLine = useful[postalIndex];
                const before = useful.slice(0, postalIndex);
                // Останній рядок перед індексом зазвичай є вулицею; назву фірми
                // навмисно не передаємо геокодеру.
                const street = before.length ? before[before.length - 1] : '';
                let address = (street ? street + ', ' : '') + postalLine;
                if (countryCode && countryNames[countryCode]) {{
                    address += ', ' + countryNames[countryCode];
                }}
                return address;
            }}

            // Для вже готових адрес «один рядок = одна точка» лишаємо
            // стару поведінку.
            return useful.length ? cleanLabel(useful[useful.length - 1]) : '';
        }}

        // Старий/ручний формат: одна готова адреса на рядок, за бажанням
        // з часовим вікном через |.
        const simpleLines = rawText.split(/\\r?\\n/)
            .map(function(line) {{ return line.trim(); }})
            .filter(Boolean);
        const looksLikeOrder = simpleLines.some(function(line) {{
            return datePattern.test(line) || stopTypeFromText(line);
        }});

        // Plain copied transport data often comes as:
        //   Company name
        //   Street, DE12345 City, DE
        //   Company name
        //   Street, DE12345 City, DE
        // There are TWO stops here, not four. Detect address-looking lines
        // and treat the company line immediately before each one as a label.
        function looksLikePlainAddress(line) {{
            const value = String(line || '').trim();
            if (!value) return false;
            return (
                /(?:^|[,\\s])(?:DE|PL|CZ|AT|NL|BE|FR|IT|ES|PT|DK|SE|NO|FI|LT|LV|EE|SK|HU|RO|BG|HR|SI|CH|LU)[-\\s]?\\d{{4,6}}\\s+[^,]+(?:,\\s*[A-Z]{{2}})?$/i.test(value) ||
                /\\b\\d{{4,6}}\\s+[\\p{{L}}][\\p{{L}} .'-]+(?:,\\s*[A-Z]{{2}})?$/u.test(value)
            );
        }}

        if (!looksLikeOrder && simpleLines.length >= 2) {{
            /*
             * Plain copied addresses:
             * accumulate lines until a line containing postal code + city.
             * That line ENDS the current stop. The next line starts a new stop.
             * No blank separator is required.
             */
            function isPostalEnd(line) {{
                const v = String(line || '').trim();
                return (
                    /(?:^|[\s,])\d{{2}}-\d{{3}}(?:\s|,|$)/.test(v) ||          // PL: 65-138
                    /(?:^|[\s,])(?:D|DE)\s*-\s*\d{{5}}(?:\s|,|$)/i.test(v) || // D - 72531
                    /(?:^|[\s,])DE\d{{5}}(?:\s|,|$)/i.test(v) ||              // DE15711
                    /(?:^|[\s,])\d{{5}}(?:\s|,|$)/.test(v)                    // DE/other: 15711
                );
            }}

            function cleanBlockAddress(block) {{
                const rows = block.slice();

                // A leading company name is context, not its own stop.
                // Keep all following address rows together.
                let useful = rows;
                if (rows.length >= 2 && !isPostalEnd(rows[0]) &&
                    !/\d/.test(rows[0]) &&
                    !/\b(?:str(?:aße|asse)?|weg|platz|allee|gasse|ring|damm|chaussee|ufer|ul\.?|aleja|al\.?|plac|os\.?)\b/i.test(rows[0])) {{
                    useful = rows.slice(1);
                }}

                return useful.join(', ')
                    .replace(/\bD\s*-\s*(\d{{5}})\b/ig, '$1')
                    .replace(/\bDE\s*[- ]?\s*(\d{{5}})\b/ig, '$1')
                    .replace(/\s*,\s*/g, ', ')
                    .trim();
            }}

            const parsedAddresses = [];
            let block = [];

            simpleLines.forEach(function(line) {{
                block.push(line);

                if (isPostalEnd(line)) {{
                    const address = cleanBlockAddress(block);
                    if (address) parsedAddresses.push(address);
                    block = [];
                }}
            }});

            // Do not throw away a final one-line address lacking a postal code.
            if (block.length) {{
                const address = cleanBlockAddress(block);
                if (address) parsedAddresses.push(address);
            }}

            if (parsedAddresses.length >= 1) {{
                if (parsedAddresses.length > 24) {{
                    throw new Error('За один раз можна додати до 24 точок.');
                }}
                return parsedAddresses.map(function(address) {{
                    return {{
                        address: address,
                        date: null,
                        window_start: null,
                        window_end: null,
                        stop_type: null
                    }};
                }});
            }}
        }}

        if (!looksLikeOrder) {{
            // Одна адреса теж є повноцінним маршрутом: старт беремо з
            // поточної GPS-позиції вибраного автомобіля, а ця адреса є фінішем.
            if (simpleLines.length < 1) {{
                throw new Error('Вставте щонайменше одну адресу.');
            }}
            if (simpleLines.length > 24) {{
                throw new Error('За один раз можна додати до 24 точок.');
            }}
            return simpleLines.map(function(line, index) {{
                const parts = line.split('|').map(function(part) {{
                    return part.trim();
                }});
                const address = parts[0] || '';
                let stopDate = null;
                let windowStart = '';
                let windowEnd = '';
                if (parts.length === 4) {{
                    stopDate = normalizeDeliveryDate(parts[1]);
                    windowStart = parts[2] || '';
                    windowEnd = parts[3] || '';
                }} else {{
                    windowStart = parts[1] || '';
                    windowEnd = parts[2] || '';
                }}
                const hasAnyWindow = Boolean(windowStart || windowEnd);
                if (!address) {{
                    throw new Error('Рядок ' + (index + 1) + ': адреса порожня.');
                }}
                if (parts.length > 4 ||
                        (parts.length === 4 && !stopDate) ||
                        (hasAnyWindow &&
                        (!validTime.test(windowStart) || !validTime.test(windowEnd)))) {{
                    throw new Error(
                        'Рядок ' + (index + 1) +
                        ': використайте «адреса», «адреса | 08:00 | 10:00» або ' +
                        '«адреса | 25.09.2026 | 08:00 | 10:00».'
                    );
                }}
                return {{
                    address: address,
                    date: stopDate,
                    window_start: hasAnyWindow ? windowStart : null,
                    window_end: hasAnyWindow ? windowEnd : null,
                    stop_type: null
                }};
            }});
        }}

        // Транспортне завдання: кожна дата відкриває новий блок точки.
        // Це не дозволяє назві фірми, вулиці та індексу стати окремими точками.
        const blocks = [];
        let current = null;
        let pendingType = null;
        let sawLoading = false;
        let sawUnloading = false;

        simpleLines.forEach(function(originalLine) {{
            const lineType = stopTypeFromText(originalLine);
            if (lineType === 'loading') sawLoading = true;
            if (lineType === 'unloading') sawUnloading = true;

            // Якщо в одному заголовку одночасно написано Załadunek і Rozładunek
            // (типовий експорт замовлення), це заголовок, а не адреса.
            const lower = originalLine.toLowerCase();
            const hasBothTypes =
                /za[łl]adunek|loading|laden|beladung|завантаж|загрузка/.test(lower) &&
                /roz[łl]adunek|unloading|entladen|entladung|розвантаж|выгруз/.test(lower);
            if (hasBothTypes) {{
                return;
            }}

            let line = cleanLabel(originalLine);
            if (lineType && !line) {{
                pendingType = lineType;
                return;
            }}

            if (datePattern.test(line)) {{
                if (current && current.lines.length) blocks.push(current);
                current = {{lines: [line], stop_type: pendingType}};
                pendingType = null;
                return;
            }}

            if (!current) {{
                // Службові рядки до першої дати не є точками маршруту.
                if (lineType) pendingType = lineType;
                return;
            }}
            if (lineType && !current.stop_type) current.stop_type = lineType;
            if (line) current.lines.push(line);
        }});
        if (current && current.lines.length) blocks.push(current);

        let stops = blocks.map(function(block) {{
            return {{
                address: addressFromBlock(block.lines),
                date: normalizeDeliveryDate(block.lines[0]),
                window_start: null,
                window_end: null,
                stop_type: block.stop_type
            }};
        }}).filter(function(stop) {{ return Boolean(stop.address); }});

        // У багатьох заявках заголовок лише повідомляє, що є завантаження
        // і розвантаження, а тип не повторюється перед кожною адресою.
        // Тоді перша точка = завантаження, наступні = розвантаження.
        if (stops.length >= 2 && sawLoading && sawUnloading &&
                !stops.some(function(stop) {{ return Boolean(stop.stop_type); }})) {{
            stops = stops.map(function(stop, index) {{
                stop.stop_type = index === 0 ? 'loading' : 'unloading';
                return stop;
            }});
        }}

        if (stops.length < 1) {{
            throw new Error(
                'Не вдалося розпізнати адресу доставки. ' +
                'Перевірте, чи в заявці є адреса.'
            );
        }}
        if (stops.length > 24) {{
            throw new Error('За один раз можна додати до 24 точок.');
        }}
        return stops;
    }}

    function waitForGeocode(milliseconds) {{
        return new Promise(function(resolve) {{
            window.setTimeout(resolve, milliseconds);
        }});
    }}

    async function geocodeDeliveryStop(stop, index, total) {{
        let lastError = null;
        for (let attempt = 1; attempt <= 3; attempt += 1) {{
            const retryText = attempt > 1
                ? ' — повтор ' + attempt + '/3'
                : '';
            buildDeliveryRouteButton.textContent =
                'Шукаю адресу ' + (index + 1) + '/' + total +
                retryText + '...';

            const controller = new AbortController();
            const requestTimeout = window.setTimeout(function() {{
                controller.abort();
            }}, 25000);

            try {{
                const response = await fetch(
                    '/api/geocode?mode=address&q=' +
                    encodeURIComponent(stop.address) +
                    '&purpose=delivery&consent=addresses_only',
                    {{
                        headers: {{'Accept': 'application/json'}},
                        signal: controller.signal
                    }}
                );
                const data = await response.json();
                if (response.ok && data.results && data.results.length) {{
                    return Object.assign({{}}, stop, {{
                        latitude: Number(data.results[0].latitude),
                        longitude: Number(data.results[0].longitude),
                        map_name: data.results[0].name
                    }});
                }}
                lastError = new Error(
                    (data && data.error) || 'Адресу не знайдено.'
                );
            }} catch (error) {{
                lastError = error;
            }} finally {{
                window.clearTimeout(requestTimeout);
            }}

            if (attempt < 3) {{
                await waitForGeocode(1500 * attempt);
            }}
        }}

        throw new Error(
            'Не вдалося знайти адресу №' + (index + 1) + ': ' +
            stop.address + '. Спробуйте ще раз через хвилину.'
        );
    }}

    async function geocodeDeliveryStops(stops) {{
        const geocoded = [];
        for (let index = 0; index < stops.length; index += 1) {{
            const stop = stops[index];
            geocoded.push(await geocodeDeliveryStop(
                stop,
                index,
                stops.length
            ));
        }}
        return geocoded;
    }}

    function deliveryStopIcon(index, status) {{
        const normalizedStatus = [
            'pending',
            'current',
            'refused',
            'completed'
        ].includes(status) ? status : 'pending';
        return L.divIcon({{
            className: 'delivery-stop-icon',
            html: '<div class="delivery-stop-pin delivery-stop-' +
                normalizedStatus + '">' + index + '</div>',
            iconSize: [32, 32],
            iconAnchor: [16, 16],
            popupAnchor: [0, -18]
        }});
    }}

    function deliveryStatusLabel(status) {{
        if (gpsUiLanguage === 'pl') {{
            if (status === 'completed') return 'Rozładowano';
            if (status === 'refused') return 'Nie przyjęto · towar został w pojeździe';
            if (status === 'current') return 'Pojazd na rozładunku';
            return 'Jeszcze nie rozładowano';
        }}
        if (gpsUiLanguage === 'en') {{
            if (status === 'completed') return 'Unloaded';
            if (status === 'refused') return 'Not accepted · cargo remains in vehicle';
            if (status === 'current') return 'Vehicle at unloading';
            return 'Not unloaded yet';
        }}
        if (gpsUiLanguage === 'de') {{
            if (status === 'completed') return 'Entladen';
            if (status === 'refused') return 'Nicht angenommen · Ware bleibt im Fahrzeug';
            if (status === 'current') return 'Fahrzeug an der Entladestelle';
            return 'Noch nicht entladen';
        }}
        if (status === 'completed') return 'Розвантажено';
        if (status === 'refused') return 'Не прийняли · товар залишився в машині';
        if (status === 'current') return 'Автомобіль на розвантаженні';
        return 'Ще не розвантажено';
    }}

    function deliveryStopPopup(stop, index, status) {{
        const completed = status === 'completed';
        const manualCompleted = stop.manual_status === 'completed';
        const manualRefused = stop.manual_status === 'refused';
        const manualPending = stop.manual_status === 'pending';
        const labels = gpsUiLanguage === 'pl'
            ? ['✓ Rozładowano', '⚠ Nie przyjęto · towar w aucie', '✕ Nie rozładowano']
            : (gpsUiLanguage === 'en'
                ? ['✓ Unloaded', '⚠ Not accepted · cargo on board', '✕ Not unloaded']
                : (gpsUiLanguage === 'de'
                    ? ['✓ Entladen', '⚠ Nicht angenommen · Ware im Fahrzeug', '✕ Nicht entladen']
                    : ['✓ Розвантажено', '⚠ Не прийняли · товар у машині', '✕ Не розвантажено']));
        const doneLabel = labels[0];
        const refusedLabel = labels[1];
        const notDoneLabel = labels[2];
        return '<strong>Доставка ' + (index + 1) + '</strong><br>' +
            escapeHtml(stop.address) + '<br>' +
            (stop.window_start && stop.window_end
                ? stop.window_start + '–' + stop.window_end
                : 'Без часового вікна') +
            '<br><strong>' + escapeHtml(deliveryStatusLabel(status)) + '</strong>' +
            '<div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">' +
            '<button type="button" onclick="setDeliveryStopManualStatus(' + index + ',\\'completed\\')"' +
            (manualCompleted ? ' disabled' : '') + '>' + doneLabel + '</button>' +
            '<button type="button" onclick="setDeliveryStopManualStatus(' + index + ',\\'refused\\')"' +
            (manualRefused ? ' disabled' : '') + '>' + refusedLabel + '</button>' +
            '<button type="button" onclick="setDeliveryStopManualStatus(' + index + ',\\'pending\\')"' +
            (manualPending ? ' disabled' : '') + '>' + notDoneLabel + '</button>' +
            '</div>';
    }}

    async function setDeliveryStopManualStatus(index, status) {{
        if (!activeDeliveryRoute || !Array.isArray(activeDeliveryRoute.stops)) return;
        const stop = activeDeliveryRoute.stops[index];
        if (!stop || !['completed', 'refused', 'pending'].includes(status)) return;
        const previousStatus = stop.manual_status;
        stop.manual_status = status;

        try {{
            const saved = await readSavedDeliveryRoute(activeDeliveryRoute.vehicle_id);
            if (!saved || !saved.delivery_route) throw new Error('Маршрут не знайдено.');
            saved.delivery_route = activeDeliveryRoute;
            saved.saved_at = new Date().toISOString();
            await saveDeliveryRouteForVehicle(saved);
            lastServerRouteStampByVehicle[saved.vehicle_id] = serverRouteStamp(saved);
        }} catch (error) {{
            stop.manual_status = previousStatus;
            alert('Не вдалося зберегти статус точки. Спробуйте ще раз.');
            return;
        }}

        const vehicle = vehicles.find(function(item) {{
            return item.id === activeDeliveryRoute.vehicle_id;
        }});
        if (vehicle) {{
            await refreshDeliveryStopStatuses(vehicle, activeDeliveryRoute);
        }}
    }}
    window.setDeliveryStopManualStatus = setDeliveryStopManualStatus;

    async function refreshDeliveryStopStatuses(vehicle, deliveryRoute) {{
        if (!deliveryMarkers.length || !deliveryRoute.stops.length) {{
            return;
        }}
        try {{
            const response = await fetch('/api/delivery-stop-status', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{
                    vehicle_id: vehicle.id,
                    date: deliveryRoute.date,
                    stops: deliveryRoute.stops.map(function(stop) {{
                        return {{
                            latitude: stop.latitude,
                            longitude: stop.longitude,
                            manual_status: stop.manual_status || null
                        }};
                    }})
                }})
            }});
            const data = await response.json();
            if (!response.ok || !Array.isArray(data.statuses)) {{
                return;
            }}

            let autoCompletedChanged = false;
            data.statuses.forEach(function(status, index) {{
                const marker = deliveryMarkers[index];
                const stop = deliveryRoute.stops[index];
                if (!marker || !stop) return;

                // GPS completion is monotonic: after the vehicle has left a
                // confirmed unloading point, remember it in the saved route.
                // The user can still explicitly change it back with
                // "Nie rozładowano".
                if (
                    status === 'completed' &&
                    !stop.manual_status
                ) {{
                    stop.manual_status = 'completed';
                    autoCompletedChanged = true;
                }}

                marker.setIcon(deliveryStopIcon(index + 1, status));
                marker.setPopupContent(
                    deliveryStopPopup(stop, index, status)
                );
            }});

            if (autoCompletedChanged) {{
                try {{
                    const saved = await readSavedDeliveryRoute(vehicle.id);
                    if (saved && saved.delivery_route) {{
                        saved.delivery_route = deliveryRoute;
                        saved.saved_at = new Date().toISOString();
                        await saveDeliveryRouteForVehicle(saved);
                        lastServerRouteStampByVehicle[vehicle.id] =
                            serverRouteStamp(saved);
                    }}
                }} catch (error) {{
                    // The map stays usable even if this one persistence write fails.
                }}
            }}

            await refreshVehicleDeliveryPopup(
                vehicle,
                deliveryRoute,
                data.statuses
            );
            // Arrival at the last stop is not proof of unloading. Keep the
            // route until the driver or dispatcher explicitly finishes it.
        }} catch (error) {{
            // Статуси не повинні ламати сам маршрут.
        }}
    }}

    let deliveryRouteSaveMode = 'active';

    async function queueDeliveryRouteForVehicle(savedRoute) {{
        const response = await fetch(
            '/api/delivery-route/' + encodeURIComponent(savedRoute.vehicle_id) + '/queue',
            {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{route: savedRoute}})
            }}
        );
        const data = await response.json();
        if (!response.ok) {{
            throw new Error(data.error || 'Не вдалося додати наступний маршрут.');
        }}
        return data;
    }}

    function buildNextDeliveryRoute() {{
        deliveryRouteSaveMode = 'queue';
        buildDeliveryRoute();
    }}
    window.buildNextDeliveryRoute = buildNextDeliveryRoute;

    async function buildDeliveryRoute() {{
        const requestedSaveMode = deliveryRouteSaveMode;
        deliveryRouteSaveMode = 'active';
        if (!deliveryMapConsent.checked) {{
            measureResult.textContent =
                'Потрібне підтвердження передачі адрес карті.';
            return;
        }}
        let parsedStops;
        try {{
            parsedStops = parseDeliveryStopLines();
        }} catch (error) {{
            measureResult.textContent = error.message;
            return;
        }}

        if (!deliveryRouteDate.value) {{
            measureResult.textContent = 'Виберіть дату доставок.';
            return;
        }}

        const vehicle = vehicles.find(function(item) {{
            return item.id === vehicleSelect.value;
        }});
        if (!vehicle) {{
            measureResult.textContent =
                'Для автомобіля немає актуальної GPS-позиції.';
            return;
        }}

        vehicleSelect.value = vehicle.id;
        updateFuelConsumption();
        removeMeasurementLayers();
        removePlannedRoute();
        measureMode = false;
        measureButton.classList.remove('active');
        buildDeliveryRouteButton.disabled = true;
        if (queueDeliveryRouteButton) queueDeliveryRouteButton.disabled = true;
        buildDeliveryRouteButton.textContent =
            'Готую ' + parsedStops.length + ' точок...';
        measureResult.textContent =
            'Будую розвізний маршрут від поточної позиції ' +
            vehicle.name + '...';

        const avoidTolls = selectedRouteAvoidsTolls();
        const vehicleProfile = selectedVehicleProfile();

        try {{
            const daySummaryPromise = loadVehicleDaySummary(vehicle.id);
            const stops = await geocodeDeliveryStops(parsedStops);
            const deliveryRoute = {{
                label: (gpsUiLanguage === 'pl' ? 'Trasa dostaw ' : (gpsUiLanguage === 'de' ? 'Ausliefertour ' : 'Розвізка ')) + deliveryRouteDate.value,
                vehicle_id: vehicle.id,
                date: deliveryRouteDate.value,
                stops: stops
            }};
            activeDeliveryRoute = deliveryRoute;
            renderDeliveryStopOrder();
            const destination = stops[stops.length - 1];
            const waypoints = stops.slice(0, -1).map(function(stop) {{
                return {{
                    latitude: stop.latitude,
                    longitude: stop.longitude
                }};
            }});

            drawDeliveryStopMarkers(deliveryRoute);

            await refreshDeliveryStopStatuses(vehicle, deliveryRoute);
            deliveryStatusTimer = window.setInterval(function() {{
                refreshDeliveryStopStatuses(vehicle, deliveryRoute);
            }}, 60000);

            buildDeliveryRouteButton.textContent =
                'Будую маршрут через усі точки...';
            const response = await fetch('/api/route', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{
                    origin: {{
                        latitude: vehicle.latitude,
                        longitude: vehicle.longitude
                    }},
                    destination: {{
                        latitude: destination.latitude,
                        longitude: destination.longitude
                    }},
                    waypoints: waypoints,
                    avoid_tolls: avoidTolls,
                    vehicle_profile: vehicleProfile
                }})
            }});
            const routeData = await response.json();

            if (!response.ok) {{
                throw new Error(
                    routeData.error || 'Розвізний маршрут недоступний.'
                );
            }}
            if (!routeData.points || !routeData.points.length) {{
                throw new Error('Маршрут через усі точки не знайдено.');
            }}

            // КРИТИЧНО: зберігаємо маршрут одразу після успішного розрахунку.
            // Раніше запис був лише в самому кінці великого блоку аналізу;
            // будь-яка помилка після малювання карти залишала на екрані новий
            // маршрут, але після Reload повертався старий або порожній.
            const earlySavedRoute = {{
                vehicle_id: vehicle.id,
                delivery_route: deliveryRoute,
                route_data: routeData,
                input_text: deliveryStopsInput.value,
                service_minutes: Math.max(
                    5,
                    Number(deliveryServiceMinutes.value) || 25
                ),
                daily_rest_hours: Math.max(
                    9,
                    Math.min(11, Number(deliveryDailyRestHours.value) || 11)
                ),
                vehicle_profile: vehicleProfile,
                summary_html: '',
                saved_at: new Date().toISOString()
            }};
            // localStorage записується всередині функції ДО запиту на сервер.
            // Навіть якщо сервер тимчасово недоступний, Reload має відновити
            // останній маршрут із цього браузера.
            if (requestedSaveMode === 'active') {{
                try {{
                    await saveDeliveryRouteForVehicle(earlySavedRoute);
                }} catch (saveError) {{
                    console.warn('Маршрут збережено локально; серверний запис не вдався.', saveError);
                }}
            }}

            buildDeliveryRouteButton.textContent =
                'Аналізую сьогоднішню роботу і паузу...';
            const daySummary = await daySummaryPromise;

            plannedRouteLayer = L.polyline(routeData.points, {{
                color: '#087f8c',
                weight: 6,
                opacity: .9
            }}).addTo(map);
            plannedRouteLayer._tranviqDeliveryOverlay = true;
            map.fitBounds(
                plannedRouteLayer.getBounds(),
                {{padding: [45, 45]}}
            );

            const serviceMinutes = Math.max(
                5,
                Number(deliveryServiceMinutes.value) || 25
            );
            const navirecMinDailyRest = numberOrNull(
                vehicle.min_daily_rest_s
            );
            const dailyRestHours = navirecMinDailyRest !== null &&
                navirecMinDailyRest >= 9 * 3600 &&
                navirecMinDailyRest <= 11 * 3600
                ? navirecMinDailyRest / 3600
                : Math.max(
                    9,
                    Math.min(
                        11,
                        Number(deliveryDailyRestHours.value) || 11
                    )
                );
            const schedule = calculateDeliverySchedule(
                vehicle,
                deliveryRoute,
                routeData,
                serviceMinutes,
                dailyRestHours,
                daySummary
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
            const fuelLitres = distanceKm * fuelConsumption / 100;
            const fuelCost = fuelLitres * fuelPrice;
            const polishUi = gpsUiLanguage === 'pl';
            const routeUi = function(uk, pl, en, de) {{
                if (gpsUiLanguage === 'pl') return pl;
                if (gpsUiLanguage === 'en') return en;
                if (gpsUiLanguage === 'de') return de;
                return uk;
            }};
            const pauseLabel = schedule.pause_type === 'daily_rest'
                ? (polishUi ? 'długi odpoczynek dobowy' : 'довгий добовий відпочинок')
                : (schedule.pause_type === 'break_45'
                    ? (polishUi ? 'przerwa co najmniej 45 min' : 'перерва щонайменше 45 хвилин')
                    : (polishUi ? 'krótki lub zwykły postój' : 'коротка або звичайна стоянка'));
            const feasibilityClass = schedule.late_count
                ? 'error'
                : (schedule.has_tachograph || schedule.rest_before_start
                    ? 'ok'
                    : 'warning');
            const feasibilityTitle = schedule.late_count
                ? routeUi(
                    'Є ризик запізнення: ' + schedule.late_count + ' розвантажень поза часовим вікном.',
                    'Ryzyko opóźnienia: ' + schedule.late_count + ' rozładunków poza oknem czasowym.',
                    'Risk of delay: ' + schedule.late_count + ' unloadings outside the time window.',
                    'Verspätungsrisiko: ' + schedule.late_count + ' Entladungen außerhalb des Zeitfensters.'
                )
                : (schedule.has_tachograph
                    ? (polishUi ? 'Trasa jest zgodna z aktualnymi danymi tachografu.' : 'Маршрут узгоджено з актуальним тахографом.')
                    : (schedule.rest_before_start
                        ? (polishUi
                            ? 'Do wyjazdu postój z wyłączonym zapłonem został uwzględniony jako szacunkowa przerwa. Po uruchomieniu pojazdu należy zweryfikować ją z danymi tachografu.'
                            : 'До виїзду враховано стоянку з вимкненим запалюванням як розрахункову паузу. Після запуску звірити з тахографом.')
                        : (polishUi
                            ? 'Trasa została obliczona, ale tachograf nie podał pełnego pozostałego czasu jazdy.'
                            : 'Маршрут розраховано, але тахограф не дав повного залишку часу.')));

            const stopRows = schedule.stops.map(function(stop, index) {{
                let note = '';
                if (stop.wait_seconds >= 60) {{
                    note += routeUi(' · очікування ', ' · oczekiwanie ', ' · waiting ', ' · Wartezeit ') +
                        formatDuration(stop.wait_seconds);
                }}
                if (stop.late) {{
                    note += routeUi(
                        ' · <strong>ЗАПІЗНЕННЯ</strong>',
                        ' · <strong>OPÓŹNIENIE</strong>',
                        ' · <strong>DELAY</strong>',
                        ' · <strong>VERSPÄTUNG</strong>'
                    );
                }}
                return '<li><strong>' +
                    formatDateTime(stop.service_start) + '</strong> — ' +
                    escapeHtml(stop.address) +
                    (stop.window_start && stop.window_end
                        ? ' (' + stop.window_start + '–' + stop.window_end + ')'
                        : routeUi(' (без часового вікна)', ' (bez okna czasowego)', ' (no time window)', ' (ohne Zeitfenster)')) +
                    '<br><span class="small">' + routeUi('виїзд ', 'wyjazd ', 'departure ', 'Abfahrt ') +
                    formatDateTime(stop.departure) +
                    routeUi(', від попередньої точки ', ', od poprzedniego punktu ', ', from previous stop ', ', vom vorherigen Stopp ') +
                    (stop.distance_m / 1000).toFixed(1) +
                    ' km' + note + '</span>' +
                    '<br><span style="display:inline-flex;gap:6px;margin-top:5px">' +
                    '<button type="button" title="' + routeUi('Підняти точку','Przesuń punkt w górę','Move stop up','Stopp nach oben') + '" ' +
                    'onclick="reorderActiveDeliveryStops(' + index + ', -1)" ' +
                    (index === 0 ? 'disabled ' : '') + '>↑</button>' +
                    '<button type="button" title="' + routeUi('Опустити точку','Przesuń punkt w dół','Move stop down','Stopp nach unten') + '" ' +
                    'onclick="reorderActiveDeliveryStops(' + index + ', 1)" ' +
                    (index === schedule.stops.length - 1 ? 'disabled ' : '') +
                    '>↓</button></span></li>';
            }}).join('');

            measureResult.innerHTML =
                '<strong>' + escapeHtml(polishUi && deliveryRoute.label.indexOf('Розвізка') === 0 ? deliveryRoute.label.replace('Розвізка', 'Trasa dostaw') : deliveryRoute.label) + '</strong>' +
                (polishUi ? '<br>Pojazd: <strong>' : '<br>Автомобіль: <strong>') +
                escapeHtml(vehicle.name) + '</strong>' +
                (polishUi ? '<br>Kierowca: <strong>' : '<br>Водій: <strong>') +
                escapeHtml(vehicle.driver_name || (polishUi ? 'nie określono' : 'не визначено')) +
                '</strong>' +
                (polishUi ? '<br>Odległość: <strong>' : '<br>Відстань: <strong>') +
                distanceKm.toFixed(1) + ' km</strong>' +
                (polishUi ? '<br>Czysty czas jazdy: ' : '<br>Чистий час керування: ') +
                formatDuration(routeData.duration_s) +
                (polishUi ? '<br>Planowany wyjazd: <strong>' : '<br>Планований виїзд: <strong>') +
                formatDateTime(schedule.route_start) + '</strong>' +
                (schedule.first_movement_at
                    ? (polishUi ? '<br>Początek dzisiejszej pracy: ' : '<br>Початок сьогоднішньої роботи: ') +
                        '<strong>' +
                        formatDateTime(schedule.first_movement_at) +
                        '</strong>'
                    : '') +
                (schedule.previous_distance_km !== null
                    ? (polishUi ? '<br>Dzisiaj już przejechano: <strong>' : '<br>Сьогодні вже пройдено: <strong>') +
                        schedule.previous_distance_km.toFixed(1) +
                        (polishUi ? ' km</strong>; jazda: ' : ' км</strong>; керування: ') +
                        formatDuration(schedule.previous_driving_s)
                    : '') +
                (schedule.parking_rest_s > 0
                    ? (polishUi ? '<br>Postój przed wyjazdem: <strong>' : '<br>Стоянка до виїзду: <strong>') +
                        formatDuration(schedule.parking_rest_s) +
                        '</strong> — ' + pauseLabel +
                        (schedule.rest_before_start
                            ? (polishUi ? '; odpoczynek dobowy zaliczony' : '; добову паузу набрано')
                            : (polishUi ? '; pełny odpoczynek dobowy nie został jeszcze osiągnięty' : '; повну добову паузу ще не набрано'))
                    : '') +
                (polishUi ? '<br>Paliwo: <strong>' : '<br>Паливо: <strong>') + fuelLitres.toFixed(1) +
                (polishUi ? ' l ≈ ' : ' л ≈ ') + fuelCost.toFixed(2) + ' ' +
                fuelCurrencySelect.value + '</strong>' +
                (polishUi ? '<br>Przerwy 45 min: ' : '<br>Перерв 45 хв: ') + schedule.break_count +
                (polishUi ? '; odpoczynki dobowe: ' : '; добових відпочинків: ') +
                schedule.daily_rest_count +
                (polishUi ? '<br><strong>Fizycznie wolny: ' : '<br><strong>Фізично вільний: ') +
                formatDateTime(schedule.free_at) + '</strong>' +
                (polishUi ? '<br><strong>Następny załadunek można planować: ' : '<br><strong>Наступне завантаження можна планувати: ') +
                formatDateTime(schedule.free_at) + '</strong>' +
                (polishUi ? '<br><strong>Zalecany następny wyjazd: ' : '<br><strong>Рекомендований наступний виїзд: ') +
                formatDateTime(schedule.next_safe_start) + '</strong>' +
                '<br><span class="small">' +
                escapeHtml(schedule.next_recommendation) + '</span>' +
                '<div class="route-feasibility ' +
                feasibilityClass + '"><strong>' +
                escapeHtml(feasibilityTitle) + '</strong></div>' +
                '<ol class="route-stop-list">' + stopRows + '</ol>' +
                '<br><strong>' +
                escapeHtml(formatTollInformation(routeData)) +
                '</strong>' +
                (polishUi ? '<br><span class="small">Przyjęto czas rozładunku: ' : (gpsUiLanguage === 'de' ? '<br><span class="small">Für die Entladung wurden ' : '<br><span class="small">Розвантаження прийнято по ')) +
                serviceMinutes + (polishUi ? ' min na punkt. Po wykonaniu trasy ' : (gpsUiLanguage === 'de' ? ' Min. pro Stopp eingeplant. Nach Abschluss der Tour ' : ' хв на точку. Після виконання рейсу ')) +
                (polishUi ? 'porównamy prognozę z rzeczywistym czasem i skorygujemy normę.</span>' : (gpsUiLanguage === 'de' ? 'vergleichen wir die Prognose mit den tatsächlichen Werten und passen die Vorgabe entsprechend an.</span>' : 'порівняємо прогноз із фактом і скоригуємо норматив.</span>'));

            measureResult.innerHTML = localizeSavedRouteSummary(measureResult.innerHTML);

            const completedSavedRoute = {{
                vehicle_id: vehicle.id,
                delivery_route: deliveryRoute,
                route_data: routeData,
                input_text: deliveryStopsInput.value,
                service_minutes: serviceMinutes,
                daily_rest_hours: dailyRestHours,
                vehicle_profile: vehicleProfile,
                summary_html: measureResult.innerHTML,
                saved_at: new Date().toISOString()
            }};
            if (requestedSaveMode === 'queue') {{
                buildDeliveryRouteButton.textContent = 'Додаю наступний маршрут...';
                await queueDeliveryRouteForVehicle(completedSavedRoute);
                measureResult.innerHTML +=
                    '<div style="margin-top:10px;padding:10px;border:1px solid #9cc7a5;' +
                    'border-radius:8px;background:#f1fff4;font-weight:800;">' +
                    (gpsUiLanguage === 'pl'
                        ? '✓ Dodano jako następną trasę. Aktualna trasa nie została zastąpiona.'
                        : '✓ Додано як наступний маршрут. Поточний маршрут не замінено.') +
                    '</div>';
            }} else {{
                buildDeliveryRouteButton.textContent =
                    'Зберігаю активний маршрут...';
                await saveDeliveryRouteForVehicle(completedSavedRoute);
            }}
        }} catch (error) {{
            measureResult.textContent =
                error.message ||
                'Не вдалося прорахувати розвізний маршрут.';
        }} finally {{
            buildDeliveryRouteButton.disabled =
                !deliveryMapConsent.checked ||
                !deliveryStopsInput.value.trim();
            if (queueDeliveryRouteButton) {{
                queueDeliveryRouteButton.disabled =
                    !deliveryMapConsent.checked || !deliveryStopsInput.value.trim();
            }}
            buildDeliveryRouteButton.textContent =
                'Прорахувати всі доставки';
        }}
    }}

    window.setTimeout(function() {{
        if (vehicleSelect.value) {{
            restoreDeliveryRouteForVehicle(vehicleSelect.value);
        }}
    }}, 0);

    cityInput.addEventListener('keydown', function(event) {{
        if (event.key === 'Enter') {{
            event.preventDefault();
            window.clearTimeout(citySearchTimer);
            searchCity();
        }}
    }});
    cityInput.addEventListener('input', function() {{
        window.clearTimeout(citySearchTimer);
        citySearchRequest += 1;
        selectedCity = null;
        selectedDestination = null;
        destinationInput.value = '';
        destinationInput.disabled = true;
        destinationInput.placeholder = 'Спочатку виберіть місто';
        addressSearchButton.disabled = true;
        addressResults.hidden = true;
        buildRouteButton.disabled = true;

        const query = cityInput.value.trim();
        if (!query) {{
            cityResults.hidden = true;
            cityResults.replaceChildren();
            return;
        }}

        cityResults.hidden = false;
        if (query.length < 3) {{
            cityResults.textContent =
                'Введіть ще ' + (3 - query.length) +
                ' символ(и), і з’являться міста.';
            return;
        }}

        cityResults.textContent = 'Шукаю міста...';
        citySearchTimer = window.setTimeout(function() {{
            searchCity();
        }}, 500);
    }});

    addressSearchButton.addEventListener('click', searchAddress);
    destinationInput.addEventListener('keydown', function(event) {{
        if (event.key === 'Enter') {{
            event.preventDefault();
            window.clearTimeout(addressSearchTimer);
            searchAddress();
        }}
    }});
    destinationInput.addEventListener('input', function() {{
        window.clearTimeout(addressSearchTimer);
        addressSearchRequest += 1;

        const query = destinationInput.value.trim();
        if (!query) {{
            selectedDestination = selectedCity;
            buildRouteButton.disabled = !selectedCity || !vehicles.length;
            addressResults.hidden = true;
            addressResults.replaceChildren();
            return;
        }}

        selectedDestination = null;
        buildRouteButton.disabled = true;

        addressResults.hidden = false;
        if (query.length < 3) {{
            addressResults.textContent =
                'Введіть ще ' + (3 - query.length) +
                ' символ(и), і з’являться підказки.';
            return;
        }}

        addressResults.textContent = 'Шукаю варіанти...';
        addressSearchTimer = window.setTimeout(function() {{
            searchAddress();
        }}, 500);
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
        ui_lang=json.dumps(current_language()),
        user_role=json.dumps(current_role()),
        lat=center_lat,
        lon=center_lon
    )

    if current_language() == "pl":
        # GPS JavaScript is rendered server-side. Force every Polish branch
        # before sending the page to the browser, so dynamic route results
        # cannot fall back to Ukrainian.
        gps_pl_replacements = {
            "Планування маршруту": "Planowanie trasy",
            "Початок маршруту — автомобіль": "Początek trasy — pojazd",
            "Тип транспорту": "Typ pojazdu",
            "Бус до 3,5 т": "Bus do 3,5 t",
            "Вантажний до 7,5 т": "Ciężarowy do 7,5 t",
            "Вантажний до 12 т": "Ciężarowy do 12 t",
            "Вантажний до 18 т": "Ciężarowy do 18 t",
            "Вантажний до 26 т": "Ciężarowy do 26 t",
            "Фура до 40 т": "Zestaw do 40 t",
            "Понад 40 т": "Powyżej 40 t",
            "Витрата, л/100 км": "Spalanie, l/100 km",
            "Ціна за літр": "Cena za litr",
            "Місто": "Miasto",
            "Почніть вводити назву міста": "Zacznij wpisywać nazwę miasta",
            "Спочатку виберіть місто": "Najpierw wybierz miasto",
            "Вулиця / адреса": "Ulica / adres",
            "Знайти адресу": "Znajdź adres",
            "Побудувати маршрут": "Wyznacz trasę",
            "Адреси й часові вікна": "Adresy i okna czasowe",
            "Дозволяю передати картографічним сервісам лише адреси цього маршруту": "Zezwalam na przekazanie serwisom mapowym wyłącznie adresów tej trasy",
            "Дозволяю передати картографічним сервісам": "Zezwalam na przekazanie serwisom mapowym",
            "лише адреси цього маршруту": "wyłącznie adresów tej trasy",
            "Для карти використовуються лише адреси й часові": "Do mapy używane są wyłącznie adresy i okna czasowe.",
            "вікна. Імена та телефони не передаються.": "Imiona i numery telefonów nie są przekazywane.",
            "Вартість є орієнтовною. Вона залежить від ваги,": "Koszt jest orientacyjny. Zależy od masy,",
            "осей, екологічного класу, віньєт і способу оплати.": "liczby osi, klasy emisji, winiet i sposobu płatności.",
            "Для карти використовуються лише адреси й часові вікна. Імена та телефони не передаються.": "Do mapy używane są wyłącznie adresy i okna czasowe. Imiona i numery telefonów nie są przekazywane.",
            "Вартість є орієнтовною. Вона залежить від ваги, осей, екологічного класу, віньєт і способу оплати.": "Koszt jest orientacyjny. Zależy od masy, liczby osi, klasy emisji, winiet i sposobu płatności.",
            "Змінити порядок адрес": "Zmień kolejność adresów",
            "Очистити всі адреси": "Wyczyść wszystkie adresy",
            "Видалити маршрут автомобіля": "Usuń trasę pojazdu",
            "Дата доставок": "Data dostaw",
            "Хв на точку": "Min na punkt",
            "Добовий відпочинок": "Odpoczynek dobowy",
            "Odpoczynek dobowy, год": "Odpoczynek dobowy, godz.",
            "Прорахувати всі доставки": "Oblicz wszystkie dostawy",
            "Передавати адреси карті": "Przekazuj adresy do mapy",
            "Виміряти маршрут": "Zmierz trasę",
            "Очистити маршрут": "Wyczyść trasę",
            "Їде": "Jedzie",
            "Заведена": "Silnik włączony",
            "Стоїть": "Stoi",
            "Статус:": "Status:",
            "Швидкість:": "Prędkość:",
            "Паливо:": "Paliwo:",
            "До наступної вигрузки:": "Do następnego rozładunku:",
            "До останньої вигрузки:": "Do ostatniego rozładunku:",
            "Доставка ": "Dostawa ",
            "Без часового вікна": "Bez okna czasowego",
            "Ще не вигружено": "Jeszcze nierozładowane",
            "Для цього автомобіля активного розвізного маршруту немає.": "Dla tego pojazdu nie ma aktywnej trasy dostaw.",
            "Вставте адреси або текст транспортного завдання.": "Wklej adresy lub tekst zlecenia transportowego.",
            "Виберіть дату доставок.": "Wybierz datę dostaw.",
            "Для автомобіля немає актуальної GPS-позиції.": "Brak aktualnej pozycji GPS pojazdu.",
            "Потрібне підтвердження передачі адрес карті.": "Wymagana jest zgoda na przekazanie adresów do mapy.",
            "Будую розвізний маршрут від поточної позиції ": "Wyznaczam trasę dostaw od aktualnej pozycji ",
            "Готую ": "Przygotowuję ",
            " точок...": " punktów...",
            "Будую маршрут через усі точки...": "Wyznaczam trasę przez wszystkie punkty...",
            "Зберігаю активний маршрут...": "Zapisuję aktywną trasę...",
            "Відстань:": "Odległość:",
            "Чистий час керування:": "Czysty czas jazdy:",
            "Планований виїзд:": "Planowany wyjazd:",
            "Сьогодні вже пройдено:": "Dzisiaj już przejechano:",
            "керування:": "jazda:",
            "Паливо: даних немає": "Paliwo: brak danych",
            "Перерв 45 хв:": "Przerw 45 min:",
            "добових відпочинків:": "odpoczynków dobowych:",
            "Фізично вільний:": "Fizycznie wolny:",
            "Наступне завантаження можна планувати:": "Następny załadunek można planować:",
            "Рекомендований наступний виїзд:": "Zalecany następny wyjazd:",
            "Оплата доріг: даних про платні ділянки немає.": "Opłaty drogowe: brak danych o płatnych odcinkach.",
            "Орієнтовна оплата доріг:": "Szacunkowe opłaty drogowe:",
            "Спочатку виберіть місто": "Najpierw wybierz miasto",
            "Шукаю міста...": "Szukam miast...",
            "Клікніть першу точку на карті.": "Kliknij pierwszy punkt na mapie.",
            "Тепер клікніть другу точку.": "Teraz kliknij drugi punkt.",
            "Будую автомобільний маршрут...": "Wyznaczam trasę samochodową...",
            "Дорогами:": "Drogami:",
            "Приблизний час:": "Przybliżony czas:",
            "По прямій:": "W linii prostej:",
            "Автомобільний маршрут зараз недоступний.": "Trasa samochodowa jest teraz niedostępna.",
            " год ": " godz. ",
            " хв": " min"
        }
        for source_text, target_text in gps_pl_replacements.items():
            body = replace_visible_gps_text(body, source_text, target_text)


    if current_language() == "en":
        gps_en_visible_replacements = {
            "Змінити порядок адрес": "Reorder addresses",
            "Добовий відпочинок, год": "Daily rest, hours",
            "Дозволяю передати картографічним сервісам": "I allow the addresses of this route to be sent to map services",
            "лише адреси цього маршруту": "",
            "Для карти використовуються лише адреси й часові": "Only addresses and time windows are used for the map.",
            "вікна. Імена та телефони не передаються.": "Names and phone numbers are not shared.",
            "Вартість є орієнтовною. Вона залежить від ваги,": "The cost is an estimate. It depends on weight,",
            "осей, екологічного класу, віньєт і способу оплати.": "number of axles, emission class, vignettes and payment method.",
        }
        for source_text, target_text in gps_en_visible_replacements.items():
            body = replace_visible_gps_text(body, source_text, target_text)


    if current_language() == "de":
        # Mirror the proven Polish visible-text handling for German.
        # These source strings are split across HTML lines, so full-phrase
        # replacements alone do not catch them.
        gps_de_visible_replacements = {
            "Добовий відпочинок, год": "Tägliche Ruhezeit, Std.",
            "Tägliche Ruhezeit, год": "Tägliche Ruhezeit, Std.",
            "Дозволяю передати картографічним сервісам": "Ich erlaube die Übermittlung an Kartendienste",
            "лише адреси цього маршруту": "ausschließlich der Adressen dieser Route",
            "Для карти використовуються лише адреси й часові": "Für die Karte werden ausschließlich Adressen und Zeitfenster verwendet.",
            "вікна. Імена та телефони не передаються.": "Namen und Telefonnummern werden nicht übermittelt.",
            "Вартість є орієнтовною. Вона залежить від ваги,": "Die Kostenangabe ist unverbindlich. Sie hängt von Gewicht,",
            "осей, екологічного класу, віньєт і способу оплати.": "Achszahl, Emissionsklasse, Vignetten und Zahlungsart ab.",
        }
        for source_text, target_text in gps_de_visible_replacements.items():
            body = replace_visible_gps_text(body, source_text, target_text)


    # GPS / route-planning localization. Keep all dynamic route text in the
    # same language as the selected interface language.
    gps_extra_translations = {
        "pl": {
            "Валюта": "Waluta", "Варіант маршруту": "Wariant trasy",
            "Швидкий": "Szybki", "Платні дороги дозволені": "Drogi płatne dozwolone",
            "Безплатний": "Bezpłatny", "Уникати платних доріг": "Unikaj dróg płatnych",
            "Вулиця або точна адреса": "Ulica lub dokładny adres", "Шукати": "Szukaj",
            "Прокласти маршрут": "Wyznacz trasę", "Розвізний маршрут": "Trasa dostaw",
            "Розвантаження, min": "Rozładunek, min", 
            "Дозволяю передати картографічним сервісам лише адреси цього маршруту": "Zezwalam na przekazanie usługom mapowym wyłącznie adresów tej trasy",
            "Для карти використовуються лише адреси й часові вікна. Імена та телефони не передаються.": "Do mapy używane są wyłącznie adresy i okna czasowe. Imiona i numery telefonów nie są przekazywane.",
            "Вартість є орієнтовною. Вона залежить від ваги, осей, екологічного класу, віньєт і способу оплати.": "Koszt jest orientacyjny. Zależy od masy, liczby osi, klasy emisji, winiet i sposobu płatności.",
            "Очистити карту": "Wyczyść mapę", "Розвізка": "Trasa dostaw",
            "Автомобіль:": "Pojazd:", "Водій:": "Kierowca:",
            "Початок сьогоднішньої роботи:": "Początek dzisiejszej pracy:",
            "Сьогодні вже пройдено:": "Dzisiaj już przejechano:", "керування:": "jazda:",
            "Перерв 45 хв:": "Przerw 45 min:", "добових відпочинків:": "odpoczynków dobowych:",
            "Після завершення залишається щонайменше": "Po zakończeniu pozostaje co najmniej",
            "керування.": "jazdy.", "Маршрут узгоджено з актуальним тахографом.": "Trasa jest zgodna z aktualnymi danymi tachografu.",
            "без часового вікна": "bez okna czasowego", "виїзд": "wyjazd",
            "від попередньої точки": "od poprzedniego punktu", "км": "km"
        },
        "en": {
            "Планування маршруту": "Route planning", "Початок маршруту — автомобіль": "Route start — vehicle",
            "Тип транспорту": "Vehicle type", "Тип автомобіля": "Vehicle type", "Бус до 3,5 т": "Van up to 3.5 t",
            "Вантажний до 7,5 т": "Truck up to 7.5 t", "Вантажний до 12 т": "Truck up to 12 t",
            "Вантажний до 18 т": "Truck up to 18 t", "Вантажний до 26 т": "Truck up to 26 t",
            "Фура до 40 т": "Combination up to 40 t", "Понад 40 т": "Over 40 t",
            "Витрата, л/100 км": "Fuel consumption, l/100 km", "Ціна за літр": "Price per litre", "Валюта": "Currency",
            "Варіант маршруту": "Route option", "Швидкий": "Fast", "Платні дороги дозволені": "Toll roads allowed",
            "Безплатний": "Toll-free", "Уникати платних доріг": "Avoid toll roads", "Місто": "City",
            "Вулиця або точна адреса": "Street or exact address", "Вулиця / адреса": "Street / address",
            "Шукати": "Search", "Знайти адресу": "Find address", "Прокласти маршрут": "Plan route",
            "Побудувати маршрут": "Plan route", "Розвізний маршрут": "Delivery route", "Дата доставок": "Delivery date",
            "Адреси й часові вікна": "Addresses and time windows", "Змінити порядок адрес": "Change address order",
            "Очистити всі адреси": "Clear all addresses", "Видалити маршрут автомобіля": "Delete vehicle route",
            "Розвантаження, min": "Unloading, min", "Хв на точку": "Min per stop", "Добовий відпочинок": "Daily rest",
            "Дозволяю передати картографічним сервісам лише адреси цього маршруту": "I allow only the addresses of this route to be sent to map services",
            "Прорахувати всі доставки": "Calculate all deliveries", "Виміряти маршрут": "Measure route", "Очистити карту": "Clear map",
            "Розвізка": "Delivery route", "Автомобіль:": "Vehicle:", "Водій:": "Driver:", "Відстань:": "Distance:",
            "Чистий час керування:": "Pure driving time:", "Планований виїзд:": "Planned departure:",
            "Початок сьогоднішньої роботи:": "Start of today's work:", "Сьогодні вже пройдено:": "Distance today:",
            "керування:": "driving:", "Паливо:": "Fuel:", "Перерв 45 хв:": "45 min breaks:",
            "добових відпочинків:": "daily rests:", "Фізично вільний:": "Physically available:",
            "Наступне завантаження можна планувати:": "Next loading can be planned:",
            "Рекомендований наступний виїзд:": "Recommended next departure:",
            "Маршрут узгоджено з актуальним тахографом.": "Route is consistent with current tachograph data.",
            "без часового вікна": "no time window", "виїзд": "departure", "від попередньої точки": "from previous stop",
            "До наступної вигрузки:": "To next unloading:", "До останньої вигрузки:": "To final unloading:",
            "Їде": "Driving", "Заведена": "Engine on", "Стоїть": "Stopped", "Статус:": "Status:", "Швидкість:": "Speed:",
            " год ": " h ", " хв": " min"
        },
        "de": {
            "Планування маршруту": "Routenplanung", "Початок маршруту — автомобіль": "Routenstart — Fahrzeug",
            "Тип транспорту": "Fahrzeugtyp", "Тип автомобіля": "Fahrzeugtyp", "Бус до 3,5 т": "Transporter bis 3,5 t",
            "Вантажний до 7,5 т": "Lkw bis 7,5 t", "Вантажний до 12 т": "Lkw bis 12 t",
            "Вантажний до 18 т": "Lkw bis 18 t", "Вантажний до 26 т": "Lkw bis 26 t",
            "Фура до 40 т": "Sattelzug bis 40 t", "Понад 40 т": "Über 40 t",
            "Витрата, л/100 км": "Verbrauch, l/100 km", "Ціна за літр": "Preis pro Liter", "Валюта": "Währung",
            "Варіант маршруту": "Routenvariante", "Швидкий": "Schnell", "Платні дороги дозволені": "Mautstraßen erlaubt",
            "Безплатний": "Mautfrei", "Уникати платних доріг": "Mautstraßen vermeiden", "Місто": "Stadt",
            "Вулиця або точна адреса": "Straße oder genaue Adresse", "Вулиця / адреса": "Straße / Adresse",
            "Почніть вводити назву міста": "Stadtnamen eingeben", "Спочатку виберіть місто": "Zuerst eine Stadt auswählen",
            "Шукати": "Suchen", "Знайти адресу": "Adresse suchen", "Прокласти маршрут": "Route planen",
            "Побудувати маршрут": "Route planen", "Розвізний маршрут": "Ausliefertour", "Дата доставок": "Lieferdatum",
            "Адреси й часові вікна": "Adressen und Zeitfenster", "Змінити порядок адрес": "Adressreihenfolge ändern",
            "Очистити всі адреси": "Alle Adressen löschen", "Видалити маршрут автомобіля": "Fahrzeugroute löschen",
            "Розвантаження, min": "Entladezeit, Min.", "Розвантаження, хв": "Entladezeit, Min.", "Хв на точку": "Min. pro Stopp", "Добовий відпочинок": "Tägliche Ruhezeit",
            "Дозволяю передати картографічним сервісам лише адреси цього маршруту": "Ich erlaube, nur die Adressen dieser Route an Kartendienste zu übermitteln",
            "Прорахувати всі доставки": "Alle Lieferungen berechnen", "Виміряти маршрут": "Route messen", "Очистити карту": "Karte leeren",
            "Розвізка": "Ausliefertour", "Автомобіль:": "Fahrzeug:", "Водій:": "Fahrer:", "Відстань:": "Entfernung:",
            "Чистий час керування:": "Reine Fahrzeit:", "Планований виїзд:": "Geplante Abfahrt:",
            "Початок сьогоднішньої роботи:": "Beginn der heutigen Arbeit:", "Сьогодні вже пройдено:": "Heute bereits gefahren:",
            "керування:": "Fahrzeit:", "Паливо:": "Kraftstoff:", "Перерв 45 хв:": "45-Min.-Pausen:",
            "добових відпочинків:": "tägliche Ruhezeiten:", "Фізично вільний:": "Physisch verfügbar:",
            "Наступне завантаження можна планувати:": "Nächste Beladung planbar:",
            "Рекомендований наступний виїзд:": "Empfohlene nächste Abfahrt:",
            "Маршрут узгоджено з актуальним тахографом.": "Route stimmt mit den aktuellen Tachographendaten überein.",
            "без часового вікна": "ohne Zeitfenster", "виїзд": "Abfahrt", "від попередньої точки": "vom vorherigen Stopp",
            "До наступної вигрузки:": "Bis zur nächsten Entladung:", "До останньої вигрузки:": "Bis zur letzten Entladung:",
            "Їде": "Fährt", "Заведена": "Motor an", "Стоїть": "Steht", "Статус:": "Status:", "Швидкість:": "Geschwindigkeit:",
            " год ": " Std. ", " хв": " Min."
        }
    }
    lang = current_language()
    if lang in gps_extra_translations:
        # Replace longer phrases first so short words cannot damage them.
        for source_text, target_text in sorted(gps_extra_translations[lang].items(), key=lambda item: len(item[0]), reverse=True):
            body = replace_visible_gps_text(body, source_text, target_text)

    # Translate text that is created later by JavaScript (route results,
    # toll explanations, tachograph summaries, popups). Static replacements
    # above cannot see those DOM nodes because they do not exist yet.
    gps_dynamic_i18n = {
        "pl": {
            "Дозволяю передати картографічним сервісам лише адреси цього маршруту": "Zezwalam na przekazanie usługom mapowym wyłącznie adresów tej trasy",
            "Для карти використовуються лише адреси й часові вікна. Імена та телефони не передаються.": "Do mapy używane są wyłącznie adresy i okna czasowe. Imiona i numery telefonów nie są przekazywane.",
            "Вартість є орієнтовною. Вона залежить від ваги, осей, екологічного класу, віньєт і способу оплати.": "Koszt jest orientacyjny. Zależy od masy, liczby osi, klasy emisji, winiet i sposobu płatności.",
            "Розвізка": "Trasa dostaw", "Автомобіль:": "Pojazd:", "Водій:": "Kierowca:",
            "Відстань:": "Odległość:", "Чистий час керування:": "Czysty czas jazdy:",
            "Планований виїзд:": "Planowany wyjazd:", "Початок сьогоднішньої роботи:": "Początek dzisiejszej pracy:",
            "Сьогодні вже пройдено:": "Dzisiaj już przejechano:", "керування:": "jazda:",
            "Паливо:": "Paliwo:", "Перерв 45 хв:": "Przerw 45 min:", "добових відпочинків:": "odpoczynków dobowych:",
            "Фізично вільний:": "Fizycznie wolny:", "Наступне завантаження можна планувати:": "Następny załadunek można planować:",
            "Рекомендований наступний виїзд:": "Zalecany następny wyjazd:",
            "Після завершення залишається щонайменше": "Po zakończeniu pozostaje co najmniej",
            "Маршрут узгоджено з актуальним тахографом.": "Trasa jest zgodna z aktualnymi danymi tachografu.",
            "без часового вікна": "bez okna czasowego", "виїзд": "wyjazd", "від попередньої точки": "od poprzedniego punktu",
            "Орієнтовна оплата доріг:": "Szacunkowe opłaty drogowe:", "Орієнтовна оплата": "Szacunkowa opłata",
            "Оплата доріг:": "Opłaty drogowe:", "платних ділянок": "płatnych odcinków",
            "Дорогами:": "Drogami:", "Приблизний час:": "Przybliżony czas:", "По прямій:": "W linii prostej:",
            "До наступної вигрузки:": "Do następnego rozładunku:", "До останньої вигрузки:": "Do ostatniego rozładunku:",
            "Дозволяю передати картографічним сервісам": "Zezwalam na przekazanie usługom mapowym",
            "лише адреси цього маршруту": "wyłącznie adresów tej trasy",
            "Для карти використовуються лише адреси й часові вікна.": "Do mapy używane są wyłącznie adresy i okna czasowe.",
            "Імена та телефони не передаються.": "Imiona i numery telefonów nie są przekazywane.",
            "Вартість є орієнтовною.": "Koszt jest orientacyjny.",
            "Вона залежить від ваги, осей, екологічного класу, віньєт і способу оплати.": "Zależy od masy, liczby osi, klasy emisji, winiet i sposobu płatności.",
            "керування.": "jazdy.", "керування": "jazdy", " год ": " godz. ", " км": " km", " л ": " l ", "хв": "min",
            "Перерв 45 min:": "Przerw 45 min:", "Після завершення залишається щонайменше": "Po zakończeniu pozostaje co najmniej",
            "Szacunkowa opłata A2: ≈ 10 PLN (67.9 км платною": "Szacunkowa opłata A2: ≈ 10 PLN (67.9 km odcinka płatnego"
        },
        "en": {
            "Дозволяю передати картографічним сервісам лише адреси цього маршруту": "I allow only the addresses of this route to be sent to map services",
            "Для карти використовуються лише адреси й часові вікна. Імена та телефони не передаються.": "Only addresses and time windows are used for the map. Names and phone numbers are not sent.",
            "Вартість є орієнтовною. Вона залежить від ваги, осей, екологічного класу, віньєт і способу оплати.": "The cost is an estimate. It depends on weight, axles, emission class, vignettes and payment method.",
            "Розвізка": "Delivery route", "Автомобіль:": "Vehicle:", "Водій:": "Driver:", "Відстань:": "Distance:",
            "Чистий час керування:": "Pure driving time:", "Планований виїзд:": "Planned departure:",
            "Початок сьогоднішньої роботи:": "Start of today's work:", "Сьогодні вже пройдено:": "Distance today:",
            "керування:": "driving:", "Паливо:": "Fuel:", "Перерв 45 хв:": "45 min breaks:", "добових відпочинків:": "daily rests:",
            "Фізично вільний:": "Physically available:", "Наступне завантаження можна планувати:": "Next loading can be planned:",
            "Рекомендований наступний виїзд:": "Recommended next departure:",
            "Після завершення залишається щонайменше": "After completion at least",
            "Маршрут узгоджено з актуальним тахографом.": "Route is consistent with current tachograph data.",
            "без часового вікна": "no time window", "виїзд": "departure", "від попередньої точки": "from previous stop",
            "Орієнтовна оплата доріг:": "Estimated road tolls:", "Орієнтовна оплата": "Estimated toll",
            "Оплата доріг:": "Road tolls:", "Дорогами:": "By road:", "Приблизний час:": "Approximate time:", "По прямій:": "Straight line:",
            "До наступної вигрузки:": "To next unloading:", "До останньої вигрузки:": "To final unloading:",
            "Дозволяю передати картографічним сервісам": "I allow addresses to be sent to map services",
            "лише адреси цього маршруту": "for this route only",
            "Для карти використовуються лише адреси й часові вікна.": "Only addresses and time windows are used for the map.",
            "Імена та телефони не передаються.": "Names and phone numbers are not sent.",
            "Вартість є орієнтовною.": "The cost is an estimate.",
            "Вона залежить від ваги, осей, екологічного класу, віньєт і способу оплати.": "It depends on weight, axles, emission class, vignettes and payment method.",
            "керування.": "driving.",  " л ": " l ",  "хв": "min"
        },
        "de": {
            "Дозволяю передати картографічним сервісам лише адреси цього маршруту": "Ich erlaube, nur die Adressen dieser Route an Kartendienste zu übermitteln",
            "Для карти використовуються лише адреси й часові вікна. Імена та телефони не передаються.": "Für die Karte werden nur Adressen und Zeitfenster verwendet. Namen und Telefonnummern werden nicht übermittelt.",
            "Вартість є орієнтовною. Вона залежить від ваги, осей, екологічного класу, віньєт і способу оплати.": "Die Kosten sind geschätzt. Sie hängen von Gewicht, Achsen, Emissionsklasse, Vignetten und Zahlungsart ab.",
            "Розвізка": "Ausliefertour", "Автомобіль:": "Fahrzeug:", "Водій:": "Fahrer:", "Відстань:": "Entfernung:",
            "Чистий час керування:": "Reine Fahrzeit:", "Планований виїзд:": "Geplante Abfahrt:",
            "Початок сьогоднішньої роботи:": "Beginn der heutigen Arbeit:", "Сьогодні вже пройдено:": "Heute bereits gefahren:",
            "керування:": "Fahrzeit:", "Паливо:": "Kraftstoff:", "Перерв 45 хв:": "45-Min.-Pausen:", "добових відпочинків:": "tägliche Ruhezeiten:",
            "Фізично вільний:": "Physisch verfügbar:", "Наступне завантаження можна планувати:": "Nächste Beladung planbar:",
            "Рекомендований наступний виїзд:": "Empfohlene nächste Abfahrt:",
            "Після завершення залишається щонайменше": "Nach Abschluss verbleiben mindestens",
            "Маршрут узгоджено з актуальним тахографом.": "Route stimmt mit den aktuellen Tachographendaten überein.",
            "без часового вікна": "ohne Zeitfenster", "виїзд": "Abfahrt", "від попередньої точки": "vom vorherigen Stopp",
            "Орієнтовна оплата доріг:": "Geschätzte Mautkosten:", "Орієнтовна оплата": "Geschätzte Maut",
            "Оплата доріг:": "Mautkosten:", "Дорогами:": "Auf der Straße:", "Приблизний час:": "Ungefähre Zeit:", "По прямій:": "Luftlinie:",
            "До наступної вигрузки:": "Bis zur nächsten Entladung:", "До останньої вигрузки:": "Bis zur letzten Entladung:",
            "Дозволяю передати картографічним сервісам": "Ich erlaube die Übermittlung von Adressen an Kartendienste",
            "лише адреси цього маршруту": "nur für diese Route",
            "Для карти використовуються лише адреси й часові вікна.": "Für die Karte werden nur Adressen und Zeitfenster verwendet.",
            "Імена та телефони не передаються.": "Namen und Telefonnummern werden nicht übermittelt.",
            "Вартість є орієнтовною.": "Die Kostenangabe ist unverbindlich.",
            "Тут поки показується поточний рівень палива з Navirec. Збільшення рівня ще не вважаємо автоматично заправкою.": "Derzeit wird der aktuelle Kraftstoffstand von Navirec angezeigt. Ein Anstieg des Kraftstoffstands wird noch nicht automatisch als Tankvorgang erkannt.",
            "Вона залежить від ваги, осей, екологічного класу, віньєт і способу оплати.": "Sie hängt von Gewicht, Achszahl, Emissionsklasse, Vignetten und Zahlungsart ab.",
            "Для карти використовуються лише адреси й часові": "Für die Karte werden ausschließlich Adressen und Zeitfenster verwendet.",
            "вікна. Імена та телефони не передаються.": "Namen und Telefonnummern werden nicht übermittelt.",
            "Вартість є орієнтовною. Вона залежить від ваги,": "Die Kostenangabe ist unverbindlich. Sie hängt von Gewicht,",
            "осей, екологічного класу, віньєт і способу оплати.": "Achszahl, Emissionsklasse, Vignetten und Zahlungsart ab.",
            "Tägliche Ruhezeit, год": "Tägliche Ruhezeit, Std.",
            "Добовий відпочинок, год": "Tägliche Ruhezeit, Std.",
            " год": " Std.",
            "керування.": "Fahrzeit.",  " л ": " l ",  "хв": "Min."
        }
    }

    gps_dynamic_map = gps_dynamic_i18n.get(lang, {})
    if gps_dynamic_map:
        body += """
<script>
(function () {
    const translations = __GPS_DYNAMIC_I18N__;
    const entries = Object.entries(translations)
        .sort((a, b) => b[0].length - a[0].length);

    function translateText(value) {
        let result = value || '';
        for (const [source, target] of entries) {
            if (result.includes(source)) {
                result = result.split(source).join(target);
            }
        }
        return result;
    }

    function translateNode(root) {
        if (!root) return;

        if (root.nodeType === Node.TEXT_NODE) {
            const parent = root.parentElement;
            if (!parent || /^(SCRIPT|STYLE|TEXTAREA)$/i.test(parent.tagName)) return;
            const translated = translateText(root.nodeValue);
            if (translated !== root.nodeValue) root.nodeValue = translated;
            return;
        }

        if (root.nodeType !== Node.ELEMENT_NODE && root.nodeType !== Node.DOCUMENT_NODE) return;

        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        let node;
        while ((node = walker.nextNode())) {
            const parent = node.parentElement;
            if (!parent || /^(SCRIPT|STYLE|TEXTAREA)$/i.test(parent.tagName)) continue;
            const translated = translateText(node.nodeValue);
            if (translated !== node.nodeValue) node.nodeValue = translated;
        }

        if (root.querySelectorAll) {
            root.querySelectorAll('[placeholder],[title],[aria-label]').forEach((el) => {
                ['placeholder', 'title', 'aria-label'].forEach((attr) => {
                    if (!el.hasAttribute(attr)) return;
                    const before = el.getAttribute(attr) || '';
                    const after = translateText(before);
                    if (after !== before) el.setAttribute(attr, after);
                });
            });
        }
    }

    function startDynamicTranslation() {
        translateNode(document.body);
        const observer = new MutationObserver((mutations) => {
            for (const mutation of mutations) {
                if (mutation.type === 'characterData') {
                    translateNode(mutation.target);
                }
                for (const node of mutation.addedNodes || []) {
                    translateNode(node);
                }
            }
        });
        observer.observe(document.body, {
            subtree: true,
            childList: true,
            characterData: true
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', startDynamicTranslation, {once: true});
    } else {
        startDynamicTranslation();
    }
})();
</script>
""".replace(
            "__GPS_DYNAMIC_I18N__",
            json.dumps(gps_dynamic_map, ensure_ascii=False)
        )

    return page(
        "GPS",
        body,
        "gps"
    )


@app.route("/history")
def history():
    if not VEHICLES: return page("Історія маршрутів", "<div class=card><p>Додайте автомобілі, щоб відкрити історію.</p><a href=/company/vehicles>Додати автомобіль</a></div>", "history")
    selected_id = normalize_vehicle_id(
        request.args.get("vehicle", "")
    )

    if not selected_id:
        selected_id = VEHICLES[0]["id"]

    vehicle = vehicle_by_id(selected_id)

    if not vehicle:
        return Response("Автомобіль не знайдено.", status=404)

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

        lang = current_language()

        if speed is not None:
            speed_unit = "km/h" if lang in {"en", "de", "pl"} else "км/год"
            speed_text = format_number(speed, 0) + " " + speed_unit
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

    lang = current_language()
    vehicle_labels = {
        "uk": {"speed": "Швидкість", "fuel": "Паливо", "heading": "Напрямок", "engine": "Оберти двигуна", "distance": "Загальна відстань", "ignition": "Запалювання", "history": "Історія маршруту"},
        "pl": {"speed": "Prędkość", "fuel": "Paliwo", "heading": "Kierunek", "engine": "Obroty silnika", "distance": "Całkowity przebieg", "ignition": "Zapłon", "history": "Historia trasy"},
        "en": {"speed": "Speed", "fuel": "Fuel", "heading": "Heading", "engine": "Engine RPM", "distance": "Total distance", "ignition": "Ignition", "history": "Route history"},
        "de": {"speed": "Geschwindigkeit", "fuel": "Kraftstoff", "heading": "Fahrtrichtung", "engine": "Motordrehzahl", "distance": "Gesamtstrecke", "ignition": "Zündung", "history": "Routenverlauf"},
    }.get(lang, {})

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

    const streetLayer = L.tileLayer(
        'https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
        {{
            maxZoom: 19,
            attribution: '&copy; OpenStreetMap'
        }}
    );

    const satelliteLayer = L.tileLayer(
        'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}',
        {{
            maxZoom: 19,
            attribution: 'Tiles &copy; Esri'
        }}
    );

    const terrainLayer = L.tileLayer(
        'https://{{s}}.tile.opentopomap.org/{{z}}/{{x}}/{{y}}.png',
        {{
            maxZoom: 17,
            attribution: 'Map data &copy; OpenStreetMap contributors, SRTM | Map style &copy; OpenTopoMap'
        }}
    );

    const baseMaps = {{
        'Карта': streetLayer,
        'Супутник': satelliteLayer,
        'Рельєф': terrainLayer
    }};

    let savedMapLayer = 'Карта';
    try {{
        savedMapLayer = localStorage.getItem('oo_map_layer') || 'Карта';
    }} catch (e) {{}}

    const initialLayer = baseMaps[savedMapLayer] || streetLayer;
    initialLayer.addTo(map);
    const layerControl = L.control.layers(
        baseMaps,
        null,
        {{position: 'topright', collapsed: true}}
    ).addTo(map);
    layerControl.getContainer().style.marginTop = '42px';

    (function(control) {{
        const lang = (document.documentElement.lang || 'uk').toLowerCase().slice(0, 2);
        const names = {{
            uk: ['Карта', 'Супутник', 'Рельєф'],
            pl: ['Mapa', 'Satelita', 'Teren'],
            en: ['Map', 'Satellite', 'Terrain'],
            de: ['Karte', 'Satellit', 'Gelände']
        }}[lang] || ['Карта', 'Супутник', 'Рельєф'];

        const labels = control.getContainer().querySelectorAll(
            '.leaflet-control-layers-base label'
        );
        labels.forEach(function(label, index) {{
            const spans = label.querySelectorAll('span');
            const target = spans.length ? spans[spans.length - 1] : null;
            if (target && names[index]) {{
                target.textContent = ' ' + names[index];
            }}
        }});
    }})(layerControl);

    map.on('baselayerchange', function(event) {{
        try {{
            localStorage.setItem('oo_map_layer', event.name);
        }} catch (e) {{}}
    }});

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


@app.route("/fuel", methods=["GET", "POST"])
def fuel():
    states = get_vehicle_states()
    state_map = state_map_by_vehicle(states)
    lang = current_language()

    labels = {
        "uk": {
            "title": "Паливо", "vehicle": "Автомобіль", "level": "Рівень",
            "liters": "У баку", "avg": "Середня витрата", "round": "Поточний круг",
            "distance": "Км на крузі", "used": "Пального на круг", "started": "Старт",
            "capacity": "Бак, л", "save": "Зберегти", "reset": "Новий круг / скинути на 0",
            "calibration": "Калібровка по заправці", "actual": "Фактично з чека, л",
            "navirec": "Navirec показав, л", "date": "Дата заправки",
            "add": "Додати калібровку", "factor": "Поправка", "samples": "заправок",
            "no_capacity": "Вкажіть об’єм бака, щоб бачити залишок у літрах.",
            "note": "Витрата на 100 км береться з Navirec. Після калібровок за чеками TRANVIQ автоматично застосовує поправку окремо для кожної машини.",
            "saved": "Збережено.", "bad": "Перевірте введені дані.",
            "no_distance": "Navirec поки не повернув загальний пробіг для цієї машини.",
            "history": "Останні калібровки"
        },
        "pl": {
            "title": "Paliwo", "vehicle": "Pojazd", "level": "Poziom",
            "liters": "W baku", "avg": "Średnie spalanie", "round": "Bieżący cykl",
            "distance": "Km w cyklu", "used": "Paliwo w cyklu", "started": "Start",
            "capacity": "Zbiornik, l", "save": "Zapisz", "reset": "Nowy cykl / wyzeruj",
            "calibration": "Kalibracja tankowania", "actual": "Faktycznie z dokumentu, l",
            "navirec": "Navirec pokazał, l", "date": "Data tankowania",
            "add": "Dodaj kalibrację", "factor": "Korekta", "samples": "tankowań",
            "no_capacity": "Wpisz pojemność zbiornika, aby widzieć paliwo w litrach.",
            "note": "Spalanie l/100 km pochodzi z Navirec. Po kalibracji dokumentami TRANVIQ automatycznie stosuje korektę osobno dla każdego pojazdu.",
            "saved": "Zapisano.", "bad": "Sprawdź wprowadzone dane.",
            "no_distance": "Navirec nie zwrócił jeszcze całkowitego przebiegu tego pojazdu.",
            "history": "Ostatnie kalibracje"
        },
        "en": {
            "title": "Fuel", "vehicle": "Vehicle", "level": "Level",
            "liters": "In tank", "avg": "Average consumption", "round": "Current round",
            "distance": "Round distance", "used": "Fuel used on round", "started": "Started",
            "capacity": "Tank, l", "save": "Save", "reset": "New round / reset to 0",
            "calibration": "Refuelling calibration", "actual": "Actual from receipt, l",
            "navirec": "Navirec showed, l", "date": "Refuelling date",
            "add": "Add calibration", "factor": "Correction", "samples": "refuellings",
            "no_capacity": "Enter tank capacity to show remaining fuel in litres.",
            "note": "Consumption per 100 km comes from Navirec. After receipt calibrations TRANVIQ automatically applies a per-vehicle correction.",
            "saved": "Saved.", "bad": "Check the entered values.",
            "no_distance": "Navirec has not returned total distance for this vehicle yet.",
            "history": "Recent calibrations"
        },
        "de": {
            "title": "Kraftstoff", "vehicle": "Fahrzeug", "level": "Füllstand",
            "liters": "Im Tank", "avg": "Durchschnittsverbrauch", "round": "Aktuelle Runde",
            "distance": "Km in der Runde", "used": "Kraftstoff in der Runde", "started": "Start",
            "capacity": "Tank, l", "save": "Speichern", "reset": "Neue Runde / auf 0 setzen",
            "calibration": "Tankkalibrierung", "actual": "Tatsächlich laut Beleg, l",
            "navirec": "Navirec zeigte, l", "date": "Tankdatum",
            "add": "Kalibrierung hinzufügen", "factor": "Korrektur", "samples": "Tankvorgänge",
            "no_capacity": "Tankvolumen eingeben, um den Rest in Litern anzuzeigen.",
            "note": "Der Verbrauch pro 100 km kommt aus Navirec. Nach Beleg-Kalibrierungen wendet TRANVIQ automatisch eine fahrzeugspezifische Korrektur an.",
            "saved": "Gespeichert.", "bad": "Bitte Eingaben prüfen.",
            "no_distance": "Navirec hat für dieses Fahrzeug noch keinen Gesamt-km-Stand geliefert.",
            "history": "Letzte Kalibrierungen"
        }
    }.get(lang, {})
    message = ""

    if request.method == "POST":
        vehicle_id = normalize_vehicle_id(request.form.get("vehicle_id", ""))
        vehicle = vehicle_by_id(vehicle_id)
        action = str(request.form.get("action") or "").strip()
        if not vehicle:
            message = labels["bad"]
        else:
            state = state_map.get(vehicle_id) or {}
            with FUEL_TRACKING_LOCK:
                tracking = _load_fuel_tracking()
                item = tracking.setdefault(vehicle_id, {})

                if action == "save_capacity":
                    capacity = safe_float(request.form.get("tank_capacity_l"))
                    if capacity is None or capacity < 20 or capacity > 2000:
                        message = labels["bad"]
                    else:
                        item["tank_capacity_l"] = round(capacity, 2)
                        _write_fuel_tracking(tracking)
                        message = labels["saved"]

                elif action == "start_round":
                    distance_m = safe_float(state.get("total_distance"))
                    fuel_pct = safe_float(state.get("fuel_level"))
                    if distance_m is None:
                        message = labels["no_distance"]
                    else:
                        item["round"] = {
                            "started_at": datetime.now(timezone.utc).isoformat(),
                            "start_total_distance_m": distance_m,
                            "start_fuel_pct": fuel_pct
                        }
                        _write_fuel_tracking(tracking)
                        message = labels["saved"]

                elif action == "add_calibration":
                    actual_liters = safe_float(request.form.get("actual_liters"))
                    navirec_liters = safe_float(request.form.get("navirec_liters"))
                    date_value = str(request.form.get("fuel_date") or "").strip()
                    if (
                        actual_liters is None or navirec_liters is None
                        or actual_liters <= 0 or navirec_liters <= 0
                        or actual_liters > 2000 or navirec_liters > 2000
                    ):
                        message = labels["bad"]
                    else:
                        rows = item.setdefault("calibrations", [])
                        rows.append({
                            "date": date_value,
                            "actual_liters": round(actual_liters, 2),
                            "navirec_liters": round(navirec_liters, 2),
                            "created_at": datetime.now(timezone.utc).isoformat()
                        })
                        item["calibrations"] = rows[-50:]
                        _write_fuel_tracking(tracking)
                        message = labels["saved"]

    with FUEL_TRACKING_LOCK:
        tracking = _load_fuel_tracking()

    cards = []
    for vehicle in VEHICLES:
        vehicle_id = vehicle["id"]
        state = state_map.get(vehicle_id) or {}
        fuel_pct = safe_float(state.get("fuel_level"))
        total_distance_m = safe_float(state.get("total_distance"))
        item = tracking.get(vehicle_id, {}) if isinstance(tracking.get(vehicle_id, {}), dict) else {}
        capacity = safe_float(item.get("tank_capacity_l"))
        factor = _fuel_calibration_factor(item)
        raw_consumption = get_vehicle_average_consumption(vehicle_id)
        corrected_consumption = raw_consumption * factor if raw_consumption is not None else None

        current_liters = (
            capacity * fuel_pct / 100.0
            if capacity is not None and fuel_pct is not None
            else None
        )

        round_data = item.get("round") if isinstance(item.get("round"), dict) else None
        round_km = None
        round_liters = None
        if round_data and total_distance_m is not None:
            start_distance = safe_float(round_data.get("start_total_distance_m"))
            if start_distance is not None and total_distance_m >= start_distance:
                round_km = (total_distance_m - start_distance) / 1000.0
                if corrected_consumption is not None:
                    round_liters = round_km * corrected_consumption / 100.0

        started_text = "—"
        if round_data:
            started = parse_time(round_data.get("started_at"))
            if started:
                try:
                    started_text = started.astimezone(POLAND_TZ).strftime("%d.%m.%Y %H:%M")
                except Exception:
                    started_text = str(round_data.get("started_at") or "—")

        calibration_rows = []
        for row in reversed(item.get("calibrations", [])[-5:]):
            if not isinstance(row, dict):
                continue
            actual = safe_float(row.get("actual_liters"))
            nav = safe_float(row.get("navirec_liters"))
            ratio = actual / nav if actual and nav else None
            calibration_rows.append(
                "<tr><td>{date}</td><td>{actual}</td><td>{nav}</td><td>{ratio}</td></tr>".format(
                    date=escape(str(row.get("date") or "—")),
                    actual=(format_number(actual, 2) + " l") if actual is not None else "—",
                    nav=(format_number(nav, 2) + " l") if nav is not None else "—",
                    ratio=(format_number(ratio, 3) + "×") if ratio is not None else "—"
                )
            )

        fuel_pct_text = format_number(fuel_pct, 1) + "%" if fuel_pct is not None else "—"
        liters_text = format_number(current_liters, 1) + " l" if current_liters is not None else "—"
        consumption_text = (
            format_number(corrected_consumption, 2) + " l/100 km"
            if corrected_consumption is not None else "—"
        )
        round_km_text = format_number(round_km, 1) + " km" if round_km is not None else "—"
        round_liters_text = format_number(round_liters, 1) + " l" if round_liters is not None else "—"
        calibration_count = len(item.get("calibrations", []))
        factor_text = (
            format_number(factor, 3) + "× · " + str(calibration_count) + " " + labels["samples"]
            if calibration_count else "1.000× · 0 " + labels["samples"]
        )
        capacity_value = "" if capacity is None else str(round(capacity, 2))

        no_capacity_note = (
            '<div class="small" style="margin-top:6px;color:#a15c00">'
            + escape(labels["no_capacity"]) + '</div>'
            if capacity is None else ""
        )
        history_html = (
            '<details style="margin-top:12px"><summary><strong>' + escape(labels["history"]) + '</strong></summary>'
            '<div style="overflow:auto;margin-top:8px"><table><thead><tr><th>'
            + escape(labels["date"]) + '</th><th>' + escape(labels["actual"]) + '</th><th>'
            + escape(labels["navirec"]) + '</th><th>' + escape(labels["factor"]) + '</th></tr></thead><tbody>'
            + "".join(calibration_rows) + '</tbody></table></div></details>'
            if calibration_rows else ""
        )

        cards.append("""
        <div class="card">
          <h2>{vehicle}</h2>
          <div class="grid">
            <div class="stat"><div class="label">{level}</div><div class="value">{fuel_pct}</div></div>
            <div class="stat"><div class="label">{liters}</div><div class="value">{liters_value}</div></div>
            <div class="stat"><div class="label">{avg}</div><div class="value">{consumption}</div></div>
            <div class="stat"><div class="label">{factor}</div><div class="value">{factor_value}</div></div>
            <div class="stat"><div class="label">{distance}</div><div class="value">{round_km}</div></div>
            <div class="stat"><div class="label">{used}</div><div class="value">{round_liters}</div></div>
          </div>
          <p class="small">{started}: <strong>{started_value}</strong></p>
          <form method="post" style="display:flex;gap:8px;flex-wrap:wrap;align-items:end;margin-top:10px">
            <input type="hidden" name="vehicle_id" value="{vehicle_id}">
            <label>{capacity}<input type="number" name="tank_capacity_l" min="20" max="2000" step="0.1" value="{capacity_value}" style="max-width:130px"></label>
            <button name="action" value="save_capacity" type="submit">{save}</button>
            <button name="action" value="start_round" type="submit">{reset}</button>
          </form>
          {no_capacity_note}
          <details style="margin-top:12px">
            <summary><strong>{calibration}</strong></summary>
            <form method="post" style="display:flex;gap:8px;flex-wrap:wrap;align-items:end;margin-top:10px">
              <input type="hidden" name="vehicle_id" value="{vehicle_id}">
              <label>{date}<input type="date" name="fuel_date"></label>
              <label>{actual}<input type="number" name="actual_liters" min="0.1" max="2000" step="0.01"></label>
              <label>{navirec}<input type="number" name="navirec_liters" min="0.1" max="2000" step="0.01"></label>
              <button name="action" value="add_calibration" type="submit">{add}</button>
            </form>
            {history_html}
          </details>
        </div>
        """.format(
            vehicle=escape(vehicle.get("plate") or vehicle["name"]),
            level=escape(labels["level"]), liters=escape(labels["liters"]), avg=escape(labels["avg"]),
            factor=escape(labels["factor"]), distance=escape(labels["distance"]), used=escape(labels["used"]),
            fuel_pct=fuel_pct_text, liters_value=liters_text, consumption=consumption_text,
            factor_value=factor_text, round_km=round_km_text, round_liters=round_liters_text,
            started=escape(labels["started"]), started_value=escape(started_text),
            vehicle_id=escape(str(vehicle_id)), capacity=escape(labels["capacity"]),
            capacity_value=escape(capacity_value), save=escape(labels["save"]), reset=escape(labels["reset"]),
            no_capacity_note=no_capacity_note, calibration=escape(labels["calibration"]), date=escape(labels["date"]),
            actual=escape(labels["actual"]), navirec=escape(labels["navirec"]), add=escape(labels["add"]),
            history_html=history_html
        ))

    body = """
    <div class="card">
      <p>{note}</p>
      {message}
    </div>
    {cards}
    """.format(
        note=escape(labels["note"]),
        message=('<p class="alert alert-ok">' + escape(message) + '</p>') if message else "",
        cards="".join(cards)
    )

    return page(labels["title"], body, "fuel")


@app.route("/tachograph")
@app.route("/tachograph-test")
def tachograph():

    vehicle_states = get_vehicle_states()

    if vehicle_states:
        with _LAST_GOOD_TACHO_LOCK:
            tenancy.cache("tacho")["states"] = list(vehicle_states)
            tenancy.cache("tacho")["at"] = time.time()
    else:
        with _LAST_GOOD_TACHO_LOCK:
            if (
                tenancy.cache("tacho").get("states", [])
                and time.time() - tenancy.cache("tacho").get("at", 0) <= NAVIREC_LIST_STALE_MAX
            ):
                vehicle_states = list(tenancy.cache("tacho").get("states", []))

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
        iq_balance = build_driver_iq_balance(combined)
        min_daily_rest = iq_balance["min_daily_rest_s"]
        min_weekly_rest = iq_balance["min_weekly_rest_s"]
        open_weekly_compensation = iq_balance["open_weekly_compensation_s"]
        weekly_rest_with_compensation = iq_balance[
            "weekly_rest_plus_all_compensation_s"
        ]

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

                <div class="alert alert-ok" style="margin-top:16px">
                    <strong>TRANVIQ IQ — баланс відпочинку</strong>
                    <div class="detail-grid" style="margin-top:10px">
                        <div class="detail">
                            <div class="label">Мінімальний добовий відпочинок зараз</div>
                            <div class="value">{iq_min_daily}</div>
                        </div>
                        <div class="detail">
                            <div class="label">Мінімальний тижневий відпочинок Navirec</div>
                            <div class="value">{iq_min_weekly}</div>
                        </div>
                        <div class="detail">
                            <div class="label">Відкрита компенсація тижневого відпочинку</div>
                            <div class="value">{iq_compensation}</div>
                        </div>
                        <div class="detail">
                            <div class="label">45 год + весь відкритий борг</div>
                            <div class="value">{iq_long_rest}</div>
                        </div>
                        <div class="detail">
                            <div class="label">Кінець останнього добового відпочинку</div>
                            <div class="value">{iq_last_daily}</div>
                        </div>
                        <div class="detail">
                            <div class="label">Кінець останнього тижневого відпочинку</div>
                            <div class="value">{iq_last_weekly}</div>
                        </div>
                    </div>
                    <div class="small" style="margin-top:9px">
                        IQ використовує тільки підтверджені поля Navirec.
                        Лічильник використаних 9-годинних добових відпочинків
                        додамо до постійної пам’яті TRANVIQ, щоб він не губився
                        після перезапуску сервера.
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
                iq_min_daily=format_duration_short(min_daily_rest),
                iq_min_weekly=format_duration_short(min_weekly_rest),
                iq_compensation=format_duration_short(open_weekly_compensation),
                iq_long_rest=format_duration_short(weekly_rest_with_compensation),
                iq_last_daily=html_text(format_time(iq_balance["last_daily_rest_end"])),
                iq_last_weekly=html_text(format_time(iq_balance["last_weekly_rest_end"])),
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

    lang = current_language()
    vehicle_labels = {
        "uk": {"speed": "Швидкість", "fuel": "Паливо", "heading": "Напрямок", "engine": "Оберти двигуна", "distance": "Загальна відстань", "ignition": "Запалювання", "history": "Історія маршруту"},
        "pl": {"speed": "Prędkość", "fuel": "Paliwo", "heading": "Kierunek", "engine": "Obroty silnika", "distance": "Całkowity przebieg", "ignition": "Zapłon", "history": "Historia trasy"},
        "en": {"speed": "Speed", "fuel": "Fuel", "heading": "Heading", "engine": "Engine RPM", "distance": "Total distance", "ignition": "Ignition", "history": "Route history"},
        "de": {"speed": "Geschwindigkeit", "fuel": "Kraftstoff", "heading": "Fahrtrichtung", "engine": "Motordrehzahl", "distance": "Gesamtstrecke", "ignition": "Zündung", "history": "Routenverlauf"},
    }.get(lang, {})

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

        {debug_link}
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
        debug_link=(
            '<a class="button" href="/tachograph-debug">Технічна перевірка даних</a>'
            if current_role() == "director" else ""
        ),
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
        "account": str(NAVIREC_ACCOUNT_ID),
        "vehicle_states": vehicle_states,
        "driver_states": driver_states_result,
        "drivers": drivers_result,
        "tachograph_cards": cards_result
    }

    lang = current_language()
    vehicle_labels = {
        "uk": {"speed": "Швидкість", "fuel": "Паливо", "heading": "Напрямок", "engine": "Оберти двигуна", "distance": "Загальна відстань", "ignition": "Запалювання", "history": "Історія маршруту"},
        "pl": {"speed": "Prędkość", "fuel": "Paliwo", "heading": "Kierunek", "engine": "Obroty silnika", "distance": "Całkowity przebieg", "ignition": "Zapłon", "history": "Historia trasy"},
        "en": {"speed": "Speed", "fuel": "Fuel", "heading": "Heading", "engine": "Engine RPM", "distance": "Total distance", "ignition": "Ignition", "history": "Route history"},
        "de": {"speed": "Geschwindigkeit", "fuel": "Kraftstoff", "heading": "Fahrtrichtung", "engine": "Motordrehzahl", "distance": "Gesamtstrecke", "ignition": "Zündung", "history": "Routenverlauf"},
    }.get(lang, {})

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

    lang = current_language()
    vehicle_labels = {
        "uk": {"speed": "Швидкість", "fuel": "Паливо", "heading": "Напрямок", "engine": "Оберти двигуна", "distance": "Загальна відстань", "ignition": "Запалювання", "history": "Історія маршруту"},
        "pl": {"speed": "Prędkość", "fuel": "Paliwo", "heading": "Kierunek", "engine": "Obroty silnika", "distance": "Całkowity przebieg", "ignition": "Zapłon", "history": "Historia trasy"},
        "en": {"speed": "Speed", "fuel": "Fuel", "heading": "Heading", "engine": "Engine RPM", "distance": "Total distance", "ignition": "Ignition", "history": "Route history"},
        "de": {"speed": "Geschwindigkeit", "fuel": "Kraftstoff", "heading": "Fahrtrichtung", "engine": "Motordrehzahl", "distance": "Gesamtstrecke", "ignition": "Zündung", "history": "Routenverlauf"},
    }.get(lang, {})

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
    if tenancy.company():
        if current_role() != "director": return Response("Доступ лише для директора.", status=403)
        return page("Стан системи", "<div class=card><p>Компанія: "+escape(str(COMPANY_NAME))+"</p><p>Автомобілів: "+str(len(VEHICLES))+"</p><p>Збереження: "+("постійне" if _tenant_storage_is_persistent() else "тимчасове")+"</p><p>GPS: "+escape(PROVIDERS.get(tenancy.company().get("gps", {}).get("provider", "navirec"), "Не підключено"))+"</p></div>", "health")
    return jsonify({
        "status": "ok",
        "company": str(COMPANY_NAME),
        "company_id": str(COMPANY_ID),
        "vehicles": len(VEHICLES),
        "driver_password_storage": (
            "persistent"
            if _driver_access_storage_is_persistent()
            else "ephemeral"
        ),
        "driver_access_file": DRIVER_ACCESS_FILE,
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
