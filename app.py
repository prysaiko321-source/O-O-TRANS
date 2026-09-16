import os
from datetime import datetime, timezone
from flask import Flask, render_template_string, redirect, url_for, session, request
import requests

app = Flask(__name__)

app.secret_key = os.environ.get("SESSION_SECRET", "change-me")

ADMIN_USER = os.environ.get("ADMIN_USER", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
NAVIREC_TOKEN = os.environ.get("NAVIREC_TOKEN", "")

NAVIREC_API = "https://api.navirec.com"

ACCOUNT_ID = "5c980074-7a71-4c9b-b5a8-a7c45163adf5"

VEHICLES = {
    "aaaa9acd-5bb5-467e-8241-81444292bbfe": "Renault Master SH 9203G",
    "cbb121b6-34dd-41c6-974b-5b7aa3d9a1cb": "Renault Master DX 9034F",
    "f016af91-dee6-4e72-9f86-4b2e27a253c1": "Renault Master DX 5405A",
}


def logged_in():
    return session.get("logged_in") is True


def get_headers():
    return {
        "Authorization": f"Token {NAVIREC_TOKEN}",
        "Accept": "application/json; version=1.52.1",
    }


def get_vehicle_id(state):
    vehicle = state.get("vehicle")

    if isinstance(vehicle, dict):
        if vehicle.get("id"):
            return vehicle["id"]

        url = vehicle.get("url", "")
        if url:
            return url.rstrip("/").split("/")[-1]

    if isinstance(vehicle, str):
        return vehicle.rstrip("/").split("/")[-1]

    return None


def get_states():
    if not NAVIREC_TOKEN:
        return []

    try:
        url = f"{NAVIREC_API}/last_vehicle_states/"
        params = {"account": ACCOUNT_ID}

        response = requests.get(
            url,
            headers=get_headers(),
            params=params,
            timeout=20,
        )

        response.raise_for_status()
        data = response.json()

        if isinstance(data, dict):
            return data.get("results", [])

        return data

    except Exception:
        return []


def vehicle_status(state):
    speed = float(state.get("speed") or 0)

    time_value = state.get("time")

    if not time_value:
        return "🔴 Brak połączenia"

    try:
        dt = datetime.fromisoformat(
            time_value.replace("Z", "+00:00")
        )

        now = datetime.now(timezone.utc)
        minutes = (now - dt).total_seconds() / 60

        if minutes > 30:
            return "🔴 Brak połączenia"

    except Exception:
        pass

    if speed > 3:
        return "🟢 W trasie"

    return "🟡 Postój"


def format_distance(value):
    if value is None:
        return "—"

    try:
        km = float(value) / 1000
        return f"{km:,.1f}".replace(",", " ") + " km"
    except Exception:
        return "—"


def format_fuel(value):
    if value is None:
        return "—"

    try:
        return f"{float(value):.1f} %"
    except Exception:
        return "—"


BASE_STYLE = """
<style>
body {
    margin: 0;
    font-family: Arial, sans-serif;
    background: #f3f5f7;
    color: #1f2937;
}

.header {
    background: #111827;
    color: white;
    padding: 18px 30px;
    display: flex;
    justify-content: space-between;
    align-items: center;
}

.logo {
    font-size: 24px;
    font-weight: bold;
}

.logout {
    color: white;
    text-decoration: none;
    background: #374151;
    padding: 9px 15px;
    border-radius: 8px;
}

.nav {
    background: white;
    padding: 14px 25px;
    border-bottom: 1px solid #ddd;
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
}

.nav a {
    text-decoration: none;
    color: #111827;
    padding: 10px 13px;
    border-radius: 8px;
}

.nav a:hover {
    background: #e5e7eb;
}

.container {
    max-width: 1200px;
    margin: 25px auto;
    padding: 0 20px;
}

.cards {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 15px;
}

.card {
    background: white;
    border-radius: 12px;
    padding: 20px;
    box-shadow: 0 2px 8px rgba(0,0,0,.08);
}

.card-title {
    color: #6b7280;
    font-size: 14px;
}

.card-number {
    font-size: 30px;
    font-weight: bold;
    margin-top: 8px;
}

table {
    width: 100%;
    border-collapse: collapse;
    background: white;
    border-radius: 12px;
    overflow: hidden;
}

th, td {
    padding: 13px;
    border-bottom: 1px solid #e5e7eb;
    text-align: left;
}

th {
    background: #f9fafb;
}

a.vehicle-link {
    color: #111827;
    font-weight: bold;
    text-decoration: none;
}

a.vehicle-link:hover {
    text-decoration: underline;
}

.section {
    margin-top: 25px;
}

.fuel-box {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 18px;
}

.fuel-card {
    background: white;
    border-radius: 14px;
    padding: 22px;
    box-shadow: 0 2px 8px rgba(0,0,0,.08);
}

.fuel-percent {
    font-size: 36px;
    font-weight: bold;
    margin: 10px 0;
}

.progress {
    height: 14px;
    background: #e5e7eb;
    border-radius: 10px;
    overflow: hidden;
}

.progress-bar {
    height: 100%;
    background: #16a34a;
}

.info {
    background: white;
    border-radius: 12px;
    padding: 20px;
    margin-top: 20px;
}

.back {
    display: inline-block;
    margin-bottom: 20px;
    text-decoration: none;
    color: #2563eb;
}

@media(max-width: 800px) {
    .cards,
    .fuel-box {
        grid-template-columns: 1fr;
    }
}
</style>
"""


NAV = """
<div class="nav">
    <a href="/">🏠 Dashboard</a>
    <a href="/vehicles">🚚 Samochody</a>
    <a href="/gps">🗺️ Navirec</a>
    <a href="/fuel">⛽ Paliwo</a>
    <a href="#">📦 Trans.eu</a>
    <a href="#">💰 Finanse</a>
    <a href="#">🔧 Naprawy</a>
    <a href="#">📊 Raporty</a>
</div>
"""


def page(title, content):
    return f"""
<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>O&O TRANS - {title}</title>
{BASE_STYLE}
</head>
<body>

<div class="header">
    <div class="logo">O&O TRANS</div>
    <a class="logout" href="/logout">Wyloguj</a>
</div>

{NAV}

<div class="container">
{content}
</div>

</body>
</html>
"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if username == ADMIN_USER and password == ADMIN_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("dashboard"))

        return """
        <h2>Nieprawidłowy login lub hasło</h2>
        <a href="/login">Wróć</a>
        """

    return """
<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="UTF-8">
<title>O&O TRANS</title>
<style>
body {
    background:#111827;
    font-family:Arial;
    display:flex;
    justify-content:center;
    align-items:center;
    height:100vh;
}
.box {
    background:white;
    padding:35px;
    border-radius:15px;
    width:320px;
}
input {
    width:100%;
    padding:12px;
    margin:8px 0;
    box-sizing:border-box;
}
button {
    width:100%;
    padding:12px;
    background:#111827;
    color:white;
    border:0;
    border-radius:8px;
    cursor:pointer;
}
</style>
</head>
<body>
<div class="box">
<h2>O&O TRANS</h2>
<form method="post">
<input name="username" placeholder="Login">
<input name="password" type="password" placeholder="Hasło">
<button type="submit">Zaloguj</button>
</form>
</div>
</body>
</html>
"""


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def dashboard():
    if not logged_in():
        return redirect(url_for("login"))

    states = get_states()

    moving = 0
    stopped = 0
    offline = 0

    rows = ""

    for state in states:
        vehicle_id = get_vehicle_id(state)
        name = VEHICLES.get(vehicle_id, vehicle_id or "Nieznany pojazd")

        status = vehicle_status(state)

        if status.startswith("🟢"):
            moving += 1
        elif status.startswith("🟡"):
            stopped += 1
        else:
            offline += 1

        speed = float(state.get("speed") or 0)
        fuel = state.get("fuel_level")
        distance = state.get("total_distance")
        signal = state.get("time") or "—"

        rows += f"""
        <tr>
            <td>
                <a class="vehicle-link"
                   href="/vehicle/{vehicle_id}">
                   {name}
                </a>
            </td>
            <td>{status}</td>
            <td>{speed:.1f} km/h</td>
            <td>{format_fuel(fuel)}</td>
            <td>{format_distance(distance)}</td>
            <td>{signal}</td>
        </tr>
        """

    content = f"""
<h1>Dashboard</h1>

<div class="cards">
    <div class="card">
        <div class="card-title">Samochody</div>
        <div class="card-number">{len(states)}</div>
    </div>

    <div class="card">
        <div class="card-title">W trasie</div>
        <div class="card-number">{moving}</div>
    </div>

    <div class="card">
        <div class="card-title">Postój</div>
        <div class="card-number">{stopped}</div>
    </div>

    <div class="card">
        <div class="card-title">Brak połączenia</div>
        <div class="card-number">{offline}</div>
    </div>
</div>

<div class="section">
<h2>Samochody</h2>

<table>
<thead>
<tr>
<th>Samochód</th>
<th>Status</th>
<th>Prędkość</th>
<th>Paliwo</th>
<th>Przebieg</th>
<th>Ostatni sygnał</th>
</tr>
</thead>
<tbody>
{rows}
</tbody>
</table>
</div>
"""

    return page("Dashboard", content)


@app.route("/vehicles")
def vehicles():
    if not logged_in():
        return redirect(url_for("login"))

    states = get_states()

    rows = ""

    for state in states:
        vehicle_id = get_vehicle_id(state)
        name = VEHICLES.get(vehicle_id, vehicle_id or "Nieznany pojazd")

        rows += f"""
        <tr>
            <td>
                <a class="vehicle-link"
                   href="/vehicle/{vehicle_id}">
                   {name}
                </a>
            </td>
            <td>{vehicle_status(state)}</td>
            <td>{format_fuel(state.get("fuel_level"))}</td>
            <td>{format_distance(state.get("total_distance"))}</td>
        </tr>
        """

    content = f"""
<h1>🚚 Samochody</h1>

<table>
<thead>
<tr>
<th>Samochód</th>
<th>Status</th>
<th>Paliwo</th>
<th>Przebieg</th>
</tr>
</thead>
<tbody>
{rows}
</tbody>
</table>
"""

    return page("Samochody", content)


@app.route("/vehicle/<vehicle_id>")
def vehicle(vehicle_id):
    if not logged_in():
        return redirect(url_for("login"))

    states = get_states()

    state = None

    for item in states:
        if get_vehicle_id(item) == vehicle_id:
            state = item
            break

    name = VEHICLES.get(vehicle_id, "Nieznany pojazd")

    if not state:
        content = f"""
        <a class="back" href="/vehicles">← Samochody</a>
        <h1>{name}</h1>
        <div class="info">
        Brak aktualnych danych z Navirec.
        </div>
        """
        return page(name, content)

    speed = float(state.get("speed") or 0)

    location = state.get("location") or {}
    coordinates = location.get("coordinates", [])

    gps = "—"

    if len(coordinates) >= 2:
        gps = f"{coordinates[1]}, {coordinates[0]}"

    content = f"""
<a class="back" href="/vehicles">← Samochody</a>

<h1>{name}</h1>

<div class="info">
<p>Status</p>
<h2>{vehicle_status(state)}</h2>

<p>Prędkość</p>
<h2>{speed:.1f} km/h</h2>

<p>Poziom paliwa</p>
<h2>{format_fuel(state.get("fuel_level"))}</h2>

<p>Przebieg</p>
<h2>{format_distance(state.get("total_distance"))}</h2>

<p>Ostatni sygnał</p>
<h2>{state.get("time") or "—"}</h2>

<p>GPS</p>
<h2>{gps}</h2>
</div>
"""

    return page(name, content)


@app.route("/fuel")
def fuel():
    if not logged_in():
        return redirect(url_for("login"))

    states = get_states()

    cards = ""

    for state in states:
        vehicle_id = get_vehicle_id(state)
        name = VEHICLES.get(vehicle_id, vehicle_id or "Nieznany pojazd")

        fuel = state.get("fuel_level")

        try:
            fuel_value = float(fuel)
            width = max(0, min(100, fuel_value))
        except Exception:
            fuel_value = None
            width = 0

        cards += f"""
        <div class="fuel-card">
            <h2>{name}</h2>

            <div class="fuel-percent">
                {format_fuel(fuel)}
            </div>

            <div class="progress">
                <div class="progress-bar"
                     style="width:{width}%"></div>
            </div>

            <p>Przebieg: {format_distance(state.get("total_distance"))}</p>
            <p>Ostatni sygnał: {state.get("time") or "—"}</p>
        </div>
        """

    content = f"""
<h1>⛽ Paliwo</h1>

<div class="fuel-box">
{cards}
</div>

<div class="info">
<h2>Historia tankowania</h2>
<p>
Moduł jest przygotowany do dalszego podłączenia
danych o tankowaniach z Navirec.
</p>

<p>
Tutaj będziemy docelowo widzieć:
</p>

<ul>
<li>ilość zatankowanych litrów;</li>
<li>datę i godzinę tankowania;</li>
<li>miejsce tankowania;</li>
<li>poziom paliwa przed i po tankowaniu;</li>
<li>zużycie paliwa;</li>
<li>koszt tankowania;</li>
<li>historię dla każdego samochodu.</li>
</ul>
</div>
"""

    return page("Paliwo", content)


@app.route("/gps")
def gps():
    if not logged_in():
        return redirect(url_for("login"))

    states = get_states()

    markers = ""

    for state in states:
        vehicle_id = get_vehicle_id(state)
        name = VEHICLES.get(vehicle_id, vehicle_id or "Nieznany pojazd")

        location = state.get("location") or {}
        coordinates = location.get("coordinates", [])

        if len(coordinates) < 2:
            continue

        lon = coordinates[0]
        lat = coordinates[1]

        status = vehicle_status(state)

        if status.startswith("🟢"):
            color = "green"
        elif status.startswith("🟡"):
            color = "orange"
        else:
            color = "red"

        markers += f"""
        L.marker([{lat}, {lon}], {{
            icon: L.divIcon({{
                className: 'truck-icon',
                html: '<div style="font-size:30px; filter: hue-rotate(0deg);">🚚</div>',
                iconSize: [35,35]
            }})
        }})
        .addTo(map)
        .bindPopup("<b>{name}</b><br>{status}<br>{lat}, {lon}");
        """

    content = f"""
<h1>🗺️ Navirec</h1>

<div id="map" style="height:650px;border-radius:15px;"></div>

<link rel="stylesheet"
href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>

<script>
var map = L.map('map').setView([52.3, 13.4], 6);

L.tileLayer(
'https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
{{
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap'
}}
).addTo(map);

{markers}
</script>
"""

    return page("Navirec", content)


@app.route("/health")
def health():
    return "O&O TRANS OK"


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000))
    )
