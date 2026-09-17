import os
import json
from datetime import datetime

import requests
from flask import Flask, redirect, url_for, session, request

app = Flask(__name__)

# =========================================================
# O&O TRANS TRANSPORT PLATFORM
# =========================================================
#
# Standalone transport management platform.
#
# Architecture:
#
# Company
#   ↓
# Users
#   ↓
# Vehicles
#   ↓
# Navirec
#   ↓
# GPS / Fuel / Routes / Tachograph
#
# O&O TRANS is the first company.
# The platform is prepared for future multi-company use.
# =========================================================


# =========================================================
# SETTINGS
# =========================================================

app.secret_key = os.getenv(
    "SESSION_SECRET",
    "change-this-secret"
)

NAVIREC_TOKEN = os.getenv("NAVIREC_TOKEN")

ADMIN_USER = os.getenv(
    "ADMIN_USER",
    "admin"
)

ADMIN_PASSWORD = os.getenv(
    "ADMIN_PASSWORD",
    "admin"
)

NAVIREC_API = "https://api.navirec.com"


# =========================================================
# COMPANY
# =========================================================

COMPANY_NAME = os.getenv(
    "COMPANY_NAME",
    "O&O TRANS"
)

COMPANY_ID = os.getenv(
    "COMPANY_ID",
    "o-o-trans"
)


# =========================================================
# NAVIREC ACCOUNT
# =========================================================

ACCOUNT_ID = os.getenv(
    "NAVIREC_ACCOUNT_ID",
    "5c980074-7a71-4c9b-b5a8-a7c45163adf5"
)


# =========================================================
# VEHICLES
# =========================================================

VEHICLES = {

    "aaaa9acd-5bb5-467e-8241-81444292bbfe": {
        "name": "Renault Master SH 9203G"
    },

    "cbb121b6-34dd-41c6-974b-5b7aa3d9a1cb": {
        "name": "Renault Master DX 9034F"
    },

    "f016af91-dee6-4e72-9f86-4b2e27a253c1": {
        "name": "Renault Master DX 5405A"
    },

}


# =========================================================
# LOGIN
# =========================================================

def logged_in():

    return session.get("logged_in") is True


@app.route("/login", methods=["GET", "POST"])
def login():

    error = ""

    if request.method == "POST":

        username = request.form.get(
            "username",
            ""
        )

        password = request.form.get(
            "password",
            ""
        )

        if (
            username == ADMIN_USER
            and password == ADMIN_PASSWORD
        ):

            session["logged_in"] = True

            return redirect(
                url_for("home")
            )

        error = "Неправильний логін або пароль"

    return f"""
<!doctype html>

<html lang="uk">

<head>

<meta charset="utf-8">

<meta
name="viewport"
content="width=device-width, initial-scale=1"
>

<title>{COMPANY_NAME} — Вхід</title>

<style>

* {{
    box-sizing:border-box;
}}

body {{

    margin:0;

    font-family:Arial,sans-serif;

    background:#111827;

    color:white;

    min-height:100vh;

    display:flex;

    align-items:center;

    justify-content:center;

}}

.login-box {{

    width:380px;

    max-width:90%;

    background:#1f2937;

    padding:32px;

    border-radius:18px;

    box-shadow:
        0 20px 50px
        rgba(0,0,0,.35);

}}

.logo {{

    font-size:30px;

    font-weight:bold;

    margin-bottom:8px;

}}

.subtitle {{

    color:#9ca3af;

    margin-bottom:25px;

}}

input {{

    width:100%;

    padding:13px;

    margin-bottom:12px;

    border:0;

    border-radius:9px;

    font-size:15px;

}}

button {{

    width:100%;

    padding:13px;

    border:0;

    border-radius:9px;

    background:#2563eb;

    color:white;

    font-weight:bold;

    font-size:15px;

    cursor:pointer;

}}

button:hover {{

    background:#1d4ed8;

}}

.error {{

    margin-top:15px;

    color:#f87171;

}}

</style>

</head>

<body>

<div class="login-box">

<div class="logo">
🚚 {COMPANY_NAME}
</div>

<div class="subtitle">
Transport Management System
</div>

<form method="post">

<input
name="username"
placeholder="Логін"
autocomplete="username"
>

<input
name="password"
type="password"
placeholder="Пароль"
autocomplete="current-password"
>

<button type="submit">
Увійти
</button>

</form>

<div class="error">
{error}
</div>

</div>

</body>

</html>
"""


@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("login")
    )


# =========================================================
# NAVIREC
# =========================================================

def navirec_headers():

    return {

        "Authorization":
            f"Token {NAVIREC_TOKEN}",

        "Accept":
            "application/json; version=1.52.1",

    }


def get_vehicle_states():

    if not NAVIREC_TOKEN:

        return None, "NAVIREC_TOKEN не налаштований"

    try:

        response = requests.get(

            f"{NAVIREC_API}/last_vehicle_states/",

            headers=navirec_headers(),

            params={
                "account": ACCOUNT_ID
            },

            timeout=(3, 5),

        )

        response.raise_for_status()

        return response.json(), None

    except requests.exceptions.Timeout:

        return (
            None,
            "Navirec не відповів протягом 5 секунд"
        )

    except requests.exceptions.RequestException as e:

        return (
            None,
            f"Помилка Navirec: {e}"
        )

    except Exception as e:

        return (
            None,
            f"Невідома помилка: {e}"
        )


# =========================================================
# VEHICLE SEARCH
# =========================================================

def extract_vehicle_id(vehicle_url):

    if not vehicle_url:
        return None

    return str(
        vehicle_url
    ).rstrip("/").split("/")[-1]


def find_vehicle_state(
    states,
    vehicle_id
):

    if not isinstance(states, list):
        return None

    for item in states:

        if not isinstance(item, dict):
            continue

        current_id = extract_vehicle_id(
            item.get("vehicle")
        )

        if current_id == vehicle_id:

            return item

    return None


