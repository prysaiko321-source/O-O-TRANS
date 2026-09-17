import os
import json
from datetime import datetime

from flask import Flask, request, redirect, url_for, session
import requests


app = Flask(__name__)

# ============================================================
# CONFIG
# ============================================================

app.secret_key = os.environ.get(
    "SESSION_SECRET",
    "change-this-secret"
)

NAVIREC_API = "https://api.navirec.com"

NAVIREC_TOKEN = os.environ.get(
    "NAVIREC_TOKEN",
    ""
)

NAVIREC_ACCOUNT_ID = os.environ.get(
    "NAVIREC_ACCOUNT_ID",
    "5c980074-7a71-4c9b-b5a8-a7c45163adf5"
)

COMPANY_NAME = os.environ.get(
    "COMPANY_NAME",
    "O&O TRANS"
)

COMPANY_ID = os.environ.get(
    "COMPANY_ID",
    "O&O-TRANS"
)

ADMIN_USER = os.environ.get(
    "ADMIN_USER",
    ""
)

ADMIN_PASSWORD = os.environ.get(
    "ADMIN_PASSWORD",
    ""
)


# ============================================================
# VEHICLES
# ============================================================

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


# ============================================================
# HELPERS
# ============================================================

def is_logged_in():
    return session.get("logged_in") is True


def vehicle_by_id(vehicle_id):
    for vehicle in VEHICLES:
        if vehicle["id"] == vehicle_id:
            return vehicle

    return None


def normalize_vehicle_id(value):
    """
    Навірек може повернути UUID або повний URL.
    """

    if not value:
        return None

    value = str(value).strip()

    if "/vehicles/" in value:
        value = value.split(
            "/vehicles/",
            1
        )[1]

    return value.rstrip("/")


def extract_coordinates(location):

    if not location:
        return None, None

    # GeoJSON
    if isinstance(location, dict):

        coordinates = location.get(
            "coordinates"
        )

        if (
            isinstance(coordinates, (list, tuple))
            and len(coordinates) >= 2
        ):

            try:

                longitude = float(
                    coordinates[0]
                )

                latitude = float(
                    coordinates[1]
                )

                return latitude, longitude

            except (TypeError, ValueError):
                pass

        latitude = location.get(
            "latitude"
        )

        longitude = location.get(
            "longitude"
        )

        if (
            latitude is not None
            and longitude is not None
        ):

            try:

                return (
                    float(latitude),
                    float(longitude)
                )

            except (TypeError, ValueError):
                pass

    # [longitude, latitude]
    if isinstance(
        location,
        (list, tuple)
    ):

        if len(location) >= 2:

            try:

                longitude = float(
                    location[0]
                )

                latitude = float(
                    location[1]
                )

                return latitude, longitude

            except (TypeError, ValueError):
                pass

    return None, None


def navirec_headers(
    accept="application/json; version=1.52.1"
):

    return {
        "Authorization":
            f"Token {NAVIREC_TOKEN}",

        "Accept":
            accept,

        "User-Agent":
            "OO-TRANS-Transport-Platform/1.0"
    }


def format_time(value):

    if not value:
        return "—"

    try:

        dt = datetime.fromisoformat(
            str(value).replace(
                "Z",
                "+00:00"
            )
        )

        return dt.astimezone().strftime(
            "%d.%m.%Y %H:%M:%S"
        )

    except Exception:
        return str(value)


def format_number(
    value,
    decimals=1
):

    if value is None:
        return "—"

    try:

        return (
            f"{float(value):,.{decimals}f}"
            .replace(",", " ")
        )

    except Exception:
        return str(value)


def safe_float(value):

    try:
        return float(value)

    except Exception:
        return None


def get_activity(activity):

    mapping = {
        "driving": "Рухається",
        "parking": "Стоїть",
        "stopped": "Зупинка",
        "idling": "Двигун працює — стоїть",
        "offline": "Немає зв'язку",
        "towing": "Буксирування"
    }

    return mapping.get(
        activity,
        activity or "Невідомо"
    )


# ============================================================
# NAVIREC CURRENT STATES
# ============================================================

