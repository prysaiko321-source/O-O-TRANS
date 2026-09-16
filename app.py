import os
import json
from flask import Flask, redirect, url_for, session, request
import requests

app = Flask(__name__)

app.secret_key = os.getenv("SESSION_SECRET", "change-me")

NAVIREC_TOKEN = os.getenv("NAVIREC_TOKEN")
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")

NAVIREC_API = "https://api.navirec.com"

ACCOUNT_ID = "5c980074-7a71-4c9b-b5a8-a7c45163adf5"

VEHICLES = {
    "aaaa9acd-5bb5-467e-8241-81444292bbfe": "Renault Master SH 9203G",
    "cbb121b6-34dd-41c6-974b-5b7aa3d9a1cb": "Renault Master DX 9034F",
    "f016af91-dee6-4e72-9f86-4b2e27a253c1": "Renault Master DX 5405A",
}


# ---------------------------------------------------------
# LOGIN
# ---------------------------------------------------------

def logged_in():
    return session.get("logged_in") is True


@app.route("/login", methods=["GET", "POST"])
def login():

    error = ""

    if request.method == "POST":

        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if username == ADMIN_USER and password == ADMIN_PASSWORD:

            session["logged_in"] = True

            return redirect(url_for("home"))

        error = "Неправильний логін або пароль"

    return f"""
    <!doctype html>
    <html lang="uk">

    <head>
        <meta charset="utf-8">
        <title>O&O TRANS — Login</title>

        <style>

            body {{
                margin:0;
                font-family:Arial;
                background:#111827;
                color:white;

                display:flex;
                justify-content:center;
                align-items:center;

                min-height:100vh;
            }}

            .login {{
                width:320px;
                background:#1f2937;
                padding:30px;
                border-radius:16px;
                box-shadow:0 10px 30px rgba(0,0,0,.3);
            }}

            input {{
                width:100%;
                box-sizing:border-box;
                padding:12px;
                margin:8px 0;
                border:0;
                border-radius:8px;
            }}

            button {{
                width:100%;
                padding:12px;
                margin-top:10px;
                border:0;
                border-radius:8px;
                background:#2563eb;
                color:white;
                font-weight:bold;
                cursor:pointer;
            }}

            .error {{
                color:#f87171;
                margin-top:15px;
            }}

        </style>

    </head>

    <body>

        <div class="login">

            <h1>O&O TRANS</h1>

            <p>Вхід у систему</p>

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

    return redirect(url_for("login"))


# ---------------------------------------------------------
# NAVIREC
# ---------------------------------------------------------

def navirec_headers():

    return {
        "Authorization": f"Token {NAVIREC_TOKEN}",
        "Accept": "application/json; version=1.52.1",
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

        return None, "Navirec не відповів протягом 5 секунд"

    except requests.exceptions.RequestException as e:

        return None, f"Помилка Navirec: {e}"

    except Exception as e:

        return None, f"Невідома помилка: {e}"


# ---------------------------------------------------------
# HELPERS
# ---------------------------------------------------------

def find_vehicle_state(states, vehicle_id):

    if states is None:
        return None

    if isinstance(states, dict):

        for key in [
            "results",
            "data",
            "items",
            "vehicles",
            "states",
        ]:

            value = states.get(key)

            if isinstance(value, list):

                for item in value:

                    if not isinstance(item, dict):
                        continue

                    for id_key in [
                        "vehicle",
                        "vehicle_id",
                        "vehicleId",
                    ]:

                        if item.get(id_key) == vehicle_id:

                            return item

    if isinstance(states, list):

        for item in states:

            if not isinstance(item, dict):
                continue

            for id_key in [
                "vehicle",
                "vehicle_id",
                "vehicleId",
            ]:

                if item.get(id_key) == vehicle_id:

                    return item

    return None


def extract_value(data, keys):

    if not isinstance(data, dict):
        return None

    for key in keys:

        if key in data and data[key] is not None:

            return data[key]

    return None


def vehicle_status(state):

    value = extract_value(
        state,
        [
            "state",
            "status",
            "vehicle_state",
            "movement_state",
        ],
    )

    if value is None:
        return "Немає даних"

    return str(value)


def vehicle_position(state):

    if not isinstance(state, dict):
        return None, None

    latitude = extract_value(
        state,
        [
            "latitude",
            "lat",
            "gps_latitude",
        ],
    )

    longitude = extract_value(
        state,
        [
            "longitude",
            "lon",
            "lng",
            "gps_longitude",
        ],
    )

    try:

        if latitude is not None:
            latitude = float(latitude)

        if longitude is not None:
            longitude = float(longitude)

    except Exception:

        return None, None

    return latitude, longitude


def vehicle_speed(state):

    value = extract_value(
        state,
        [
            "speed",
            "gps_speed",
            "vehicle_speed",
        ],
    )

    if value is None:
        return "—"

    return str(value)


# ---------------------------------------------------------
# HOME
# ---------------------------------------------------------

@app.route("/")
def home():

    if not logged_in():
        return redirect(url_for("login"))

    return """
    <!doctype html>

    <html lang="uk">

    <head>

        <meta charset="utf-8">

        <title>O&O TRANS</title>

        <style>

            body {
                margin:0;
                font-family:Arial;
                background:#f3f4f6;
                color:#111827;
            }

            header {
                background:#111827;
                color:white;
                padding:22px 30px;
            }

            .container {
                padding:30px;
                max-width:1200px;
                margin:auto;
            }

            .grid {
                display:grid;
                grid-template-columns:
                    repeat(auto-fit,minmax(220px,1fr));
                gap:18px;
            }

            .card {
                background:white;
                padding:25px;
                border-radius:14px;
                box-shadow:0 3px 12px rgba(0,0,0,.08);
            }

            .card a {
                color:#111827;
                text-decoration:none;
                font-weight:bold;
            }

            .card:hover {
                background:#eef2ff;
            }

        </style>

    </head>

    <body>

        <header>

            <h1>O&O TRANS</h1>

            <div>
                Внутрішня система транспортної компанії
            </div>

        </header>

        <div class="container">

            <div class="grid">

                <div class="card">
                    🚚
                    <h2>
                        <a href="/vehicles">
                            Машини
                        </a>
                    </h2>
                    <p>Стан автомобілів</p>
                </div>

                <div class="card">
                    📍
                    <h2>
                        <a href="/gps">
                            GPS
                        </a>
                    </h2>
                    <p>Карта та позиції</p>
                </div>

                <div class="card">
                    ⛽
                    <h2>
                        <a href="/fuel">
                            Паливо
                        </a>
                    </h2>
                    <p>Контроль пального</p>
                </div>

                <div class="card">
                    ⏱️
                    <h2>
                        <a href="/tachograph-test">
                            Тахограф
                        </a>
                    </h2>
                    <p>Тест Navirec stream</p>
                </div>

                <div class="card">
                    ❤️
                    <h2>
                        <a href="/health">
                            Health
                        </a>
                    </h2>
                    <p>Стан сервера</p>
                </div>

                <div class="card">
                    🚪
                    <h2>
                        <a href="/logout">
                            Вийти
                        </a>
                    </h2>
                </div>

            </div>

        </div>

    </body>

    </html>
    """


# ---------------------------------------------------------
# VEHICLES
# ---------------------------------------------------------

@app.route("/vehicles")
def vehicles():

    if not logged_in():
        return redirect(url_for("login"))

    states, error = get_vehicle_states()

    cards = ""

    for vehicle_id, name in VEHICLES.items():

        state = find_vehicle_state(
            states,
            vehicle_id
        )

        status = vehicle_status(state)

        speed = vehicle_speed(state)

        latitude, longitude = vehicle_position(state)

        if latitude is not None and longitude is not None:

            position = (
                f"{latitude}, {longitude}"
            )

        else:

            position = "Координати не отримані"

        cards += f"""

        <div class="vehicle">

            <h2>🚚 {name}</h2>

            <p>
                <b>Статус:</b>
                {status}
            </p>

            <p>
                <b>Швидкість:</b>
                {speed}
            </p>

            <p>
                <b>GPS:</b>
                {position}
            </p>

            <p>
                <a href="/vehicle/{vehicle_id}">
                    Деталі →
                </a>
            </p>

        </div>

        """

    if error:

        message = f"""

        <div class="warning">

            ⚠️ <b>Navirec зараз не відповідає.</b>

            <br><br>

            {error}

            <br><br>

            Сам сайт працює нормально.
            Спробуйте оновити сторінку пізніше.

        </div>

        """

    else:

        message = """

        <div class="success">

            ✅ З'єднання з Navirec працює.

        </div>

        """

    return f"""

    <!doctype html>

    <html lang="uk">

    <head>

        <meta charset="utf-8">

        <title>O&O TRANS — Машини</title>

        <style>

            body {{
                margin:0;
                font-family:Arial;
                background:#f3f4f6;
            }}

            .container {{
                max-width:1200px;
                margin:auto;
                padding:30px;
            }}

            .warning {{
                background:#fff7ed;
                border:1px solid #fdba74;
                padding:20px;
                border-radius:12px;
                margin-bottom:20px;
            }}

            .success {{
                background:#ecfdf5;
                border:1px solid #6ee7b7;
                padding:20px;
                border-radius:12px;
                margin-bottom:20px;
            }}

            .grid {{
                display:grid;
                grid-template-columns:
                    repeat(auto-fit,minmax(280px,1fr));
                gap:18px;
            }}

            .vehicle {{
                background:white;
                padding:22px;
                border-radius:14px;
                box-shadow:0 3px 12px rgba(0,0,0,.08);
            }}

            a {{
                color:#2563eb;
                text-decoration:none;
                font-weight:bold;
            }}

            pre {{
                background:#111827;
                color:#e5e7eb;
                padding:20px;
                border-radius:12px;
                overflow:auto;
            }}

        </style>

    </head>

    <body>

        <div class="container">

            <h1>🚚 Машини</h1>

            {message}

            <div class="grid">

                {cards}

            </div>

            <br>

            <details>

                <summary>
                    Технічні дані Navirec
                </summary>

                <pre>
{json.dumps(states, indent=2, ensure_ascii=False)}
                </pre>

            </details>

            <br>

            <a href="/">← Головна</a>

        </div>

    </body>

    </html>

    """


# ---------------------------------------------------------
# VEHICLE DETAILS
# ---------------------------------------------------------

@app.route("/vehicle/<vehicle_id>")
def vehicle(vehicle_id):

    if not logged_in():
        return redirect(url_for("login"))

    name = VEHICLES.get(
        vehicle_id,
        "Невідомий автомобіль"
    )

    states, error = get_vehicle_states()

    state = find_vehicle_state(
        states,
        vehicle_id
    )

    latitude, longitude = vehicle_position(state)

    return f"""

    <!doctype html>

    <html lang="uk">

    <head>

        <meta charset="utf-8">

        <title>{name}</title>

        <style>

            body {{
                font-family:Arial;
                background:#f3f4f6;
                padding:30px;
            }}

            .box {{
                background:white;
                padding:25px;
                border-radius:14px;
                max-width:900px;
                margin:auto;
            }}

            pre {{
                background:#111827;
                color:white;
                padding:20px;
                border-radius:10px;
                overflow:auto;
            }}

        </style>

    </head>

    <body>

        <div class="box">

            <h1>🚚 {name}</h1>

            <p>
                <b>Vehicle ID:</b>
                {vehicle_id}
            </p>

            <p>
                <b>Статус:</b>
                {vehicle_status(state)}
            </p>

            <p>
                <b>Швидкість:</b>
                {vehicle_speed(state)}
            </p>

            <p>
                <b>Latitude:</b>
                {latitude if latitude is not None else "—"}
            </p>

            <p>
                <b>Longitude:</b>
                {longitude if longitude is not None else "—"}
            </p>

            <h2>Дані Navirec</h2>

            <pre>
{json.dumps(state, indent=2, ensure_ascii=False)}
            </pre>

            <a href="/vehicles">
                ← До машин
            </a>

        </div>

    </body>

    </html>

    """


# ---------------------------------------------------------
# GPS
# ---------------------------------------------------------

@app.route("/gps")
def gps():

    if not logged_in():
        return redirect(url_for("login"))

    states, error = get_vehicle_states()

    markers = []

    for vehicle_id, name in VEHICLES.items():

        state = find_vehicle_state(
            states,
            vehicle_id
        )

        latitude, longitude = vehicle_position(state)

        if latitude is not None and longitude is not None:

            markers.append({
                "name": name,
                "lat": latitude,
                "lon": longitude,
            })

    markers_json = json.dumps(
        markers,
        ensure_ascii=False
    )

    error_text = ""

    if error:

        error_text = f"""

        <div class="warning">

            ⚠️ {error}

            <br><br>

            Карта залишилась доступною,
            але нові координати зараз не отримані.

        </div>

        """

    return f"""

    <!doctype html>

    <html lang="uk">

    <head>

        <meta charset="utf-8">

        <title>O&O TRANS — GPS</title>

        <meta
            name="viewport"
            content="width=device-width, initial-scale=1"
        >

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
                padding:20px;
            }}

            #map {{
                height:650px;
                width:100%;
            }}

            .warning {{
                background:#fff7ed;
                border:1px solid #fdba74;
                padding:15px;
                margin:15px;
                border-radius:10px;
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

            <h1>📍 O&O TRANS — GPS</h1>

            {error_text}

            <p>
                Отримано автомобілів на карті:
                <b>{len(markers)}</b>
            </p>

            <a href="/">← Головна</a>

        </div>

        <div id="map"></div>

        <script>

            const vehicles = {markers_json};

            const map = L.map("map").setView(
                [52.0, 19.0],
                6
            );

            L.tileLayer(
                "https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png",
                {{
                    maxZoom: 19,
                    attribution: "&copy; OpenStreetMap"
                }}
            ).addTo(map);

            if (vehicles.length > 0) {{

                const bounds = [];

                vehicles.forEach(function(vehicle) {{

                    const marker = L.marker([
                        vehicle.lat,
                        vehicle.lon
                    ]).addTo(map);

                    marker.bindPopup(
                        "<b>" +
                        vehicle.name +
                        "</b><br>" +
                        vehicle.lat +
                        "<br>" +
                        vehicle.lon
                    );

                    bounds.push([
                        vehicle.lat,
                        vehicle.lon
                    ]);

                }});

                map.fitBounds(bounds, {{
                    padding:[40,40]
                }});

            }}

        </script>

    </body>

    </html>

    """


# ---------------------------------------------------------
# FUEL
# ---------------------------------------------------------

@app.route("/fuel")
def fuel():

    if not logged_in():
        return redirect(url_for("login"))

    return """

    <!doctype html>

    <html lang="uk">

    <head>
        <meta charset="utf-8">
        <title>O&O TRANS — Паливо</title>
    </head>

    <body style="font-family:Arial;padding:30px">

        <h1>⛽ Паливо</h1>

        <p>
            Модуль палива буде підключений
            після перевірки Navirec API.
        </p>

        <a href="/">
            ← Головна
        </a>

    </body>

    </html>

    """


# ---------------------------------------------------------
# TACHOGRAPH TEST
# ---------------------------------------------------------

@app.route("/tachograph-test")
def tachograph_test():

    if not logged_in():
        return redirect(url_for("login"))

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

            timeout=(3, 5),
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

            <title>Tachograph Test</title>

        </head>

        <body style="font-family:Arial;padding:30px">

            <h1>⏱ Tachograph Stream Test</h1>

            <p>
                <b>HTTP:</b>
                {response.status_code}
            </p>

            <p>
                <b>Content-Type:</b>
                {response.headers.get("Content-Type")}
            </p>

            <p>
                <b>Account:</b>
                {ACCOUNT_ID}
            </p>

            <h2>Відповідь</h2>

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

            <title>Tachograph Error</title>

        </head>

        <body style="font-family:Arial;padding:30px">

            <h1>❌ Navirec stream</h1>

            <pre>{e}</pre>

            <a href="/">
                ← Головна
            </a>

        </body>

        </html>

        """


# ---------------------------------------------------------
# HEALTH
# ---------------------------------------------------------

@app.route("/health")
def health():

    return {

        "status": "ok",

        "service": "O&O TRANS bot"

    }


# ---------------------------------------------------------
# START
# ---------------------------------------------------------

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