# =========================================================
# ACTIVITY
# =========================================================

def get_activity_raw(state):

    if not isinstance(state, dict):

        return None

    return state.get("activity")


def get_activity(state):

    activity = get_activity_raw(state)

    if activity == "driving":

        return "Рухається"

    if activity == "parking":

        return "Стоїть"

    if activity == "stopped":

        return "Зупинка"

    if activity == "idling":

        return "Двигун працює — стоїть"

    if activity == "unknown":

        return "Невідомо"

    if activity:

        return str(activity)

    return "Немає даних"


def status_class(activity):

    if activity == "Рухається":

        return "moving"

    if activity == "Двигун працює — стоїть":

        return "idling"

    if activity == "Стоїть":

        return "parking"

    return "unknown"


# =========================================================
# DATA HELPERS
# =========================================================

def get_coordinates(state):

    if not isinstance(state, dict):

        return None, None

    location = state.get("location")

    if not isinstance(location, dict):

        return None, None

    coordinates = location.get(
        "coordinates"
    )

    if not isinstance(
        coordinates,
        list
    ):

        return None, None

    if len(coordinates) < 2:

        return None, None

    try:

        longitude = float(
            coordinates[0]
        )

        latitude = float(
            coordinates[1]
        )

        return latitude, longitude

    except Exception:

        return None, None


def get_driver_id(state):

    if not isinstance(state, dict):

        return None

    driver = state.get("driver")

    if not driver:

        return None

    return str(
        driver
    ).rstrip("/").split("/")[-1]


def get_speed(state):

    if not isinstance(state, dict):

        return None

    value = state.get("speed")

    if value is None:

        return None

    try:

        return round(
            float(value),
            1
        )

    except Exception:

        return value


def get_fuel(state):

    if not isinstance(state, dict):

        return None

    value = state.get(
        "fuel_level"
    )

    if value is None:

        value = state.get(
            "fuel_level_ewma"
        )

    if value is None:

        return None

    try:

        return round(
            float(value),
            1
        )

    except Exception:

        return value


def get_distance(state):

    if not isinstance(state, dict):

        return None

    value = state.get(
        "total_distance"
    )

    if value is None:

        return None

    try:

        return round(
            float(value) / 1000,
            1
        )

    except Exception:

        return None


def get_heading(state):

    if not isinstance(state, dict):

        return None

    return state.get(
        "heading"
    )


def get_ignition(state):

    if not isinstance(state, dict):

        return None

    value = state.get(
        "ignition"
    )

    if value is None:

        return None

    return bool(value)


def get_satellites(state):

    if not isinstance(state, dict):

        return None

    return state.get(
        "satellites"
    )


def get_altitude(state):

    if not isinstance(state, dict):

        return None

    return state.get(
        "altitude"
    )


def get_voltage(state):

    if not isinstance(state, dict):

        return None

    value = state.get(
        "supply_voltage"
    )

    if value is None:

        return None

    try:

        return round(
            float(value),
            2
        )

    except Exception:

        return value


def get_engine_speed(state):

    if not isinstance(state, dict):

        return None

    value = state.get(
        "engine_speed"
    )

    if value is None:

        return None

    try:

        return round(
            float(value),
            0
        )

    except Exception:

        return value


def get_time(state):

    if not isinstance(state, dict):

        return None

    return state.get(
        "time"
    )


def get_driver_card(state):

    if not isinstance(state, dict):

        return None

    return state.get(
        "driver_1_card_id"
    )


def format_time(value):

    if not value:

        return "—"

    try:

        dt = datetime.fromisoformat(
            value.replace(
                "Z",
                "+00:00"
            )
        )

        return dt.strftime(
            "%d.%m.%Y %H:%M:%S"
        )

    except Exception:

        return str(value)


def format_number(value):

    if value is None:

        return "—"

    try:

        return f"{float(value):,.1f}".replace(
            ",",
            " "
        )

    except Exception:

        return str(value)


# =========================================================
# HISTORY / TRAIL HELPERS
# =========================================================

def normalize_history_point(point):

    """
    Converts different possible Navirec trail point structures
    into a simple internal format:

    {
        lat,
        lon,
        time,
        speed,
        fuel,
        heading,
        activity
    }
    """

    if not isinstance(point, dict):

        return None

    # -----------------------------------------
    # LOCATION
    # -----------------------------------------

    latitude = None
    longitude = None

    location = point.get("location")

    if isinstance(location, dict):

        coordinates = location.get(
            "coordinates"
        )

        if (
            isinstance(coordinates, list)
            and len(coordinates) >= 2
        ):

            try:

                longitude = float(
                    coordinates[0]
                )

                latitude = float(
                    coordinates[1]
                )

            except Exception:

                pass

        else:

            try:

                if location.get("latitude") is not None:

                    latitude = float(
                        location.get("latitude")
                    )

                if location.get("longitude") is not None:

                    longitude = float(
                        location.get("longitude")
                    )

            except Exception:

                pass

    # -----------------------------------------
    # DIRECT COORDINATES
    # -----------------------------------------

    if latitude is None:

        for key in (
            "latitude",
            "lat"
        ):

            if point.get(key) is not None:

                try:

                    latitude = float(
                        point.get(key)
                    )

                    break

                except Exception:

                    pass

    if longitude is None:

        for key in (
            "longitude",
            "lon",
            "lng"
        ):

            if point.get(key) is not None:

                try:

                    longitude = float(
                        point.get(key)
                    )

                    break

                except Exception:

                    pass

    if latitude is None or longitude is None:

        return None

    # -----------------------------------------
    # TIME
    # -----------------------------------------

    point_time = (
        point.get("time")
        or point.get("timestamp")
        or point.get("datetime")
        or point.get("received_at")
    )

    # -----------------------------------------
    # SPEED
    # -----------------------------------------

    point_speed = (
        point.get("speed")
        or point.get("vehicle_speed")
    )

    try:

        if point_speed is not None:

            point_speed = round(
                float(point_speed),
                1
            )

    except Exception:

        pass

    # -----------------------------------------
    # FUEL
    # -----------------------------------------

    point_fuel = (
        point.get("fuel_level")
        or point.get("fuel")
    )

    try:

        if point_fuel is not None:

            point_fuel = round(
                float(point_fuel),
                1
            )

    except Exception:

        pass

    # -----------------------------------------
    # HEADING
    # -----------------------------------------

    point_heading = (
        point.get("heading")
        or point.get("course")
    )

    # -----------------------------------------
    # ACTIVITY
    # -----------------------------------------

    point_activity = point.get(
        "activity"
    )

    return {

        "lat":
            latitude,

        "lon":
            longitude,

        "time":
            point_time,

        "speed":
            point_speed,

        "fuel":
            point_fuel,

        "heading":
            point_heading,

        "activity":
            point_activity,

    }