def get_vehicle_states():

    if not NAVIREC_TOKEN:
        return []

    try:

        response = requests.get(
            f"{NAVIREC_API}/last_vehicle_states/",
            headers=navirec_headers(),
            params={
                "account": NAVIREC_ACCOUNT_ID
            },
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


def state_map_by_vehicle(states):

    result = {}

    for state in states:

        vehicle_id = normalize_vehicle_id(
            state.get("vehicle")
        )

        if vehicle_id:
            result[vehicle_id] = state

    return result


def state_for_vehicle(vehicle_id):

    target = normalize_vehicle_id(
        vehicle_id
    )

    states = get_vehicle_states()

    for state in states:

        current_id = normalize_vehicle_id(
            state.get("vehicle")
        )

        if current_id == target:
            return state

    return None


# ============================================================
# VEHICLE HISTORY
# ============================================================

def get_vehicle_history(
    vehicle_id,
    date_string
):

    if not NAVIREC_TOKEN:

        return {
            "ok": False,
            "error": "NAVIREC_TOKEN не налаштований.",
            "points": []
        }

    try:

        start_time = (
            f"{date_string}"
            "T00:00:00+02:00"
        )

        end_time = (
            f"{date_string}"
            "T23:59:59+02:00"
        )

        response = requests.get(
            f"{NAVIREC_API}/vehicle_history/",
            headers=navirec_headers(),
            params={
                "vehicle": vehicle_id,
                "start_time": start_time,
                "end_time": end_time,
                "format": "json"
            },
            timeout=45
        )

        if response.status_code != 200:

            return {
                "ok": False,
                "error":
                    f"Navirec HTTP {response.status_code}: "
                    f"{response.text[:1000]}",
                "points": []
            }

        data = response.json()

        if not isinstance(data, list):

            return {
                "ok": False,
                "error":
                    "Navirec повернув не список GPS точок.",
                "points": []
            }

        points = []

        for item in data:

            latitude, longitude = (
                extract_coordinates(
                    item.get("location")
                )
            )

            if (
                latitude is None
                or longitude is None
            ):
                continue

            points.append({
                "id":
                    item.get("id"),

                "time":
                    item.get("time"),

                "latitude":
                    latitude,

                "longitude":
                    longitude,

                "activity":
                    item.get("activity"),

                "speed":
                    item.get("speed"),

                "heading":
                    item.get("heading"),

                "fuel_level":
                    item.get("fuel_level"),

                "altitude":
                    item.get("altitude"),

                "engine_speed":
                    item.get("engine_speed"),

                "ignition":
                    item.get("ignition"),

                "driver_name":
                    item.get("driver_name"),

                "driver_surname":
                    item.get("driver_surname"),

                "total_distance":
                    item.get("total_distance"),

                "accumulated_distance":
                    item.get("accumulated_distance"),

                "accumulated_driving_distance":
                    item.get(
                        "accumulated_driving_distance"
                    ),

                "total_engine_time":
                    item.get("total_engine_time"),

                "satellites":
                    item.get("satellites")
            })

        points.sort(
            key=lambda x:
                x.get("time") or ""
        )

        return {
            "ok": True,
            "error": None,
            "points": points
        }

    except requests.RequestException as exc:

        return {
            "ok": False,
            "error":
                f"Помилка з'єднання з Navirec: {exc}",
            "points": []
        }

    except Exception as exc:

        return {
            "ok": False,
            "error":
                f"Помилка обробки історії: {exc}",
            "points": []
        }


# ============================================================
# NAVIREC DAILY TOTALS
# ============================================================

def get_vehicle_timeline_totals(
    vehicle_id,
    date_string
):

    if not NAVIREC_TOKEN:
        return None

    try:

        start_time = (
            f"{date_string}"
            "T00:00:00+02:00"
        )

        end_time = (
            f"{date_string}"
            "T23:59:59+02:00"
        )

        response = requests.get(
            f"{NAVIREC_API}/vehicle_timeline/totals/",
            headers=navirec_headers(),
            params={
                "vehicle": vehicle_id,
                "start_time": start_time,
                "end_time": end_time
            },
            timeout=30
        )

        if response.status_code != 200:
            return None

        data = response.json()

        if isinstance(data, dict):
            return data

        return None

    except Exception:
        return None


# ============================================================
# CSS
# ============================================================

BASE_STYLE = """
<style>

body {
    font-family: Arial, sans-serif;
    margin: 0;
    background: #f4f6f8;
    color: #1f2937;
}

.topbar {
    background: #111827;
    color: white;
    padding: 14px 22px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 10px;
}

.topbar a {
    color: white;
    text-decoration: none;
    margin-right: 15px;
    font-size: 14px;
}

.container {
    max-width: 1400px;
    margin: 0 auto;
    padding: 22px;
}

.card,
.vehicle-card,
.stat {
    background: white;
    border-radius: 12px;
    padding: 18px;
    margin-bottom: 16px;
    box-shadow:
        0 2px 10px rgba(0,0,0,.06);
}

.grid {
    display: grid;
    grid-template-columns:
        repeat(
            auto-fit,
            minmax(210px, 1fr)
        );
    gap: 14px;
}

.stat-title {
    font-size: 13px;
    color: #6b7280;
    margin-bottom: 7px;
}

.stat-value {
    font-size: 23px;
    font-weight: bold;
}

.btn {
    display: inline-block;
    background: #2563eb;
    color: white;
    padding: 10px 15px;
    border-radius: 8px;
    text-decoration: none;
    border: none;
    cursor: pointer;
}

.btn-secondary {
    background: #374151;
}

.status-driving {
    color: #059669;
    font-weight: bold;
}

.status-idling {
    color: #d97706;
    font-weight: bold;
}

.status-parking,
.status-stopped {
    color: #6b7280;
    font-weight: bold;
}

.status-offline {
    color: #dc2626;
    font-weight: bold;
}

#map {
    height: 600px;
    width: 100%;
    border-radius: 12px;
}

.small {
    color: #6b7280;
    font-size: 13px;
}

.error {
    background: #fee2e2;
    color: #991b1b;
    padding: 14px;
    border-radius: 10px;
    margin-bottom: 15px;
    white-space: pre-wrap;
}

.success {
    background: #dcfce7;
    color: #166534;
    padding: 14px;
    border-radius: 10px;
}

.table-wrap {
    overflow-x: auto;
}

table {
    border-collapse: collapse;
    width: 100%;
    background: white;
}

th,
td {
    padding: 9px;
    border-bottom:
        1px solid #e5e7eb;
    text-align: left;
    white-space: nowrap;
    font-size: 13px;
}

th {
    background: #f9fafb;
}

pre {
    overflow-x: auto;
}

select,
input {
    padding: 10px;
    border: 1px solid #d1d5db;
    border-radius: 8px;
    font-size: 15px;
    width: 100%;
    box-sizing: border-box;
}

label {
    display: block;
    font-weight: bold;
    margin-bottom: 7px;
}

.form-grid {
    display: grid;
    grid-template-columns:
        1fr 1fr auto;
    gap: 14px;
    align-items: end;
}

@media (max-width: 800px) {

    .form-grid {
        grid-template-columns: 1fr;
    }

    #map {
        height: 450px;
    }

    .topbar a {
        display: inline-block;
        margin: 5px;
    }

}

</style>
"""


def page(title, content):

    return f"""
<!DOCTYPE html>

<html lang="uk">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width,
             initial-scale=1.0"
>

<title>
    {title} — {COMPANY_NAME}
</title>

{BASE_STYLE}

</head>

<body>

<div class="topbar">

<strong>
    🚚 {COMPANY_NAME}
</strong>

<div>

<a href="/">
Головна
</a>

<a href="/vehicles">
Автомобілі
</a>

<a href="/gps">
GPS
</a>

<a href="/history">
Історія
</a>

<a href="/fuel">
Паливо
</a>

<a href="/tachograph-test">
Тахограф
</a>

<a href="/health">
Health
</a>

<a href="/logout">
Вийти
</a>

</div>

</div>

<div class="container">

{content}

</div>

</body>

</html>
"""


# ============================================================
# LOGIN
# ============================================================

@app.route(
    "/login",
    methods=["GET", "POST"]
)
def login():

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

            return redirect("/")

        error = """
        <div class="error">
            Неправильний логін або пароль.
        </div>
        """

    else:

        error = ""

    return page(
        "Вхід",
        f"""

        <div class="card">

            <h1>
                🔐 O&O TRANS
            </h1>

            {error}

            <form method="post">

                <p>

                    <label>
                        Логін
                    </label>

                    <input name="username">

                </p>

                <p>

                    <label>
                        Пароль
                    </label>

                    <input
                        name="password"
                        type="password"
                    >

                </p>

                <button
                    class="btn"
                    type="submit"
                >
                    Увійти
                </button>

            </form>

        </div>

        """
    )


@app.route("/logout")
def logout():

    session.clear()

    return redirect("/login")


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    if not is_logged_in():
        return redirect("/login")

    states = get_vehicle_states()

    state_map = state_map_by_vehicle(
        states
    )

    cards = ""

    for vehicle in VEHICLES:

        state = state_map.get(
            vehicle["id"]
        )

        if state:

            activity = state.get(
                "activity"
            )

            cards += f"""

            <div class="vehicle-card">

                <h2>
                    🚚 {vehicle["name"]}
                </h2>

                <p class="status-{activity}">
                    {get_activity(activity)}
                </p>

                <div class="grid">

                    <div>
                        <b>Швидкість</b><br>
                        {format_number(
                            state.get("speed"),
                            0
                        )} км/год
                    </div>

                    <div>
                        <b>Паливо</b><br>
                        {format_number(
                            state.get("fuel_level"),
                            1
                        )}%
                    </div>

                    <div>
                        <b>Оберти</b><br>
                        {format_number(
                            state.get("engine_speed"),
                            0
                        )} rpm
                    </div>

                    <div>
                        <b>Пробіг</b><br>
                        {format_number(
                            (
                                state.get(
                                    "total_distance"
                                ) or 0
                            ) / 1000,
                            1
                        )} км
                    </div>

                </div>

                <p>

                    <a
                        class="btn"
                        href="/vehicle/{vehicle["id"]}"
                    >
                        Відкрити автомобіль
                    </a>

                    <a
                        class="btn btn-secondary"
                        href="/history?vehicle={vehicle["id"]}"
                    >
                        Історія
                    </a>

                </p>

            </div>

            """

        else:

            cards += f"""

            <div class="vehicle-card">

                <h2>
                    🚚 {vehicle["name"]}
                </h2>

                <p class="status-offline">
                    Немає актуального стану Navirec
                </p>

                <a
                    class="btn"
                    href="/vehicle/{vehicle["id"]}"
                >
                    Відкрити автомобіль
                </a>

            </div>

            """

    return page(
        "Головна",
        f"""

        <h1>
            🚚 Панель O&O TRANS
        </h1>

        {cards}

        """
    )


# ============================================================
# VEHICLES
# ============================================================

@app.route("/vehicles")
def vehicles():

    if not is_logged_in():
        return redirect("/login")

    states = get_vehicle_states()

    state_map = state_map_by_vehicle(
        states
    )

    rows = ""

    for vehicle in VEHICLES:

        state = state_map.get(
            vehicle["id"]
        )

        if state:

            status = get_activity(
                state.get("activity")
            )

            speed = format_number(
                state.get("speed"),
                0
            )

            fuel = format_number(
                state.get("fuel_level"),
                1
            )

        else:

            status = "Немає актуальних даних"
            speed = "—"
            fuel = "—"

        rows += f"""

        <tr>

            <td>
                <b>{vehicle["name"]}</b>
            </td>

            <td>
                {status}
            </td>

            <td>
                {speed} км/год
            </td>

            <td>
                {fuel}%
            </td>

            <td>

                <a
                    class="btn"
                    href="/vehicle/{vehicle["id"]}"
                >
                    Відкрити
                </a>

            </td>

        </tr>

        """

    return page(
        "Автомобілі",
        f"""

        <h1>
            🚚 Автомобілі O&O TRANS
        </h1>

        <div class="card table-wrap">

            <table>

                <thead>

                    <tr>

                        <th>
                            Автомобіль
                        </th>

                        <th>
                            Статус
                        </th>

                        <th>
                            Швидкість
                        </th>

                        <th>
                            Паливо
                        </th>

                        <th>
                        </th>

                    </tr>

                </thead>

                <tbody>

                    {rows}

                </tbody>

            </table>

        </div>

        """
    )


# ============================================================
# VEHICLE PAGE
# ============================================================

@app.route(
    "/vehicle/<vehicle_id>"
)
def vehicle_page(vehicle_id):

    if not is_logged_in():
        return redirect("/login")

    vehicle = vehicle_by_id(
        vehicle_id
    )

    if not vehicle:

        return (
            "Автомобіль не знайдений",
            404
        )

    state = state_for_vehicle(
        vehicle_id
    )

    if not state:

        return page(
            vehicle["name"],
            f"""

            <h1>
                🚚 {vehicle["name"]}
            </h1>

            <div class="error">

                Navirec зараз не передав
                актуальний стан цього
                автомобіля.

                <br><br>

                Сам автомобіль присутній
                у системі O&O TRANS.

            </div>

            <a
                class="btn"
                href="/history?vehicle={vehicle_id}"
            >
                Перевірити історію
            </a>

            """
        )

    activity = state.get(
        "activity"
    )

    latitude, longitude = (
        extract_coordinates(
            state.get("location")
        )
    )

    driver = (
        f'{state.get("driver_name") or ""} '
        f'{state.get("driver_surname") or ""}'
    ).strip()

    if not driver:
        driver = "—"

    map_html = ""

    if (
        latitude is not None
        and longitude is not None
    ):

        map_html = f"""

        <div class="card">

            <h2>
                📍 Поточне місцезнаходження
            </h2>

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

        const map =
            L.map('map').setView(
                [{latitude}, {longitude}],
                12
            );

        L.tileLayer(
            'https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
            {{
                maxZoom: 19,
                attribution:
                    '&copy; OpenStreetMap'
            }}
        ).addTo(map);

        L.marker(
            [{latitude}, {longitude}]
        )
        .addTo(map)
        .bindPopup(
            "<strong>{vehicle["name"]}</strong><br>" +
            "{get_activity(activity)}<br>" +
            "Швидкість: " +
            "{format_number(state.get("speed"),0)} км/год"
        )
        .openPopup();

        </script>

        """

    else:

        map_html = """

        <div class="card">

            <h2>
                📍 Місцезнаходження
            </h2>

            <p>
                Navirec зараз не передав
                координати цього автомобіля.
            </p>

        </div>

        """

    return page(
        vehicle["name"],
        f"""

        <h1>
            🚚 {vehicle["name"]}
        </h1>

        <div class="grid">

            <div class="stat">

                <div class="stat-title">
                    Статус
                </div>

                <div class="stat-value">
                    {get_activity(activity)}
                </div>

            </div>

            <div class="stat">

                <div class="stat-title">
                    Швидкість
                </div>

                <div class="stat-value">
                    {format_number(
                        state.get("speed"),
                        0
                    )} км/год
                </div>

            </div>

            <div class="stat">

                <div class="stat-title">
                    Паливо
                </div>

                <div class="stat-value">
                    {format_number(
                        state.get("fuel_level"),
                        1
                    )}%
                </div>

            </div>

            <div class="stat">

                <div class="stat-title">
                    Оберти
                </div>

                <div class="stat-value">
                    {format_number(
                        state.get("engine_speed"),
                        0
                    )}
                </div>

            </div>

        </div>

        <div class="card">

            <h2>
                📊 Технічні дані
            </h2>

            <div class="grid">

                <div>
                    <b>Пробіг</b><br>
                    {format_number(
                        (
                            state.get(
                                "total_distance"
                            ) or 0
                        ) / 1000,
                        1
                    )} км
                </div>

                <div>
                    <b>Напрямок</b><br>
                    {state.get("heading","—")}°
                </div>

                <div>
                    <b>Висота</b><br>
                    {state.get("altitude","—")} м
                </div>

                <div>
                    <b>Супутники</b><br>
                    {state.get("satellites","—")}
                </div>

                <div>
                    <b>Напруга</b><br>
                    {format_number(
                        state.get(
                            "supply_voltage"
                        ),
                        2
                    )} V
                </div>

                <div>
                    <b>Запалювання</b><br>
                    {
                        "Увімкнено"
                        if state.get("ignition")
                        else "Вимкнено"
                    }
                </div>

                <div>
                    <b>Водій</b><br>
                    {driver}
                </div>

                <div>
                    <b>Останній сигнал</b><br>
                    {format_time(
                        state.get("time")
                    )}
                </div>

                <div>
                    <b>Широта</b><br>
                    {latitude if latitude is not None else "—"}
                </div>

                <div>
                    <b>Довгота</b><br>
                    {longitude if longitude is not None else "—"}
                </div>

            </div>

        </div>

        {map_html}

        <div class="card">

            <h2>
                🛣️ Історія маршруту
            </h2>

            <a
                class="btn"
                href="/history?vehicle={vehicle_id}"
            >
                Відкрити історію
            </a>

        </div>

        """
    )


# ============================================================
# GPS ALL VEHICLES
# ============================================================

@app.route("/gps")
def gps():

    if not is_logged_in():
        return redirect("/login")

    states = get_vehicle_states()

    state_map = state_map_by_vehicle(
        states
    )

    markers = []

    for vehicle in VEHICLES:

        state = state_map.get(
            vehicle["id"]
        )

        if not state:
            continue

        latitude, longitude = (
            extract_coordinates(
                state.get("location")
            )
        )

        if (
            latitude is None
            or longitude is None
        ):
            continue

        markers.append({
            "name":
                vehicle["name"],

            "lat":
                latitude,

            "lon":
                longitude,

            "speed":
                state.get("speed"),

            "activity":
                get_activity(
                    state.get("activity")
                )
        })

    markers_json = json.dumps(
        markers,
        ensure_ascii=False
    )

    return page(
        "GPS",
        f"""

        <h1>
            📍 GPS усіх автомобілів
        </h1>

        <div class="card">

            <p class="small">
                На карті показані всі автомобілі,
                для яких Navirec передав координати.
            </p>

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

        const vehicles =
            {markers_json};

        const map =
            L.map('map').setView(
                [51.5, 10.0],
                6
            );

        L.tileLayer(
            'https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
            {{
                maxZoom: 19,
                attribution:
                    '&copy; OpenStreetMap'
            }}
        ).addTo(map);

        const bounds = [];

        vehicles.forEach(
            function(vehicle) {{

                const marker =
                    L.marker([
                        vehicle.lat,
                        vehicle.lon
                    ]).addTo(map);

                marker.bindPopup(
                    "<strong>" +
                    vehicle.name +
                    "</strong><br>" +
                    vehicle.activity +
                    "<br>" +
                    "Швидкість: " +
                    (
                        vehicle.speed ??
                        "—"
                    ) +
                    " км/год"
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
                    padding: [30, 30]
                }}
            );

        }}

        </script>

        """
    )


# ============================================================
# HISTORY
# ============================================================

@app.route("/history")
def history():

    if not is_logged_in():
        return redirect("/login")

    vehicle_id = request.args.get(
        "vehicle",
        VEHICLES[0]["id"]
    )

    if not vehicle_by_id(vehicle_id):

        vehicle_id = VEHICLES[0]["id"]

    date_string = request.args.get(
        "date",
        datetime.now().strftime(
            "%Y-%m-%d"
        )
    )

    vehicle = vehicle_by_id(
        vehicle_id
    )

    result = get_vehicle_history(
        vehicle_id,
        date_string
    )

    points = result.get(
        "points",
        []
    )

    error_html = ""

    if not result.get("ok"):

        error_html = f"""

        <div class="error">

            ❌ Не вдалося отримати історію.

            {result.get(
                "error",
                "Невідома помилка"
            )}

        </div>

        """

    elif not points:

        error_html = """

        <div class="card">

            <h2>
                🛣️ Історії не знайдено
            </h2>

            <p>
                Для вибраного автомобіля
                та дати Navirec не повернув
                GPS-точок.
            </p>

        </div>

        """

    first_point = (
        points[0]
        if points
        else None
    )

    last_point = (
        points[-1]
        if points
        else None
    )

    # --------------------------------------------------------
    # TOTALS NAVIREC
    # --------------------------------------------------------

    totals_data = (
        get_vehicle_timeline_totals(
            vehicle_id,
            date_string
        )
    )

    totals = {}

    if isinstance(
        totals_data,
        dict
    ):

        totals = totals_data.get(
            "totals",
            {}
        )

    driving_distance = totals.get(
        "driving_distance"
    )

    driving_time = totals.get(
        "driving_time"
    )

    parking_time = totals.get(
        "parking_time"
    )

    idling_time = totals.get(
        "idling_time"
    )

    fuel_used = totals.get(
        "fuel_used_100km"
    )

    # --------------------------------------------------------
    # DISTANCE
    # --------------------------------------------------------

    distance_text = "—"

    if driving_distance is not None:

        distance_text = (
            f"{format_number(
                driving_distance,
                2
            )} км"
        )

    elif first_point and last_point:

        first_distance = safe_float(
            first_point.get(
                "accumulated_driving_distance"
            )
        )

        last_distance = safe_float(
            last_point.get(
                "accumulated_driving_distance"
            )
        )

        if (
            first_distance is not None
            and last_distance is not None
        ):

            distance_km = (
                last_distance -
                first_distance
            ) / 1000

            if distance_km < 0:
                distance_km = 0

            distance_text = (
                f"{format_number(
                    distance_km,
                    1
                )} км"
            )

    # --------------------------------------------------------
    # SPEED
    # --------------------------------------------------------

    speeds = []

    for point in points:

        speed = safe_float(
            point.get("speed")
        )

        if speed is not None:
            speeds.append(speed)

    max_speed = (
        max(speeds)
        if speeds
        else None
    )

    # --------------------------------------------------------
    # FUEL
    # --------------------------------------------------------

    fuel_start = (
        first_point.get(
            "fuel_level"
        )
        if first_point
        else None
    )

    fuel_end = (
        last_point.get(
            "fuel_level"
        )
        if last_point
        else None
    )

    # --------------------------------------------------------
    # ROUTE
    # --------------------------------------------------------

    route_points = []

    for point in points:

        route_points.append([
            point["latitude"],
            point["longitude"]
        ])

    route_json = json.dumps(
        route_points,
        ensure_ascii=False
    )

    map_html = ""

    if points:

        center = points[
            len(points) // 2
        ]

        center_lat = center["latitude"]
        center_lon = center["longitude"]

        first_time = format_time(
            first_point.get("time")
        )

        last_time = format_time(
            last_point.get("time")
        )

        map_html = f"""

        <div class="card">

            <h2>
                🗺️ Маршрут {vehicle["name"]}
            </h2>

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

        const route =
            {route_json};

        const map =
            L.map('map').setView(
                [{center_lat}, {center_lon}],
                8
            );

        L.tileLayer(
            'https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
            {{
                maxZoom: 19,
                attribution:
                    '&copy; OpenStreetMap'
            }}
        ).addTo(map);

        const polyline =
            L.polyline(
                route,
                {{
                    weight: 5
                }}
            ).addTo(map);

        L.marker(
            route[0]
        )
        .addTo(map)
        .bindPopup(
            "Початок маршруту<br>{first_time}"
        );

        L.marker(
            route[route.length - 1]
        )
        .addTo(map)
        .bindPopup(
            "Кінець маршруту<br>{last_time}"
        );

        map.fitBounds(
            polyline.getBounds(),
            {{
                padding: [30, 30]
            }}
        );

        </script>

        """

    # --------------------------------------------------------
    # VEHICLE SELECT
    # --------------------------------------------------------

    vehicle_options = ""

    for v in VEHICLES:

        selected = ""

        if v["id"] == vehicle_id:
            selected = "selected"

        vehicle_options += f"""

        <option
            value="{v["id"]}"
            {selected}
        >
            {v["name"]}
        </option>

        """

    # --------------------------------------------------------
    # STATS
    # --------------------------------------------------------

    stats_html = ""

    if points:

        stats_html = f"""

        <div class="grid">

            <div class="stat">

                <div class="stat-title">
                    GPS точок
                </div>

                <div class="stat-value">
                    {len(points)}
                </div>

            </div>

            <div class="stat">

                <div class="stat-title">
                    Початок
                </div>

                <div
                    class="stat-value"
                    style="font-size:18px"
                >
                    {format_time(
                        first_point.get("time")
                    )}
                </div>

            </div>

            <div class="stat">

                <div class="stat-title">
                    Кінець
                </div>

                <div
                    class="stat-value"
                    style="font-size:18px"
                >
                    {format_time(
                        last_point.get("time")
                    )}
                </div>

            </div>

            <div class="stat">

                <div class="stat-title">
                    Відстань руху
                </div>

                <div class="stat-value">
                    {distance_text}
                </div>

            </div>

            <div class="stat">

                <div class="stat-title">
                    Максимальна швидкість
                </div>

                <div class="stat-value">
                    {format_number(
                        max_speed,
                        0
                    )} км/год
                </div>

            </div>

            <div class="stat">

                <div class="stat-title">
                    Паливо на початку
                </div>

                <div class="stat-value">
                    {format_number(
                        fuel_start,
                        1
                    )}%
                </div>

            </div>

            <div class="stat">

                <div class="stat-title">
                    Паливо в кінці
                </div>

                <div class="stat-value">
                    {format_number(
                        fuel_end,
                        1
                    )}%
                </div>

            </div>

        </div>

        """

    # --------------------------------------------------------
    # TOTALS
    # --------------------------------------------------------

    totals_html = ""

    if totals_data:

        totals_html = f"""

        <div class="card">

            <h2>
                📊 Підсумок Navirec
            </h2>

            <div class="grid">

                <div>
                    <b>Відстань руху</b><br>
                    {driving_distance
                        if driving_distance is not None
                        else "—"} км
                </div>

                <div>
                    <b>Час руху</b><br>
                    {driving_time or "—"}
                </div>

                <div>
                    <b>Стоянка</b><br>
                    {parking_time or "—"}
                </div>

                <div>
                    <b>Холостий хід</b><br>
                    {idling_time or "—"}
                </div>

                <div>
                    <b>Паливо / 100 км</b><br>
                    {fuel_used or "—"}
                </div>

            </div>

        </div>

        """

    # --------------------------------------------------------
    # GPS TABLE
    # --------------------------------------------------------

    table_rows = ""

    for point in points[-100:]:

        table_rows += f"""

        <tr>

            <td>
                {format_time(
                    point.get("time")
                )}
            </td>

            <td>
                {get_activity(
                    point.get("activity")
                )}
            </td>

            <td>
                {format_number(
                    point.get("speed"),
                    0
                )}
            </td>

            <td>
                {format_number(
                    point.get("fuel_level"),
                    1
                )}%
            </td>

            <td>
                {format_number(
                    point.get("engine_speed"),
                    0
                )}
            </td>

            <td>
                {format_number(
                    point.get("altitude"),
                    0
                )} m
            </td>

            <td>
                {format_number(
                    point.get("latitude"),
                    6
                )}
            </td>

            <td>
                {format_number(
                    point.get("longitude"),
                    6
                )}
            </td>

        </tr>

        """

    points_html = ""

    if points:

        points_html = f"""

        <div class="card">

            <h2>
                📍 GPS точки
            </h2>

            <p class="small">

                Показано останні
                {min(len(points),100)}
                із {len(points)} точок.

                На карті відображено
                весь отриманий маршрут.

            </p>

            <div class="table-wrap">

                <table>

                    <thead>

                        <tr>

                            <th>Час</th>
                            <th>Статус</th>
                            <th>Швидкість</th>
                            <th>Паливо</th>
                            <th>Оберти</th>
                            <th>Висота</th>
                            <th>Широта</th>
                            <th>Довгота</th>

                        </tr>

                    </thead>

                    <tbody>

                        {table_rows}

                    </tbody>

                </table>

            </div>

        </div>

        """

    return page(
        "Історія маршрутів",
        f"""

        <h1>
            🛣️ Історія маршрутів
        </h1>

        <div class="card">

            <form method="get">

                <div class="form-grid">

                    <div>

                        <label>
                            Автомобіль
                        </label>

                        <select name="vehicle">

                            {vehicle_options}

                        </select>

                    </div>

                    <div>

                        <label>
                            Дата
                        </label>

                        <input
                            type="date"
                            name="date"
                            value="{date_string}"
                            required
                        >

                    </div>

                    <button
                        class="btn"
                        type="submit"
                    >
                        Показати маршрут
                    </button>

                </div>

            </form>

        </div>

        {error_html}

        {stats_html}

        {totals_html}

        {map_html}

        {points_html}

        """
    )


