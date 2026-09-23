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
        "delivery_stop_status",
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
        "name": "Renault Master SH 9203G",
        "plate": "SH 9203G"
    },
    {
        "id": "cbb121b6-34dd-41c6-974b-5b7aa3d9a1cb",
        "name": "Renault Master DX 9034F",
        "plate": "DX 9034F"
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
    """Return the latest Navirec vehicle states with a short retry.

    Navirec can occasionally return a transient error/empty response.  The
    GPS planner must not lose every vehicle because of one failed request,
    so we retry before treating the state list as unavailable.
    """
    if not NAVIREC_TOKEN:
        return []

    url = f"{NAVIREC_API}/last_vehicle_states/"
    params = {"account": NAVIREC_ACCOUNT_ID}

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


DELIVERY_ROUTES_FILE = os.environ.get(
    "DELIVERY_ROUTES_FILE",
    "/tmp/tranviq_delivery_routes.json"
)
DELIVERY_ROUTES_LOCK = threading.Lock()


def _load_delivery_routes():
    try:
        with open(DELIVERY_ROUTES_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_delivery_routes(data):
    folder = os.path.dirname(DELIVERY_ROUTES_FILE)
    if folder:
        os.makedirs(folder, exist_ok=True)
    temporary = DELIVERY_ROUTES_FILE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    os.replace(temporary, DELIVERY_ROUTES_FILE)


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
            stops.append({
                "latitude": latitude,
                "longitude": longitude
            })

    if not stops:
        return jsonify({"error": "Немає координат точок."}), 400

    today = datetime.now(POLAND_TZ).date()
    if selected_date > today:
        return jsonify({
            "statuses": ["pending"] * len(stops),
            "radius_m": 180
        })

    history = get_vehicle_history(vehicle_id, date_string)
    points = history.get("points", []) if history.get("ok") else []
    current_state = state_for_vehicle(vehicle_id)
    current_latitude = None
    current_longitude = None

    if current_state:
        current_latitude, current_longitude = extract_coordinates(
            current_state.get("location")
        )

    radius_km = 0.18
    statuses = []

    for stop in stops:
        is_current = False
        if current_latitude is not None and current_longitude is not None:
            current_distance = haversine_km(
                {
                    "latitude": current_latitude,
                    "longitude": current_longitude
                },
                stop
            )
            is_current = current_distance <= radius_km

        if is_current and selected_date == today:
            statuses.append("current")
            continue

        visited = False
        for point in points:
            distance = haversine_km(point, stop)
            if distance > radius_km:
                continue

            speed = safe_float(point.get("speed")) or 0.0
            activity = str(point.get("activity") or "").strip().lower()
            if speed <= 8 or activity not in ("driving", "moving"):
                visited = True
                break

        statuses.append("completed" if visited else "pending")

    return jsonify({
        "statuses": statuses,
        "radius_m": int(radius_km * 1000)
    })


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
        'Попередження': 'Ostrzeżenia',
        'Водій': 'Kierowca',
        'Картка водія': 'Karta kierowcy',
        'Вставлена': 'Włożona',
        'Оновлено': 'Zaktualizowano',
        'Стан часу': 'Stan czasu',
        'До наступної перерви': 'Do następnej przerwy',
        'Залишок безперервного керування': 'Pozostały czas jazdy ciągłej',
        'Залишок керування сьогодні': 'Pozostały czas jazdy dzisiaj',
        'Залишок у зміні': 'Pozostały czas jazdy w zmianie',
        'Залишок керування цього тижня': 'Pozostały czas jazdy w tym tygodniu',
        'До денного відпочинку': 'Do odpoczynku dobowego',
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
        "Картка водія не вставлена в тахограф.": "Karta kierowcy nie jest włożona do tachografu.",
        "Термін дії картки водія закінчився.": "Karta kierowcy straciła ważność.", "Без попереджень": "Brak ostrzeżeń",
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
        "Початок сьогоднішньої роботи:": "Start of today's work:", "Після завершення залишається щонайменше": "After completion, at least",
        "Орієнтовна оплата доріг:": "Estimated road toll:", "До наступної вигрузки:": "To next unloading:", "До останньої вигрузки:": "To final unloading:",
        "Тахограф і час водіїв": "Tachograph and driver time", "Тахограф — технічні дані": "Tachograph — technical data",
        "Планування маршруту": "Route planning", "Початок маршруту — автомобіль": "Route start — vehicle", "Розвізний маршрут": "Delivery route",
        "Адреси й часові вікна": "Addresses and time windows", "Видалити маршрут автомобіля": "Delete vehicle route", "Змінити порядок адрес": "Change address order",
        "Очистити всі адреси": "Clear all addresses", "Прорахувати всі доставки": "Calculate all deliveries", "Розвантаження, min": "Unloading, min",
        "Добовий відпочинок": "Daily rest", "Вулиця або точна адреса": "Street or exact address", "Прокласти маршрут": "Calculate route",
        "Варіант маршруту": "Route option", "Платні дороги дозволені": "Toll roads allowed", "Уникати платних доріг": "Avoid toll roads", "Безплатний": "Toll-free", "Швидкий": "Fast",
        "Витрата, л/100 км": "Consumption, l/100 km", "Ціна за літр": "Price per litre", "Валюта": "Currency", "Тип автомобіля": "Vehicle type", "Тип транспорту": "Transport type",
        "Місто": "City", "Вулиця / адреса": "Street / address", "Шукати": "Search", "Знайти адресу": "Find address", "Виміряти маршрут": "Measure route", "Очистити карту": "Clear map",
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
        "Компанія:": "Company:", "Автомобілів у системі:": "Vehicles in system:", "Є дані Navirec": "Navirec data available", "Немає поточного стану": "No current state", "Відкрити": "Open",
        "Автомобіль": "Vehicle", "Швидкість": "Speed", "Деталі": "Details", "Дата": "Date", "Показати історію": "Show history", "GPS-точок": "GPS points",
        "Початок": "Start", "Кінець": "End", "Відстань": "Distance", "Макс. швидкість": "Max. speed", "Паливо на початку": "Fuel at start", "Паливо в кінці": "Fuel at end",
        "Рух": "Driving", "Час руху": "Driving time", "Стоянка": "Parking", "Холостий хід": "Idling", "Витрата": "Consumption", "Останні 100 GPS-точок": "Last 100 GPS points",
        "Час": "Time", "Стан": "State", "Координати": "Coordinates", "Оплата доріг і віньєти": "Road tolls and vignettes",
        "Розрахувати маршрут, паливо й оплату доріг": "Calculate route, fuel and road tolls",
        "Тут поки показується поточний рівень палива з Navirec. Збільшення рівня ще не вважаємо автоматично заправкою.": "For now, the current fuel level from Navirec is shown. An increase in level is not yet automatically treated as refuelling.",
    },
    "de": {
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

# Never replace bare word fragments inside other words.  Units are handled
# with whitespace-aware forms, preventing strings such as "сьоgodz.ні".
for _lang in ("pl", "en", "de"):
    for _unsafe in ("год", "км", "хв", "керування"):
        GLOBAL_UI_TRANSLATIONS.get(_lang, {}).pop(_unsafe, None)


def translate_full_app_body(language, body):
    if language == "uk":
        return body
    mapping = GLOBAL_UI_TRANSLATIONS.get(language)
    if not mapping:
        return body
    for source, target in sorted(mapping.items(), key=lambda item: len(item[0]), reverse=True):
        body = body.replace(source, target)
    return body

def page(title, body, active=""):
    role = current_role()
    language = current_language()
    visible_title = translate_full_app_body(language, translate_title(language, title))
    body = translate_full_app_body(language, body)
    page_class = "page-gps" if active == "gps" else ""
    branding = get_company_branding(
        COMPANY_ID,
        COMPANY_NAME
    )
    company_display_name = escape(
        branding["company_name"]
    )

    road_payments_label = {
        "uk": "🛣️ Оплата доріг",
        "pl": "🛣️ Opłaty drogowe",
        "en": "🛣️ Road tolls",
        "de": "🛣️ Maut",
    }.get(language, "🛣️ Оплата доріг")

    if role == "driver":
        nav_items = [
            ("driver", "/driver", t("my_trips")),
            (
                "road_payments",
                "/road-payments",
                road_payments_label
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
            (
                "road_payments",
                "/road-payments",
                road_payments_label
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
                <i class="gps-status-dot moving"></i> Їде
            </span>
            <span>
                <i class="gps-status-dot idling"></i> Заведена
            </span>
            <span>
                <i class="gps-status-dot stopped"></i> Стоїть
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

        const speed =
            vehicle.speed === null
            ? '—'
            : vehicle.speed.toFixed(0) + ' км/год';

        const fuel =
            vehicle.fuel === null
            ? '—'
            : vehicle.fuel.toFixed(1) + '%';

        const statusLabel = markerStatus === 'moving'
            ? 'Їде'
            : (markerStatus === 'idling' ? 'Заведена' : 'Стоїть');

        const numberLabel = document.createElement('span');
        numberLabel.textContent = vehicle.plate || vehicle.name;
        marker.bindTooltip(numberLabel, {{
            permanent: true,
            direction: 'top',
            offset: [0, -4],
            opacity: 1,
            className: 'vehicle-number-label'
        }});

        const basePopup =
            '<strong>' + vehicle.name + '</strong><br>' +
            'Статус: ' + statusLabel + '<br>' +
            'Швидкість: ' + speed + '<br>' +
            'Паливо: ' + fuel + '<br>' +
            vehicle.latitude.toFixed(6) + ', ' +
            vehicle.longitude.toFixed(6);
        vehicleMarkersById[vehicle.id] = marker;
        vehiclePopupBaseById[vehicle.id] = basePopup;
        marker.bindPopup(basePopup);
        marker.on('click', function() {{
            if (vehicleSelect && vehicleSelect.value !== vehicle.id) {{
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
    vehicleSelect.addEventListener('change', function() {{
        updateFuelConsumption();
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
        if (plannedRouteLayer) {{
            map.removeLayer(plannedRouteLayer);
            plannedRouteLayer = null;
        }}
        if (destinationMarker) {{
            map.removeLayer(destinationMarker);
            destinationMarker = null;
        }}
        deliveryMarkers.forEach(function(marker) {{
            map.removeLayer(marker);
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

        const polishUi = document.documentElement.lang === 'pl';
        if (hours > 0) {{
            return polishUi
                ? hours + ' godz. ' + minutes + ' min'
                : hours + ' год ' + minutes + ' хв';
        }}
        return polishUi ? minutes + ' min' : minutes + ' хв';
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
                deliveryRoute.date,
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
                ? deliveryWindow(deliveryRoute.date, stop.window_start)
                : null;
            const windowEnd = hasWindow
                ? deliveryWindow(deliveryRoute.date, stop.window_end)
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
                (document.documentElement.lang === 'pl'
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
                (document.documentElement.lang === 'pl'
                    ? 'Następny załadunek można wykonać po zakończeniu dostaw, a dalszą jazdę planować po 45-minutowej przerwie. Ostatecznie zweryfikować z tachografem.'
                    : 'Наступне завантаження можна виконувати після ' +
                        'розвізки, а подальший рух планувати після перерви ' +
                        '45 хв. Остаточно звірити з тахографом.');
        }} else if (canDriveAfter >= 60 * 60) {{
            nextRecommendation = restBeforeStart && !hasTachograph
                ? (document.documentElement.lang === 'pl'
                    ? 'Przed porannym wyjazdem podczas postoju zostanie osiągnięty odpoczynek dobowy. Po zakończeniu pozostanie około ' + formatDuration(canDriveAfter) + ' czasu jazdy; po uruchomieniu należy zweryfikować dane z tachografem.'
                    : 'До ранкового виїзду за стоянкою набирається ' +
                        'добовий відпочинок. Після завершення залишається ' +
                        'орієнтовно ' + formatDuration(canDriveAfter) +
                        ' керування; після запуску звірити з тахографом.')
                : (document.documentElement.lang === 'pl'
                    ? 'Po zakończeniu pozostaje co najmniej ' + formatDuration(canDriveAfter) + ' czasu jazdy.'
                    : 'Після завершення залишається щонайменше ' + formatDuration(canDriveAfter) + ' керування.');
        }} else {{
            nextSafeStart = new Date(
                freeAt.getTime() + dailyRestSeconds * 1000
            );
            nextRecommendation =
                (document.documentElement.lang === 'pl'
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
        destinationInput.placeholder =
            'Введіть вулицю у ' +
            (result.short_name || result.city || 'місті');
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
            return document.documentElement.lang === 'pl'
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
            return (document.documentElement.lang === 'pl' ? 'Szacunkowe opłaty drogowe: ' : 'Орієнтовна оплата доріг: ') + prices.join(' + ');
        }}

        if (routeData.toll_estimate) {{
            const estimate = routeData.toll_estimate;
            const segmentText = estimate.segment
                ? (document.documentElement.lang === 'pl' ? '; odcinek ' : '; ділянка ') + estimate.segment
                : '';
            return (document.documentElement.lang === 'pl' ? 'Szacunkowa opłata ' : 'Орієнтовна оплата ') + estimate.road + ': ≈ ' +
                Number(estimate.amount).toFixed(0) + ' ' +
                estimate.currency + ' (' +
                Number(estimate.distance_km).toFixed(1) +
                (document.documentElement.lang === 'pl' ? ' km drogą płatną' : ' км платною дорогою') + segmentText +
                (document.documentElement.lang === 'pl' ? '; taryfa z 11.09.2026).' : '; тариф від 11.09.2026).');
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
        destinationInput.placeholder = 'Спочатку виберіть місто';
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
            addressResults.textContent = 'Спочатку виберіть місто.';
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

            let extra = '<hr style="margin:7px 0">' +
                '<strong>До наступної вигрузки:</strong> ' +
                (nextDistance / 1000).toFixed(1) + ' км · ' +
                formatDuration(nextDuration);
            if (remainingStops.length > 1) {{
                extra += '<br><strong>До останньої вигрузки:</strong> ' +
                    (finalDistance / 1000).toFixed(1) + ' км · ' +
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
            line += ' | ' + stop.window_start + ' | ' + stop.window_end;
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
            'font-size:12px;font-weight:800;">↕ Змінити порядок адрес (' +
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
            marker.bindPopup(
                '<strong>Доставка ' + (index + 1) + '</strong><br>' +
                escapeHtml(stop.address) + '<br>' +
                (stop.window_start && stop.window_end
                    ? stop.window_start + '–' + stop.window_end
                    : 'Без часового вікна') +
                '<br><strong>Ще не вигружено</strong>'
            );
            deliveryMarkers.push(marker);
        }});
    }}

    async function restoreDeliveryRouteForVehicle(vehicleId) {{
        removeMeasurementLayers();
        removePlannedRoute();
        activeDeliveryRoute = null;
        renderDeliveryStopOrder();

        const saved = await readSavedDeliveryRoute(vehicleId);
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
            map.fitBounds(
                plannedRouteLayer.getBounds(),
                {{padding: [45, 45]}}
            );
        }}
        if (saved.summary_html) {{
            let restoredSummary = saved.summary_html;
            if (document.documentElement.lang === 'pl') {{
                const plSummaryReplacements = [
                    ['Розвізка', 'Trasa dostaw'],
                    ['Автомобіль:', 'Pojazd:'],
                    ['Водій:', 'Kierowca:'],
                    ['Відстань:', 'Odległość:'],
                    ['Чистий час керування:', 'Czysty czas jazdy:'],
                    ['Планований виїзд:', 'Planowany wyjazd:'],
                    ['Початок сьогоднішньої роботи:', 'Początek dzisiejszej pracy:'],
                    ['Сьогодні вже пройдено:', 'Dzisiaj już przejechano:'],
                    ['керування:', 'jazda:'],
                    ['Паливо:', 'Paliwo:'],
                    ['Перерв 45 хв:', 'Przerwy 45 min:'],
                    ['добових відпочинків:', 'odpoczynki dobowe:'],
                    ['Фізично вільний:', 'Fizycznie wolny:'],
                    ['Наступне завантаження можна планувати:', 'Następny załadunek można planować:'],
                    ['Рекомендований наступний виїзд:', 'Zalecany następny wyjazd:'],
                    ['Після завершення залишається щонайменше', 'Po zakończeniu pozostaje co najmniej'],
                    ['Для наступного рейсу потрібен добовий відпочинок.', 'Przed następną trasą wymagany jest odpoczynek dobowy.'],
                    ['Маршрут узгоджено з актуальним тахографом.', 'Trasa jest zgodna z aktualnymi danymi tachografu.'],
                    ['без часового вікна', 'bez okna czasowego'],
                    ['виїзд', 'wyjazd'],
                    ['від попередньої точки', 'od poprzedniego punktu'],
                    ['Орієнтовна оплата', 'Szacunkowa opłata'],
                    ['км платною дорогою', 'km drogą płatną'],
                    ['км платною', 'km płatne'],
                    ['ділянка', 'odcinek'],
                    ['тариф від', 'taryfa z'],
                    ['орієнтовно', 'około'],
                    ['часу керування', 'czasu jazdy'],
                    ['Розвантаження прийнято по', 'Przyjęto czas rozładunku:'],
                    ['хв на точку.', 'min na punkt.'],
                    ['Після виконання рейсу', 'Po wykonaniu trasy'],
                    ['порівняємо прогноз із фактом і скоригуємо норматив.', 'porównamy prognozę z rzeczywistym czasem i skorygujemy normę.'],
                    [' год ', ' godz. '],
                    [' хв', ' min'],
                    [' км', ' km'],
                    [' л ≈', ' l ≈'],
                    [' керування.', ' jazdy.']
                ];
                plSummaryReplacements.forEach(function(pair) {{
                    restoredSummary = restoredSummary.split(pair[0]).join(pair[1]);
                }});
                restoredSummary = restoredSummary.replace(
                    /<li><strong>\d+\.\s*/g,
                    '<li><strong>'
                );
            }}
            measureResult.innerHTML = restoredSummary;

            // Polish GPS: translate the restored route summary once, directly in this block.
            // No observer and no changes to routing/calculation logic.
            if (document.documentElement.lang === 'pl') {{
                const walker = document.createTreeWalker(
                    measureResult,
                    NodeFilter.SHOW_TEXT
                );
                const textNodes = [];
                while (walker.nextNode()) textNodes.push(walker.currentNode);
                const replacements = [
                    ['Розвізка', 'Trasa dostaw'],
                    ['Автомобіль:', 'Pojazd:'],
                    ['Водій:', 'Kierowca:'],
                    ['Відстань:', 'Odległość:'],
                    ['Чистий час керування:', 'Czysty czas jazdy:'],
                    ['Планований виїзд:', 'Planowany wyjazd:'],
                    ['Початок сьогоднішньої роботи:', 'Początek dzisiejszej pracy:'],
                    ['Сьогодні вже пройдено:', 'Dzisiaj już przejechano:'],
                    ['керування:', 'czas jazdy:'],
                    ['Паливо:', 'Paliwo:'],
                    ['Перерв 45 хв:', 'Przerwy 45 min:'],
                    ['добових відпочинків:', 'odpoczynki dobowe:'],
                    ['Фізично вільний:', 'Fizycznie wolny:'],
                    ['Наступне завантаження можна планувати:', 'Następny załadunek można planować:'],
                    ['Рекомендований наступний виїзд:', 'Zalecany następny wyjazd:'],
                    ['Після завершення залишається щонайменше', 'Po zakończeniu pozostaje co najmniej'],
                    ['часу керування.', 'czasu jazdy.'],
                    ['Маршрут узгоджено з актуальним тахографом.', 'Trasa jest zgodna z aktualnymi danymi tachografu.'],
                    ['без часового вікна', 'bez okna czasowego'],
                    ['виїзд', 'wyjazd'],
                    ['від попередньої точки', 'od poprzedniego punktu'],
                    ['Орієнтовна оплата', 'Szacunkowa opłata'],
                    ['км платною дорогою', 'km drogą płatną'],
                    ['ділянка', 'odcinek'],
                    ['тариф від', 'taryfa z'],
                    [' год ', ' godz. '],
                    [' хв', ' min'],
                    [' км', ' km'],
                    [' л ≈', ' l ≈']
                ];
                textNodes.forEach(function(node) {{
                    let value = node.nodeValue || '';
                    replacements.forEach(function(pair) {{
                        value = value.split(pair[0]).join(pair[1]);
                    }});
                    node.nodeValue = value;
                }});
            }}
        }} else {{
            measureResult.innerHTML =
                '<strong>' + escapeHtml(saved.delivery_route.label) +
                '</strong><br>' +
                (document.documentElement.lang === 'pl'
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

    function parseDeliveryStopLines() {{
        const rawText = deliveryStopsInput.value.trim();
        if (!rawText) {{
            throw new Error('Вставте адреси або текст транспортного завдання.');
        }}

        const validTime = /^([01]\\d|2[0-3]):[0-5]\\d$/;
        const datePattern = /^\\d{{4}}[-./]\\d{{2}}[-./]\\d{{2}}$/;
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
                const windowStart = parts[1] || '';
                const windowEnd = parts[2] || '';
                const hasAnyWindow = Boolean(windowStart || windowEnd);
                if (!address) {{
                    throw new Error('Рядок ' + (index + 1) + ': адреса порожня.');
                }}
                if (parts.length > 3 || (hasAnyWindow &&
                        (!validTime.test(windowStart) || !validTime.test(windowEnd)))) {{
                    throw new Error(
                        'Рядок ' + (index + 1) +
                        ': використайте «адреса» або «адреса | 08:00 | 10:00».'
                    );
                }}
                return {{
                    address: address,
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
        if (status === 'completed') return 'Вигружено';
        if (status === 'current') return 'Машина на вигрузці';
        return 'Ще не вигружено';
    }}

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
                            longitude: stop.longitude
                        }};
                    }})
                }})
            }});
            const data = await response.json();
            if (!response.ok || !Array.isArray(data.statuses)) {{
                return;
            }}

            data.statuses.forEach(function(status, index) {{
                const marker = deliveryMarkers[index];
                const stop = deliveryRoute.stops[index];
                if (!marker || !stop) return;

                marker.setIcon(deliveryStopIcon(index + 1, status));
                marker.setPopupContent(
                    '<strong>Доставка ' + (index + 1) + '</strong><br>' +
                    escapeHtml(stop.address) + '<br>' +
                    (stop.window_start && stop.window_end
                        ? stop.window_start + '–' + stop.window_end
                        : 'Без часового вікна') +
                    '<br><strong>' +
                    escapeHtml(deliveryStatusLabel(status)) +
                    '</strong>'
                );
            }});
            await refreshVehicleDeliveryPopup(
                vehicle,
                deliveryRoute,
                data.statuses
            );
            const lastStatus = data.statuses[
                deliveryRoute.stops.length - 1
            ];
            // Активний маршрут очищаємо тільки коли автомобіль ЗАРАЗ
            // знаходиться на останній точці. Історичний заїзд на цю адресу
            // раніше того самого дня не повинен видаляти новий маршрут.
            if (lastStatus === 'current') {{
                removeSavedDeliveryRoute(vehicle.id);
            }}
        }} catch (error) {{
            // Статуси не повинні ламати сам маршрут.
        }}
    }}

    async function buildDeliveryRoute() {{
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
                label: (document.documentElement.lang === 'pl' ? 'Trasa dostaw ' : 'Розвізка ') + deliveryRouteDate.value,
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
            try {{
                await saveDeliveryRouteForVehicle(earlySavedRoute);
            }} catch (saveError) {{
                console.warn('Маршрут збережено локально; серверний запис не вдався.', saveError);
            }}

            buildDeliveryRouteButton.textContent =
                'Аналізую сьогоднішню роботу і паузу...';
            const daySummary = await daySummaryPromise;

            plannedRouteLayer = L.polyline(routeData.points, {{
                color: '#087f8c',
                weight: 6,
                opacity: .9
            }}).addTo(map);
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
            const polishUi = document.documentElement.lang === 'pl';
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
                ? (polishUi
                    ? 'Ryzyko opóźnienia: ' + schedule.late_count + ' punktów poza oknem czasowym.'
                    : 'Є ризик запізнення: ' + schedule.late_count + ' точок поза вікном.')
                : (schedule.has_tachograph
                    ? (polishUi ? 'Trasa jest zgodna z aktualnymi danymi tachografu.' : 'Маршрут узгоджено з актуальним тахографом.')
                    : (schedule.rest_before_start
                        ? 'До виїзду враховано стоянку з вимкненим ' +
                            'запалюванням як розрахункову паузу. ' +
                            'Після запуску звірити з тахографом.'
                        : 'Маршрут розраховано, але тахограф не дав ' +
                            'повного залишку часу.'));

            const stopRows = schedule.stops.map(function(stop, index) {{
                let note = '';
                if (stop.wait_seconds >= 60) {{
                    note += (polishUi ? ' · oczekiwanie ' : ' · очікування ') +
                        formatDuration(stop.wait_seconds);
                }}
                if (stop.late) {{
                    note += (polishUi ? ' · <strong>OPÓŹNIENIE</strong>' : ' · <strong>ЗАПІЗНЕННЯ</strong>');
                }}
                return '<li><strong>' +
                    formatDateTime(stop.service_start) + '</strong> — ' +
                    escapeHtml(stop.address) +
                    (stop.window_start && stop.window_end
                        ? ' (' + stop.window_start + '–' + stop.window_end + ')'
                        : (polishUi ? ' (bez okna czasowego)' : ' (без часового вікна)')) +
                    '<br><span class="small">' + (polishUi ? 'wyjazd ' : 'виїзд ') +
                    formatDateTime(stop.departure) +
                    (polishUi ? ', od poprzedniego punktu ' : ', від попередньої точки ') +
                    (stop.distance_m / 1000).toFixed(1) +
                    ' km' + note + '</span>' +
                    '<br><span style="display:inline-flex;gap:6px;margin-top:5px">' +
                    '<button type="button" title="Підняти точку" ' +
                    'onclick="reorderActiveDeliveryStops(' + index + ', -1)" ' +
                    (index === 0 ? 'disabled ' : '') + '>↑</button>' +
                    '<button type="button" title="Опустити точку" ' +
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
                (polishUi ? '<br><span class="small">Przyjęto czas rozładunku: ' : '<br><span class="small">Розвантаження прийнято по ') +
                serviceMinutes + (polishUi ? ' min na punkt. Po wykonaniu trasy ' : ' хв на точку. Після виконання рейсу ') +
                (polishUi ? 'porównamy prognozę z rzeczywistym czasem i skorygujemy normę.</span>' : 'порівняємо прогноз із фактом і скоригуємо норматив.</span>');

            buildDeliveryRouteButton.textContent =
                'Зберігаю активний маршрут...';
            await saveDeliveryRouteForVehicle({{
                vehicle_id: vehicle.id,
                delivery_route: deliveryRoute,
                route_data: routeData,
                input_text: deliveryStopsInput.value,
                service_minutes: serviceMinutes,
                daily_rest_hours: dailyRestHours,
                vehicle_profile: vehicleProfile,
                summary_html: measureResult.innerHTML,
                saved_at: new Date().toISOString()
            }});
        }} catch (error) {{
            measureResult.textContent =
                error.message ||
                'Не вдалося прорахувати розвізний маршрут.';
        }} finally {{
            buildDeliveryRouteButton.disabled =
                !deliveryMapConsent.checked ||
                !deliveryStopsInput.value.trim();
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
        lat=center_lat,
        lon=center_lon
    )

    if current_language() == "pl":
        # GPS JavaScript is rendered server-side. Force every Polish branch
        # before sending the page to the browser, so dynamic route results
        # cannot fall back to Ukrainian.
        body = body.replace("document.documentElement.lang === 'pl'", "true")
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
            body = body.replace(source_text, target_text)


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
            "Їде": "Driving", "Стоїть": "Stopped", "Статус:": "Status:", "Швидкість:": "Speed:",
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
            "Шукати": "Suchen", "Знайти адресу": "Adresse suchen", "Прокласти маршрут": "Route planen",
            "Побудувати маршрут": "Route planen", "Розвізний маршрут": "Ausliefertour", "Дата доставок": "Lieferdatum",
            "Адреси й часові вікна": "Adressen und Zeitfenster", "Змінити порядок адрес": "Adressreihenfolge ändern",
            "Очистити всі адреси": "Alle Adressen löschen", "Видалити маршрут автомобіля": "Fahrzeugroute löschen",
            "Розвантаження, min": "Entladung, Min.", "Хв на точку": "Min. pro Stopp", "Добовий відпочинок": "Tägliche Ruhezeit",
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
            "Їде": "Fährt", "Стоїть": "Steht", "Статус:": "Status:", "Швидкість:": "Geschwindigkeit:",
            " год ": " Std. ", " хв": " Min."
        }
    }
    lang = current_language()
    if lang in gps_extra_translations:
        # Replace longer phrases first so short words cannot damage them.
        for source_text, target_text in sorted(gps_extra_translations[lang].items(), key=lambda item: len(item[0]), reverse=True):
            body = body.replace(source_text, target_text)

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
            "керування.": "jazdy.", " год ": " godz. ", " км": " km", " л ": " l ", "хв": "min",
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
            "Вартість є орієнтовною.": "Die Kosten sind geschätzt.",
            "Вона залежить від ваги, осей, екологічного класу, віньєт і способу оплати.": "Sie hängen von Gewicht, Achsen, Emissionsklasse, Vignetten und Zahlungsart ab.",
            "керування.": "Fahrzeit.",  " л ": " l ",  "хв": "Min."
        }
    }

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