def extract_trail_from_state(state):

    """
    Reads the currently available Navirec trail.

    The live /last_vehicle_states/ response can contain
    a recent trail. This function deliberately accepts
    several possible structures so the application does
    not break if Navirec returns the trail differently.
    """

    if not isinstance(state, dict):

        return []

    possible_keys = (
        "trail",
        "track",
        "route",
        "history",
        "points",
    )

    raw_trail = None

    for key in possible_keys:

        value = state.get(key)

        if isinstance(value, list):

            raw_trail = value

            break

        if isinstance(value, dict):

            for nested_key in (
                "points",
                "trail",
                "track",
                "route",
                "history",
            ):

                nested = value.get(
                    nested_key
                )

                if isinstance(nested, list):

                    raw_trail = nested

                    break

        if raw_trail is not None:

            break

    if not raw_trail:

        return []

    result = []

    for point in raw_trail:

        normalized = normalize_history_point(
            point
        )

        if normalized:

            result.append(
                normalized
            )

    return result


def calculate_trail_distance(trail):

    """
    Calculates approximate distance between
    consecutive GPS points using the Haversine formula.
    """

    if not isinstance(trail, list):

        return 0.0

    if len(trail) < 2:

        return 0.0

    from math import radians, sin, cos, sqrt, atan2

    earth_radius = 6371.0

    total = 0.0

    for index in range(
        1,
        len(trail)
    ):

        previous = trail[index - 1]
        current = trail[index]

        try:

            lat1 = radians(
                float(previous["lat"])
            )

            lon1 = radians(
                float(previous["lon"])
            )

            lat2 = radians(
                float(current["lat"])
            )

            lon2 = radians(
                float(current["lon"])
            )

            dlat = lat2 - lat1
            dlon = lon2 - lon1

            a = (
                sin(dlat / 2) ** 2
                +
                cos(lat1)
                *
                cos(lat2)
                *
                sin(dlon / 2) ** 2
            )

            c = 2 * atan2(
                sqrt(a),
                sqrt(1 - a)
            )

            total += earth_radius * c

        except Exception:

            continue

    return round(
        total,
        2
    )


def get_trail_start_time(trail):

    if not trail:

        return None

    return trail[0].get(
        "time"
    )


def get_trail_end_time(trail):

    if not trail:

        return None

    return trail[-1].get(
        "time"
    )


# =========================================================
# HOME
# =========================================================

@app.route("/")
def home():

    if not logged_in():

        return redirect(
            url_for("login")
        )

    states, error = get_vehicle_states()

    online_count = 0

    moving_count = 0

    idling_count = 0

    parking_count = 0

    for vehicle_id in VEHICLES:

        state = find_vehicle_state(
            states,
            vehicle_id
        )

        if state:

            online_count += 1

            activity = get_activity(
                state
            )

            if activity == "Рухається":

                moving_count += 1

            elif activity == "Двигун працює — стоїть":

                idling_count += 1

            elif activity == "Стоїть":

                parking_count += 1

    error_box = ""

    if error:

        error_box = f"""
        <div class="alert">
        ⚠️ {error}
        </div>
        """

    return f"""
<!doctype html>

<html lang="uk">

<head>

<meta charset="utf-8">

<meta
name="viewport"
content="width=device-width, initial-scale=1"
>

<title>{COMPANY_NAME}</title>

<style>

* {{
    box-sizing:border-box;
}}

body {{

    margin:0;

    font-family:Arial,sans-serif;

    background:#f3f4f6;

    color:#111827;

}}

header {{

    background:#111827;

    color:white;

    padding:22px 30px;

}}

header h1 {{

    margin:0;

}}

header p {{

    margin:6px 0 0;

    color:#9ca3af;

}}

.container {{

    max-width:1250px;

    margin:auto;

    padding:25px;

}}

.alert {{

    background:#fff7ed;

    border:1px solid #fdba74;

    padding:15px;

    border-radius:12px;

    margin-bottom:20px;

}}

.stats {{

    display:grid;

    grid-template-columns:
    repeat(auto-fit,minmax(170px,1fr));

    gap:15px;

    margin-bottom:25px;

}}

.stat {{

    background:white;

    padding:20px;

    border-radius:14px;

    box-shadow:
    0 3px 12px rgba(0,0,0,.07);

}}

.stat-number {{

    font-size:30px;

    font-weight:bold;

    margin-top:6px;

}}

.menu {{

    display:grid;

    grid-template-columns:
    repeat(auto-fit,minmax(230px,1fr));

    gap:18px;

}}

.card {{

    background:white;

    padding:25px;

    border-radius:15px;

    box-shadow:
    0 3px 12px rgba(0,0,0,.07);

    text-decoration:none;

    color:#111827;

    transition:.15s;

}}

.card:hover {{

    transform:translateY(-2px);

}}

.icon {{

    font-size:32px;

}}

.card h2 {{

    margin-bottom:5px;

}}

.card p {{

    color:#6b7280;

}}

</style>

</head>

<body>

<header>

<h1>🚚 {COMPANY_NAME}</h1>

<p>
Transport Management System
</p>

</header>

<div class="container">

{error_box}

<div class="stats">

<div class="stat">

Дані Navirec

<div class="stat-number">
{online_count}/{len(VEHICLES)}
</div>

</div>

<div class="stat">

Рухаються

<div class="stat-number">
{moving_count}
</div>

</div>

<div class="stat">

Двигун працює

<div class="stat-number">
{idling_count}
</div>

</div>

<div class="stat">

Стоять

<div class="stat-number">
{parking_count}
</div>

</div>

</div>

<div class="menu">

<a class="card" href="/vehicles">

<div class="icon">🚚</div>

<h2>Машини</h2>

<p>
Стан та параметри автомобілів
</p>

</a>

<a class="card" href="/gps">

<div class="icon">🗺️</div>

<h2>GPS / Карта</h2>

<p>
Реальні позиції автомобілів
</p>

</a>

<a class="card" href="/fuel">

<div class="icon">⛽</div>

<h2>Паливо</h2>

<p>
Рівень пального
</p>

</a>

<a class="card" href="/history">

<div class="icon">🛣️</div>

<h2>Історія маршрутів</h2>

<p>
Маршрути та GPS-трек автомобілів
</p>

</a>

<a class="card" href="/tachograph-test">

<div class="icon">⏱️</div>

<h2>Тахограф</h2>

<p>
Підключення даних водіїв
</p>

</a>

<a class="card" href="/health">

<div class="icon">❤️</div>

<h2>Система</h2>

<p>
Стан сервера та інтеграцій
</p>

</a>

<a class="card" href="/logout">

<div class="icon">🚪</div>

<h2>Вийти</h2>

<p>
Завершити сесію
</p>

</a>

</div>

</div>

</body>

</html>
"""