# ============================================================
# FUEL
# ============================================================

@app.route("/fuel")
def fuel():

    if not is_logged_in():
        return redirect("/login")

    states = get_vehicle_states()

    state_map = state_map_by_vehicle(
        states
    )

    rows = ""

    for vehicle in VEHICLES:

        state = state_map.get(
            vehicle["id"]
        )

        if state:

            fuel = format_number(
                state.get("fuel_level"),
                1
            )

            fuel_ewma = format_number(
                state.get(
                    "fuel_level_ewma"
                ),
                1
            )

            updated = format_time(
                state.get("time")
            )

        else:

            fuel = "—"
            fuel_ewma = "—"
            updated = "—"

        rows += f"""

        <tr>

            <td>
                {vehicle["name"]}
            </td>

            <td>
                {fuel}%
            </td>

            <td>
                {fuel_ewma}%
            </td>

            <td>
                {updated}
            </td>

        </tr>

        """

    return page(
        "Паливо",
        f"""

        <h1>
            ⛽ Паливо
        </h1>

        <div class="card table-wrap">

            <table>

                <tr>

                    <th>
                        Автомобіль
                    </th>

                    <th>
                        Паливо
                    </th>

                    <th>
                        EWMA
                    </th>

                    <th>
                        Оновлено
                    </th>

                </tr>

                {rows}

            </table>

        </div>

        """
    )