# =========================================================
# VEHICLES
# =========================================================

@app.route("/vehicles")
def vehicles():

    if not logged_in():

        return redirect(
            url_for("login")
        )

    states, error = get_vehicle_states()

    cards = ""

    for vehicle_id, vehicle_info in VEHICLES.items():

        state = find_vehicle_state(
            states,
            vehicle_id
        )

        name = vehicle_info["name"]

        activity = get_activity(
            state
        )

        speed = get_speed(
            state
        )

        fuel = get_fuel(
            state
        )

        distance = get_distance(
            state
        )

        ignition = get_ignition(
            state
        )

        engine_speed = get_engine_speed(
            state
        )

        last_time = get_time(
            state
        )

        css = status_class(
            activity
        )

        if ignition is True:

            ignition_text = "Увімкнене"

        elif ignition is False:

            ignition_text = "Вимкнене"

        else:

            ignition_text = "—"

        cards += f"""

<div class="vehicle-card">

<h2>
🚚 {name}
</h2>

<div class="status {css}">
{activity}
</div>

<div class="data-grid">

<div>

<span>Швидкість</span>

<strong>
{speed if speed is not None else "—"} км/год
</strong>

</div>

<div>

<span>Паливо</span>

<strong>
{fuel if fuel is not None else "—"} %
</strong>

</div>

<div>

<span>Запалювання</span>

<strong>
{ignition_text}
</strong>

</div>

<div>

<span>Оберти двигуна</span>

<strong>
{engine_speed if engine_speed is not None else "—"} об/хв
</strong>

</div>

<div>

<span>Пробіг</span>

<strong>
{format_number(distance)} км
</strong>

</div>

</div>

<div class="last-time">

Останній сигнал:
{format_time(last_time)}

</div>

<a
class="details"
href="/vehicle/{vehicle_id}"
>
Відкрити автомобіль →
</a>

</div>

"""

    if error:

        message = f"""

<div class="warning">

⚠️ <b>Navirec тимчасово недоступний</b>

<br><br>

{error}

</div>

"""

    else:

        message = """

<div class="success">

✅ Navirec підключений

</div>

"""

    return f"""
<!doctype html>

<html lang="uk">

<head>

<meta charset="utf-8">

<meta
name="viewport"
content="width=device-width, initial-scale=1"
>

<title>Машини — {COMPANY_NAME}</title>

<style>

body {{
    margin:0;
    font-family:Arial;
    background:#f3f4f6;
}}

.container {{
    max-width:1250px;
    margin:auto;
    padding:25px;
}}

.warning {{
    background:#fff7ed;
    border:1px solid #fdba74;
    padding:18px;
    border-radius:12px;
    margin-bottom:20px;
}}

.success {{
    background:#ecfdf5;
    border:1px solid #6ee7b7;
    padding:18px;
    border-radius:12px;
    margin-bottom:20px;
}}

.vehicles {{
    display:grid;
    grid-template-columns:
    repeat(auto-fit,minmax(320px,1fr));
    gap:20px;
}}

.vehicle-card {{
    background:white;
    padding:22px;
    border-radius:16px;
    box-shadow:
    0 3px 12px rgba(0,0,0,.08);
}}

.vehicle-card h2 {{
    margin:0 0 10px;
}}

.status {{
    display:inline-block;
    padding:7px 11px;
    border-radius:20px;
    font-weight:bold;
    font-size:13px;
}}

.moving {{
    background:#dcfce7;
    color:#166534;
}}

.idling {{
    background:#fef3c7;
    color:#92400e;
}}

.parking {{
    background:#e5e7eb;
    color:#374151;
}}

.unknown {{
    background:#fee2e2;
    color:#991b1b;
}}

.data-grid {{
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:12px;
    margin-top:20px;
}}

.data-grid div {{
    background:#f9fafb;
    padding:12px;
    border-radius:10px;
}}

.data-grid span {{
    display:block;
    color:#6b7280;
    font-size:12px;
    margin-bottom:5px;
}}

.data-grid strong {{
    font-size:16px;
}}

.last-time {{
    margin-top:18px;
    color:#6b7280;
    font-size:13px;
}}

.details {{
    display:block;
    margin-top:18px;
    padding:12px;
    background:#2563eb;
    color:white;
    text-align:center;
    text-decoration:none;
    border-radius:9px;
    font-weight:bold;
}}

.back {{
    display:inline-block;
    margin-top:25px;
    color:#2563eb;
    text-decoration:none;
    font-weight:bold;
}}

</style>

</head>

<body>

<div class="container">

<h1>🚚 Машини</h1>

{message}

<div class="vehicles">

{cards}

</div>

<a class="back" href="/">
← Головна
</a>

</div>

</body>

</html>
"""