# ============================================================
# NAVIREC DEBUG
# ============================================================

@app.route("/navirec-debug")
def navirec_debug():

    if not is_logged_in():
        return redirect("/login")

    states = get_vehicle_states()

    received = []

    for state in states:

        received.append({
            "vehicle":
                state.get("vehicle"),

            "normalized_vehicle_id":
                normalize_vehicle_id(
                    state.get("vehicle")
                ),

            "time":
                state.get("time"),

            "activity":
                state.get("activity"),

            "location":
                state.get("location"),

            "speed":
                state.get("speed"),

            "fuel_level":
                state.get("fuel_level"),

            "engine_speed":
                state.get(
                    "engine_speed"
                ),

            "total_distance":
                state.get(
                    "total_distance"
                )
        })

    debug_data = {
        "account_id":
            NAVIREC_ACCOUNT_ID,

        "token_configured":
            bool(NAVIREC_TOKEN),

        "states_count":
            len(states),

        "vehicles_expected":
            VEHICLES,

        "vehicles_received":
            received
    }

    return page(
        "Navirec Debug",
        f"""

        <h1>
            🔧 Navirec Debug
        </h1>

        <div class="card">

            <pre style="
                white-space:pre-wrap;
                background:#111827;
                color:#f9fafb;
                padding:16px;
                border-radius:10px;
            ">{json.dumps(
                debug_data,
                ensure_ascii=False,
                indent=2,
                default=str
            )}</pre>

        </div>

        """
    )