# =========================================================
# SINGLE VEHICLE
# =========================================================

@app.route("/vehicle/<vehicle_id>")
def vehicle(vehicle_id):

    if not logged_in():

        return redirect(
            url_for("login")
        )

    vehicle_info = VEHICLES.get(
        vehicle_id
    )

    if not vehicle_info:

        return "Автомобіль не знайдено", 404

    name = vehicle_info["name"]

    states, error = get_vehicle_states()

    state = find_vehicle_state(
        states,
        vehicle_id
    )

    latitude, longitude = get_coordinates(
        state
    )

    activity = get_activity(
        state
    )

    speed = get_speed(
        state
    )

    fuel = get_fuel(
        state
    )

    distance = get_distance(
        state
    )

    heading = get_heading(
        state
    )

    ignition = get_ignition(
        state
    )

    satellites = get_satellites(
        state
    )

    altitude = get_altitude(
        state
    )

    voltage = get_voltage(
        state
    )

    engine_speed = get_engine_speed(
        state
    )

    driver = get_driver_id(
        state
    )

    last_time = get_time(
        state
    )

    driver_card = get_driver_card(
        state
    )

    raw_data = json.dumps(
        state,
        indent=2,
        ensure_ascii=False
    )

    if latitude is not None and longitude is not None:

        map_html = f"""

<div id="map"></div>

<script>

const map = L.map("map").setView(
    [{latitude}, {longitude}],
    13
);

L.tileLayer(
    "https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png",
    {{
        maxZoom:19,
        attribution:"&copy; OpenStreetMap"
    }}
).addTo(map);

const marker = L.marker(
    [{latitude}, {longitude}]
).addTo(map);

marker.bindPopup(
    "<b>{name}</b><br>" +
    "{activity}<br>" +
    "{speed if speed is not None else '—'} km/h"
).openPopup();

</script>

"""

    else:

        map_html = """

<div class="no-map">

📍 Координати зараз недоступні.

</div>

"""

    if ignition is True:

        ignition_text = "Увімкнене"

    elif ignition is False:

        ignition_text = "Вимкнене"

    else:

        ignition_text = "—"

    error_box = ""

    if error:

        error_box = f"""

<div class="warning">

⚠️ {error}

</div>

"""

    return f"""
<!doctype html>

<html lang="uk">

<head>

<meta charset="utf-8">

<meta
name="viewport"
content="width=device-width, initial-scale=1"
>

<title>{name} — {COMPANY_NAME}</title>

<link
rel="stylesheet"
href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
>

<script
src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js">
</script>

<style>

body {{
    margin:0;
    font-family:Arial;
    background:#f3f4f6;
}}

.container {{
    max-width:1250px;
    margin:auto;
    padding:25px;
}}

.header {{
    background:white;
    padding:25px;
    border-radius:16px;
    margin-bottom:20px;
}}

.status {{
    display:inline-block;
    padding:7px 12px;
    border-radius:20px;
    font-weight:bold;
}}

.grid {{
    display:grid;
    grid-template-columns:
    repeat(auto-fit,minmax(180px,1fr));
    gap:15px;
    margin-bottom:20px;
}}

.data {{
    background:white;
    padding:18px;
    border-radius:14px;
}}

.data span {{
    display:block;
    color:#6b7280;
    font-size:12px;
    margin-bottom:7px;
}}

.data strong {{
    font-size:18px;
}}

#map {{
    height:500px;
    border-radius:16px;
    overflow:hidden;
    margin-bottom:20px;
}}

.no-map {{
    background:white;
    padding:40px;
    text-align:center;
    border-radius:16px;
    margin-bottom:20px;
}}

.warning {{
    background:#fff7ed;
    border:1px solid #fdba74;
    padding:18px;
    border-radius:12px;
    margin-bottom:20px;
}}

details {{
    background:white;
    padding:18px;
    border-radius:14px;
}}

pre {{
    background:#111827;
    color:#e5e7eb;
    padding:20px;
    border-radius:10px;
    overflow:auto;
    white-space:pre-wrap;
}}

a {{
    color:#2563eb;
    text-decoration:none;
    font-weight:bold;
}}

</style>

</head>

<body>

<div class="container">

<div class="header">

<h1>
🚚 {name}
</h1>

<div class="status">
{activity}
</div>

</div>

{error_box}

<div class="grid">

<div class="data">
<span>Швидкість</span>
<strong>
{speed if speed is not None else "—"} км/год
</strong>
</div>

<div class="data">
<span>Паливо</span>
<strong>
{fuel if fuel is not None else "—"} %
</strong>
</div>

<div class="data">
<span>Пробіг</span>
<strong>
{format_number(distance)} км
</strong>
</div>

<div class="data">
<span>Напрямок</span>
<strong>
{heading if heading is not None else "—"}°
</strong>
</div>

<div class="data">
<span>Запалювання</span>
<strong>
{ignition_text}
</strong>
</div>

<div class="data">
<span>Оберти</span>
<strong>
{engine_speed if engine_speed is not None else "—"} об/хв
</strong>
</div>

<div class="data">
<span>Супутники</span>
<strong>
{satellites if satellites is not None else "—"}
</strong>
</div>

<div class="data">
<span>Висота</span>
<strong>
{altitude if altitude is not None else "—"} м
</strong>
</div>

<div class="data">
<span>Напруга</span>
<strong>
{voltage if voltage is not None else "—"} V
</strong>
</div>

<div class="data">
<span>Водій</span>
<strong>
{driver if driver else "—"}
</strong>
</div>

<div class="data">
<span>Карта водія</span>
<strong>
{driver_card if driver_card else "—"}
</strong>
</div>

<div class="data">
<span>Останній сигнал</span>
<strong>
{format_time(last_time)}
</strong>
</div>

<div class="data">
<span>Координати</span>
<strong>
{latitude if latitude is not None else "—"},
{longitude if longitude is not None else "—"}
</strong>
</div>

</div>

{map_html}

<details>

<summary>
Технічні дані Navirec
</summary>

<pre>{raw_data}</pre>

</details>

<br>

<a href="/history?vehicle={vehicle_id}">
🛣️ Історія маршруту
</a>

&nbsp;&nbsp;

<a href="/vehicles">
← До машин
</a>

&nbsp;&nbsp;

<a href="/">
Головна
</a>

</div>

</body>

</html>
"""


# =========================================================
# GPS
# =========================================================

@app.route("/gps")
def gps():

    if not logged_in():

        return redirect(
            url_for("login")
        )

    states, error = get_vehicle_states()

    markers = []

    for vehicle_id, vehicle_info in VEHICLES.items():

        state = find_vehicle_state(
            states,
            vehicle_id
        )

        latitude, longitude = get_coordinates(
            state
        )

        if latitude is None or longitude is None:

            continue

        markers.append({

            "id": vehicle_id,

            "name": vehicle_info["name"],

            "lat": latitude,

            "lon": longitude,

            "activity": get_activity(
                state
            ),

            "speed": get_speed(
                state
            ),

            "fuel": get_fuel(
                state
            ),

            "heading": get_heading(
                state
            ),

            "time": format_time(
                get_time(state)
            ),

        })

    markers_json = json.dumps(
        markers,
        ensure_ascii=False
    )

    error_box = ""

    if error:

        error_box = f"""

<div class="warning">
⚠️ {error}
</div>

"""

    return f"""
<!doctype html>

<html lang="uk">

<head>

<meta charset="utf-8">

<meta
name="viewport"
content="width=device-width, initial-scale=1"
>

<title>GPS — {COMPANY_NAME}</title>

<link
rel="stylesheet"
href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
>

<script
src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js">
</script>

<style>

body {{
    margin:0;
    font-family:Arial;
    background:#f3f4f6;
}}

.top {{
    background:white;
    padding:20px 25px;
}}

#map {{
    width:100%;
    height:calc(100vh - 150px);
    min-height:550px;
}}

.warning {{
    background:#fff7ed;
    border:1px solid #fdba74;
    padding:12px;
    border-radius:10px;
    margin-top:10px;
}}

a {{
    color:#2563eb;
    text-decoration:none;
    font-weight:bold;
}}

</style>

</head>

<body>

<div class="top">

<h1>
🗺️ {COMPANY_NAME} — GPS
</h1>

<div>
Автомобілів на карті:
<b>{len(markers)}</b>
</div>

{error_box}

<br>

<a href="/">
← Головна
</a>

&nbsp;&nbsp;

<a href="/vehicles">
🚚 Машини
</a>

</div>

<div id="map"></div>

<script>

const vehicles = {markers_json};

const map = L.map(
    "map"
).setView(
    [52.0,19.0],
    6
);

L.tileLayer(
    "https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png",
    {{
        maxZoom:19,
        attribution:"&copy; OpenStreetMap"
    }}
).addTo(map);

const bounds = [];

vehicles.forEach(
    function(vehicle) {{

        const marker = L.marker([

            vehicle.lat,
            vehicle.lon

        ]).addTo(map);

        const popup = `

        <div style="min-width:220px">

        <h3 style="margin-top:0">

        🚚 ${{vehicle.name}}

        </h3>

        <b>Стан:</b>
        ${{vehicle.activity}}

        <br>

        <b>Швидкість:</b>
        ${{vehicle.speed ?? "—"}} км/год

        <br>

        <b>Паливо:</b>
        ${{vehicle.fuel ?? "—"}} %

        <br>

        <b>Напрямок:</b>
        ${{vehicle.heading ?? "—"}}°

        <br>

        <b>Останній сигнал:</b>
        ${{vehicle.time}}

        <br><br>

        <a href="/vehicle/${{vehicle.id}}">

        Відкрити автомобіль →

        </a>

        <br><br>

        <a href="/history?vehicle=${{vehicle.id}}">

        🛣️ Історія маршруту

        </a>

        </div>

        `;

        marker.bindPopup(
            popup
        );

        bounds.push([

            vehicle.lat,
            vehicle.lon

        ]);

    }}
);

if (bounds.length > 0) {{

    map.fitBounds(
        bounds,
        {{
            padding:[50,50]
        }}
    );

}}

</script>

</body>

</html>
"""


# =========================================================
# FUEL
# =========================================================