# ============================================================
# TACHOGRAPH TEST
# ============================================================

@app.route("/tachograph-test")
def tachograph_test():

    if not is_logged_in():
        return redirect("/login")

    try:

        response = requests.get(
            f"{NAVIREC_API}/streams/driver_states/",
            headers=navirec_headers(
                "application/x-ndjson; version=1.52.1"
            ),
            params={
                "account":
                    NAVIREC_ACCOUNT_ID
            },
            timeout=10,
            stream=True
        )

        result = (
            f"HTTP status: {response.status_code}\n"
            f"Content-Type: "
            f"{response.headers.get('Content-Type')}\n\n"
        )

        try:

            first_line = next(
                response.iter_lines(
                    decode_unicode=True
                )
            )

            result += (
                "Перша відповідь:\n"
                f"{first_line}\n"
            )

        except Exception as exc:

            result += (
                "Не вдалося отримати "
                f"перший рядок: {exc}\n"
            )

    except Exception as exc:

        result = f"Помилка: {exc}"

    return page(
        "Тахограф",
        f"""

        <h1>
            ⏱️ Тахограф / Driver States
        </h1>

        <div class="card">

            <pre style="
                white-space:pre-wrap;
                background:#111827;
                color:#f9fafb;
                padding:16px;
                border-radius:10px;
            ">{result}</pre>

        </div>

        """
    )


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    return {
        "status":
            "ok",

        "company":
            COMPANY_NAME,

        "company_id":
            COMPANY_ID,

        "vehicles_configured":
            len(VEHICLES),

        "navirec_token_configured":
            bool(NAVIREC_TOKEN),

        "navirec_account_id_configured":
            bool(NAVIREC_ACCOUNT_ID)
    }


# ============================================================
# RUN
# ============================================================

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