@app.route("/fuel")
def fuel():

    if not logged_in():

        return redirect(
            url_for("login")
        )

    states, error = get_vehicle_states()

    rows = ""

    for vehicle_id, vehicle_info in VEHICLES.items():

        state = find_vehicle_state(
            states,
            vehicle_id
        )

        fuel = get_fuel(
            state
        )

        rows += f"""

<tr>

<td>
{vehicle_info["name"]}
</td>

<td>
{fuel if fuel is not None else "—"} %
</td>

<td>
{format_time(
    get_time(state)
)}
</td>

</tr>

"""

    error_text = ""

    if error:

        error_text = f"""

<div class="warning">

⚠️ {error}

</div>

"""

    return f"""
<!doctype html>

<html lang="uk">

<head>

<meta charset="utf-8">

<meta
name="viewport"
content="width=device-width, initial-scale=1"
>

<title>Паливо — {COMPANY_NAME}</title>

<style>

body {{
    margin:0;
    font-family:Arial;
    background:#f3f4f6;
}}

.container {{
    max-width:1000px;
    margin:auto;
    padding:30px;
}}

.box {{
    background:white;
    padding:20px;
    border-radius:15px;
}}

table {{
    width:100%;
    border-collapse:collapse;
}}

th,td {{
    padding:14px;
    border-bottom:
    1px solid #e5e7eb;
    text-align:left;
}}

.warning {{
    background:#fff7ed;
    border:1px solid #fdba74;
    padding:15px;
    border-radius:10px;
    margin-bottom:20px;
}}

a {{
    color:#2563eb;
    text-decoration:none;
    font-weight:bold;
}}

</style>

</head>

<body>

<div class="container">

<h1>⛽ Паливо</h1>

{error_text}

<div class="box">

<table>

<thead>

<tr>

<th>
Автомобіль
</th>

<th>
Паливо
</th>

<th>
Час даних
</th>

</tr>

</thead>

<tbody>

{rows}

</tbody>

</table>

</div>

<br>

<a href="/">
← Головна
</a>

</div>

</body>

</html>
"""


# =========================================================
# HISTORY
# =========================================================

@app.route("/history")
def history():

    if not logged_in():

        return redirect(
            url_for("login")
        )

    selected_vehicle = request.args.get(
        "vehicle",
        ""
    )

    if selected_vehicle not in VEHICLES:

        selected_vehicle = next(
            iter(VEHICLES)
        )

    selected_date = request.args.get(
        "date",
        datetime.now().strftime("%Y-%m-%d")
    )

    vehicle_name = VEHICLES[
        selected_vehicle
    ]["name"]

    states, error = get_vehicle_states()

    state = find_vehicle_state(
        states,
        selected_vehicle
    )

    trail = extract_trail_from_state(
        state
    )

    trail_distance = calculate_trail_distance(
        trail
    )

    start_time = get_trail_start_time(
        trail
    )

    end_time = get_trail_end_time(
        trail
    )

    trail_json = json.dumps(
        trail,
        ensure_ascii=False
    )

    vehicle_options = ""

    for vehicle_id, vehicle_info in VEHICLES.items():

        selected = (
            "selected"
            if vehicle_id == selected_vehicle
            else ""
        )

        vehicle_options += f"""

<option
value="{vehicle_id}"
{selected}
>
{vehicle_info["name"]}
</option>

"""

    error_box = ""

    if error:

        error_box = f"""

<div class="warning">

⚠️ <b>Navirec:</b> {error}

</div>

"""

    if trail:

        route_content = f"""

<div class="summary">

<div>

<span>
Автомобіль
</span>

<strong>
{vehicle_name}
</strong>

</div>

<div>

<span>
GPS точок
</span>

<strong>
{len(trail)}
</strong>

</div>

<div>

<span>
Довжина доступного треку
</span>

<strong>
{trail_distance} км
</strong>

</div>

<div>

<span>
Початок доступного треку
</span>

<strong>
{format_time(start_time)}
</strong>

</div>

<div>

<span>
Кінець доступного треку
</span>

<strong>
{format_time(end_time)}
</strong>

</div>

</div>

<div id="map"></div>

<div class="points">

<h2>
📍 GPS точки
</h2>

<table>

<thead>

<tr>

<th>
#
</th>

<th>
Час
</th>

<th>
Швидкість
</th>

<th>
Паливо
</th>

<th>
Широта
</th>

<th>
Довгота
</th>

</tr>

</thead>

<tbody>

"""

        for index, point in enumerate(
            trail,
            start=1
        ):

            speed_value = point.get(
                "speed"
            )

            fuel_value = point.get(
                "fuel"
            )

            route_content += f"""

<tr>

<td>
{index}
</td>

<td>
{format_time(point.get("time"))}
</td>

<td>
{speed_value if speed_value is not None else "—"}
км/год
</td>

<td>
{fuel_value if fuel_value is not None else "—"}
%
</td>

<td>
{point.get("lat", "—")}
</td>

<td>
{point.get("lon", "—")}
</td>

</tr>

"""

        route_content += """

</tbody>

</table>

</div>

"""

    else:

        route_content = """

<div class="empty">

<div class="empty-icon">
🛣️
</div>

<h2>
GPS-трек зараз недоступний
</h2>

<p>

Navirec не передав у поточному
стані автомобіля історичні точки
для побудови маршруту.

</p>

<p>

Це <b>не означає, що історії немає
в Navirec</b>.

Повна історія поїздок існує
в самому Navirec, але для її
отримання через API нам потрібен
окремий історичний endpoint.

</p>

</div>

"""

    return f"""
<!doctype html>

<html lang="uk">

<head>

<meta charset="utf-8">

<meta
name="viewport"
content="width=device-width, initial-scale=1"
>

<title>
Історія маршрутів — {COMPANY_NAME}
</title>

<link
rel="stylesheet"
href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
>

<script
src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js">
</script>

<style>

body {{

    margin:0;

    font-family:Arial,sans-serif;

    background:#f3f4f6;

    color:#111827;

}}

.container {{

    max-width:1250px;

    margin:auto;

    padding:25px;

}}

.header {{

    background:white;

    padding:25px;

    border-radius:16px;

    margin-bottom:20px;

}}

.filters {{

    background:white;

    padding:20px;

    border-radius:16px;

    margin-bottom:20px;

    display:flex;

    gap:15px;

    flex-wrap:wrap;

    align-items:end;

}}

.field {{

    display:flex;

    flex-direction:column;

    gap:7px;

}}

.field label {{

    color:#6b7280;

    font-size:13px;

}}

select,
input,
button {{

    padding:11px;

    border-radius:9px;

    border:1px solid #d1d5db;

    font-size:15px;

}}

button {{

    background:#2563eb;

    color:white;

    border:0;

    font-weight:bold;

    cursor:pointer;

}}

.warning {{

    background:#fff7ed;

    border:1px solid #fdba74;

    padding:15px;

    border-radius:12px;

    margin-bottom:20px;

}}

.summary {{

    display:grid;

    grid-template-columns:
    repeat(auto-fit,minmax(180px,1fr));

    gap:15px;

    margin-bottom:20px;

}}

.summary div {{

    background:white;

    padding:20px;

    border-radius:14px;

}}

.summary span {{

    display:block;

    color:#6b7280;

    font-size:13px;

    margin-bottom:8px;

}}

.summary strong {{

    font-size:16px;

}}

#map {{

    width:100%;

    height:550px;

    border-radius:16px;

    overflow:hidden;

    margin-bottom:20px;

}}

.points {{

    background:white;

    padding:20px;

    border-radius:16px;

    overflow:auto;

}}

table {{

    width:100%;

    border-collapse:collapse;

}}

th,
td {{

    padding:12px;

    border-bottom:
    1px solid #e5e7eb;

    text-align:left;

    white-space:nowrap;

}}

.empty {{

    background:white;

    padding:45px;

    border-radius:16px;

    text-align:center;

}}

.empty-icon {{

    font-size:55px;

}}

a {{

    color:#2563eb;

    text-decoration:none;

    font-weight:bold;

}}

</style>

</head>

<body>

<div class="container">

<div class="header">

<h1>
🛣️ Історія маршрутів
</h1>

<p>
Перегляд доступного GPS-треку автомобіля
</p>

</div>

{error_box}

<form
method="get"
class="filters"
>

<div class="field">

<label>
Автомобіль
</label>

<select name="vehicle">

{vehicle_options}

</select>

</div>

<div class="field">

<label>
Дата
</label>

<input
type="date"
name="date"
value="{selected_date}"
>

</div>

<div class="field">

<button type="submit">
Показати
</button>

</div>

</form>

{route_content}

<br>

<a href="/vehicles">
← Машини
</a>

&nbsp;&nbsp;

<a href="/">
Головна
</a>

</div>

<script>

const trail = {trail_json};

if (trail.length > 0) {{

    const first = trail[0];

    const map = L.map(
        "map"
    ).setView(
        [
            first.lat,
            first.lon
        ],
        12
    );

    L.tileLayer(
        "https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png",
        {{
            maxZoom:19,
            attribution:"&copy; OpenStreetMap"
        }}
    ).addTo(map);

    const coordinates = trail.map(
        point => [
            point.lat,
            point.lon
        ]
    );

    const route = L.polyline(
        coordinates,
        {{
            weight:5
        }}
    ).addTo(map);

    const start = trail[0];

    const finish =
        trail[trail.length - 1];

    L.marker([
        start.lat,
        start.lon
    ])
    .addTo(map)
    .bindPopup(
        "Початок доступного треку"
    );

    L.marker([
        finish.lat,
        finish.lon
    ])
    .addTo(map)
    .bindPopup(
        "Остання доступна точка"
    );

    map.fitBounds(
        route.getBounds(),
        {{
            padding:[40,40]
        }}
    );

}}

</script>

</body>

</html>
"""


# =========================================================
# TACHOGRAPH TEST
# =========================================================

@app.route("/tachograph-test")
def tachograph_test():

    if not logged_in():

        return redirect(
            url_for("login")
        )

    headers = {

        "Authorization":
            f"Token {NAVIREC_TOKEN}",

        "Accept":
            "application/x-ndjson; version=1.52.1",

    }

    try:

        response = requests.get(

            f"{NAVIREC_API}/streams/driver_states/",

            headers=headers,

            params={
                "account": ACCOUNT_ID
            },

            stream=True,

            timeout=(3,5),

        )

        first_line = None

        for line in response.iter_lines(
            decode_unicode=True
        ):

            if line:

                first_line = line

                break

        if first_line:

            try:

                result = json.dumps(

                    json.loads(first_line),

                    indent=2,

                    ensure_ascii=False

                )

            except Exception:

                result = first_line

        else:

            result = "Першої події не отримано."

        return f"""
<!doctype html>

<html lang="uk">

<head>

<meta charset="utf-8">

<title>Тахограф</title>

</head>

<body style="font-family:Arial;padding:30px">

<h1>⏱ Тахограф</h1>

<p>
<b>HTTP:</b>
{response.status_code}
</p>

<p>
<b>Content-Type:</b>
{response.headers.get("Content-Type")}
</p>

<pre>{result}</pre>

<br>

<a href="/">
← Головна
</a>

</body>

</html>
"""

    except Exception as e:

        return f"""
<!doctype html>

<html lang="uk">

<head>

<meta charset="utf-8">

<title>Тахограф</title>

</head>

<body style="font-family:Arial;padding:30px">

<h1>❌ Тахограф</h1>

<pre>{e}</pre>

<a href="/">
← Головна
</a>

</body>

</html>
"""


# =========================================================
# HEALTH
# =========================================================

@app.route("/health")
def health():

    return {

        "status": "ok",

        "service":
            "O&O TRANS Transport Platform",

        "company":
            COMPANY_NAME,

        "company_id":
            COMPANY_ID,

        "navirec":
            bool(NAVIREC_TOKEN),

        "vehicles":
            len(VEHICLES),

    }


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    app.run(

        host="0.0.0.0",

        port=int(
            os.getenv(
                "PORT",
                "10000"
            )
        )

    )
