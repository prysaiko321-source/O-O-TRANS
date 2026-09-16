import os
from functools import wraps
from flask import Flask, request, redirect, url_for, session, render_template_string
import requests

app = Flask(__name__)

# =========================
# O&O TRANS SETTINGS
# =========================

app.secret_key = os.getenv("SESSION_SECRET", "oo-trans-session-change-me")

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

NAVIREC_TOKEN = os.getenv("NAVIREC_TOKEN", "")
NAVIREC_API = "https://api.navirec.com"

ACCOUNT_ID = "5c980074-7a71-4c9b-b5a8-a7c45163adf5"

VEHICLES = {
    "aaaa9acd-5bb5-467e-8241-81444292bbfe": "Renault Master SH 9203G",
    "cbb121b6-34dd-41c6-974b-5b7aa3d9a1cb": "Renault Master DX 9034F",
    "f016af91-dee6-4e72-9f86-4b2e27a253c1": "Renault Master DX 5405A",
}


# =========================
# AUTH
# =========================

def login_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return function(*args, **kwargs)

    return wrapper


# =========================
# NAVIREC
# =========================

def navirec_states():
    if not NAVIREC_TOKEN:
        return []

    url = f"{NAVIREC_API}/last_vehicle_states/"

    headers = {
        "Authorization": f"Token {NAVIREC_TOKEN}",
        "Accept": "application/json; version=1.52.1",
    }

    params = {
        "account": ACCOUNT_ID
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=20
        )

        if response.status_code != 200:
            return []

        data = response.json()

        if isinstance(data, dict):
            return data.get("results", [])

        return data

    except Exception:
        return []


def vehicle_status(vehicle):
    speed = vehicle.get("speed") or 0
    time_value = vehicle.get("time")

    if not time_value:
        return "🔴 Brak sygnału", "red"

    try:
        from datetime import datetime, timezone

        last_time = datetime.fromisoformat(
            time_value.replace("Z", "+00:00")
        )

        now = datetime.now(timezone.utc)
        minutes = (now - last_time).total_seconds() / 60

        if minutes > 30:
            return "🔴 Brak połączenia", "red"

    except Exception:
        pass

    if speed > 3:
        return "🟢 W trasie", "green"

    return "🟡 Postój", "yellow"


# =========================
# LOGIN PAGE
# =========================

LOGIN_HTML = """
<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>O&O TRANS — Login</title>

<style>
body {
    margin:0;
    background:#0b1220;
    color:white;
    font-family:Arial,sans-serif;
    display:flex;
    align-items:center;
    justify-content:center;
    min-height:100vh;
}

.login {
    width:360px;
    background:#111b2e;
    padding:35px;
    border-radius:18px;
    box-shadow:0 20px 60px rgba(0,0,0,.4);
}

.logo {
    font-size:32px;
    font-weight:800;
    text-align:center;
    margin-bottom:8px;
}

.subtitle {
    text-align:center;
    color:#94a3b8;
    margin-bottom:30px;
}

input {
    width:100%;
    box-sizing:border-box;
    padding:13px;
    margin-bottom:15px;
    border-radius:9px;
    border:1px solid #334155;
    background:#0f172a;
    color:white;
    font-size:16px;
}

button {
    width:100%;
    padding:13px;
    border:0;
    border-radius:9px;
    background:#2563eb;
    color:white;
    font-size:16px;
    cursor:pointer;
}

.error {
    color:#f87171;
    text-align:center;
    margin-bottom:15px;
}
</style>
</head>

<body>

<div class="login">

<div class="logo">O&O TRANS</div>
<div class="subtitle">System zarządzania firmą</div>

{% if error %}
<div class="error">{{ error }}</div>
{% endif %}

<form method="post">

<input
    name="username"
    placeholder="Login"
    autocomplete="username"
    required
>

<input
    name="password"
    type="password"
    placeholder="Hasło"
    autocomplete="current-password"
    required
>

<button type="submit">
    Zaloguj się
</button>

</form>

</div>

</body>
</html>
"""


@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if (
            username == ADMIN_USER
            and ADMIN_PASSWORD
            and password == ADMIN_PASSWORD
        ):
            session["logged_in"] = True
            return redirect(url_for("dashboard"))

        return render_template_string(
            LOGIN_HTML,
            error="Nieprawidłowy login lub hasło."
        )

    return render_template_string(
        LOGIN_HTML,
        error=None
    )


@app.route("/logout")
def logout():

    session.clear()

    return redirect(url_for("login"))


# =========================
# MAIN DASHBOARD
# =========================

DASHBOARD_HTML = """
<!doctype html>
<html lang="uk">

<head>

<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">

<title>O&O TRANS</title>

<style>

* {
    box-sizing:border-box;
}

body {
    margin:0;
    background:#f1f5f9;
    font-family:Arial,sans-serif;
    color:#0f172a;
}

.topbar {
    height:72px;
    background:#0b1220;
    color:white;
    display:flex;
    align-items:center;
    padding:0 25px;
    justify-content:space-between;
}

.logo {
    font-size:27px;
    font-weight:800;
}

.logo span {
    color:#3b82f6;
}

.logout {
    color:#cbd5e1;
    text-decoration:none;
}

.layout {
    display:flex;
    min-height:calc(100vh - 72px);
}

.sidebar {
    width:230px;
    background:#111827;
    color:white;
    padding:20px 12px;
}

.menu-title {
    color:#64748b;
    font-size:12px;
    text-transform:uppercase;
    margin:12px 12px;
}

.menu a {
    display:block;
    padding:12px;
    margin:4px 0;
    border-radius:9px;
    color:#cbd5e1;
    text-decoration:none;
}

.menu a:hover {
    background:#1e293b;
    color:white;
}

.content {
    flex:1;
    padding:30px;
}

h1 {
    margin-top:0;
}

.cards {
    display:grid;
    grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
    gap:18px;
    margin-bottom:30px;
}

.card {
    background:white;
    border-radius:15px;
    padding:20px;
    box-shadow:0 3px 15px rgba(15,23,42,.06);
}

.card-title {
    color:#64748b;
    font-size:14px;
}

.card-value {
    font-size:28px;
    font-weight:700;
    margin-top:8px;
}

.table-box {
    background:white;
    border-radius:15px;
    padding:20px;
    overflow:auto;
}

table {
    width:100%;
    border-collapse:collapse;
}

th, td {
    padding:13px;
    border-bottom:1px solid #e2e8f0;
    text-align:left;
}

th {
    color:#64748b;
    font-size:13px;
}

.vehicle-link {
    color:#2563eb;
    text-decoration:none;
    font-weight:700;
}

.status-green {
    color:#16a34a;
}

.status-yellow {
    color:#ca8a04;
}

.status-red {
    color:#dc2626;
}

@media(max-width:800px) {

    .sidebar {
        display:none;
    }

    .content {
        padding:18px;
    }
}

</style>

</head>

<body>

<div class="topbar">

<div class="logo">
O&O <span>TRANS</span>
</div>

<a class="logout" href="/logout">
Wyloguj
</a>

</div>

<div class="layout">

<div class="sidebar">

<div class="menu-title">
O&O TRANS
</div>

<div class="menu">

<a href="/">
🏠 Dashboard
</a>

<a href="/vehicles">
🚚 Samochody
</a>

<a href="/gps">
🗺️ Navirec
</a>

<a href="#">
📦 Trans.eu
</a>

<a href="#">
⛽ Paliwo
</a>

<a href="#">
💰 Finanse
</a>

<a href="#">
🔧 Naprawy
</a>

<a href="#">
📊 Raporty
</a>

</div>

</div>

<div class="content">

<h1>Dashboard</h1>

<div class="cards">

<div class="card">
<div class="card-title">Samochody</div>
<div class="card-value">{{ total }}</div>
</div>

<div class="card">
<div class="card-title">W trasie</div>
<div class="card-value">{{ moving }}</div>
</div>

<div class="card">
<div class="card-title">Postój</div>
<div class="card-value">{{ stopped }}</div>
</div>

<div class="card">
<div class="card-title">Brak połączenia</div>
<div class="card-value">{{ offline }}</div>
</div>

</div>


<div class="table-box">

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

{% for v in vehicles %}

<tr>

<td>
<a class="vehicle-link"
href="/vehicle/{{ v.id }}">
{{ v.name }}
</a>
</td>

<td class="status-{{ v.status_color }}">
{{ v.status }}
</td>

<td>
{{ v.speed }} km/h
</td>

<td>
{{ v.fuel }} %
</td>

<td>
{{ v.distance }} km
</td>

<td>
{{ v.time }}
</td>

</tr>

{% endfor %}

</tbody>

</table>

</div>

</div>

</div>

</body>
</html>
"""


@app.route("/")
@login_required
def dashboard():

    states = navirec_states()

    vehicles = []

    moving = 0
    stopped = 0
    offline = 0

    for state in states:

        vehicle_id = state.get("vehicle")

        if isinstance(vehicle_id, dict):
            vehicle_id = vehicle_id.get("id")

        name = VEHICLES.get(
            vehicle_id,
            str(vehicle_id or "Nieznany")
        )

        status, status_color = vehicle_status(state)

        if status_color == "green":
            moving += 1
        elif status_color == "yellow":
            stopped += 1
        else:
            offline += 1

        vehicles.append({
            "id": vehicle_id,
            "name": name,
            "status": status,
            "status_color": status_color,
            "speed": round(float(state.get("speed") or 0), 1),
            "fuel": state.get("fuel_level") if state.get("fuel_level") is not None else "—",
            "distance": round(float(state.get("total_distance") or 0), 1),
            "time": state.get("time") or "—",
        })

    return render_template_string(
        DASHBOARD_HTML,
        vehicles=vehicles,
        total=len(vehicles),
        moving=moving,
        stopped=stopped,
        offline=offline
    )


# =========================
# VEHICLES PAGE
# =========================

@app.route("/vehicles")
@login_required
def vehicles_page():
    return redirect(url_for("dashboard"))


# =========================
# VEHICLE DETAILS
# =========================

VEHICLE_HTML = """
<!doctype html>
<html lang="uk">

<head>

<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">

<title>{{ name }} — O&O TRANS</title>

<style>

body {
    margin:0;
    background:#f1f5f9;
    font-family:Arial,sans-serif;
}

.top {
    background:#0b1220;
    color:white;
    padding:20px 30px;
}

.top a {
    color:#cbd5e1;
    text-decoration:none;
}

.content {
    padding:30px;
}

.grid {
    display:grid;
    grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
    gap:18px;
}

.card {
    background:white;
    padding:22px;
    border-radius:15px;
    box-shadow:0 3px 15px rgba(0,0,0,.06);
}

.label {
    color:#64748b;
    font-size:13px;
}

.value {
    font-size:25px;
    font-weight:bold;
    margin-top:7px;
}

</style>

</head>

<body>

<div class="top">

<a href="/">← Dashboard</a>

<h1>{{ name }}</h1>

</div>

<div class="content">

<div class="grid">

<div class="card">
<div class="label">Status</div>
<div class="value">{{ status }}</div>
</div>

<div class="card">
<div class="label">Prędkość</div>
<div class="value">{{ speed }} km/h</div>
</div>

<div class="card">
<div class="label">Poziom paliwa</div>
<div class="value">{{ fuel }} %</div>
</div>

<div class="card">
<div class="label">Przebieg</div>
<div class="value">{{ distance }} km</div>
</div>

<div class="card">
<div class="label">Ostatni sygnał</div>
<div class="value">{{ time }}</div>
</div>

<div class="card">
<div class="label">GPS</div>
<div class="value">{{ gps }}</div>
</div>

</div>

</div>

</body>

</html>
"""


@app.route("/vehicle/<vehicle_id>")
@login_required
def vehicle(vehicle_id):

    states = navirec_states()

    selected = None

    for state in states:

        current_id = state.get("vehicle")

        if isinstance(current_id, dict):
            current_id = current_id.get("id")

        if current_id == vehicle_id:
            selected = state
            break

    name = VEHICLES.get(
        vehicle_id,
        "Nieznany samochód"
    )

    if not selected:

        return render_template_string(
            VEHICLE_HTML,
            name=name,
            status="🔴 Brak danych",
            speed="—",
            fuel="—",
            distance="—",
            time="—",
            gps="—"
        )

    status, _ = vehicle_status(selected)

    location = selected.get("location") or {}

    coordinates = location.get("coordinates")

    gps = "—"

    if coordinates:
        gps = f"{coordinates[1]}, {coordinates[0]}"

    return render_template_string(
        VEHICLE_HTML,
        name=name,
        status=status,
        speed=round(float(selected.get("speed") or 0), 1),
        fuel=selected.get("fuel_level") if selected.get("fuel_level") is not None else "—",
        distance=round(float(selected.get("total_distance") or 0), 1),
        time=selected.get("time") or "—",
        gps=gps
    )


# =========================
# GPS
# =========================

GPS_HTML = """
<!doctype html>

<html lang="uk">

<head>

<meta charset="utf-8">

<meta name="viewport"
content="width=device-width,initial-scale=1">

<title>Navirec — O&O TRANS</title>

<link
rel="stylesheet"
href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
/>

<style>

body {
    margin:0;
    font-family:Arial,sans-serif;
}

.top {
    height:65px;
    background:#0b1220;
    color:white;
    display:flex;
    align-items:center;
    padding:0 20px;
}

.top a {
    color:white;
    text-decoration:none;
    margin-right:25px;
}

#map {
    height:calc(100vh - 65px);
}

.truck {
    font-size:28px;
}

</style>

</head>

<body>

<div class="top">

<a href="/">← O&O TRANS</a>

<b>🗺️ Navirec</b>

</div>

<div id="map"></div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>

<script>

const map = L.map('map').setView([51.5, 10], 6);

L.tileLayer(
    'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    {
        attribution:'© OpenStreetMap'
    }
).addTo(map);

const vehicles = {{ vehicles|tojson }};

const markers = [];

vehicles.forEach(v => {

    if (!v.lat || !v.lon) {
        return;
    }

    let color = 'red';

    if (v.status_color === 'green') {
        color = 'green';
    }

    if (v.status_color === 'yellow') {
        color = 'orange';
    }

    const icon = L.divIcon({
        className:'',
        html:`<div style="
            font-size:32px;
            filter:
            ${color === 'green'
                ? 'hue-rotate(80deg) saturate(4)'
                : color === 'orange'
                ? 'hue-rotate(5deg) saturate(5)'
                : 'grayscale(1) saturate(8)'
            };
        ">🚚</div>`,
        iconSize:[40,40],
        iconAnchor:[20,20]
    });

    const marker = L.marker(
        [v.lat,v.lon],
        {icon:icon}
    ).addTo(map);

    marker.bindPopup(`
        <b>${v.name}</b><br>
        ${v.status}<br>
        Prędkość: ${v.speed} km/h<br>
        Paliwo: ${v.fuel}%<br>
        Przebieg: ${v.distance} km<br>
        Ostatni sygnał: ${v.time}<br><br>
        <a href="/vehicle/${v.id}">
        Otwórz szczegóły
        </a>
    `);

    markers.push(marker);

});

if (markers.length > 0) {

    const group = L.featureGroup(markers);

    map.fitBounds(
        group.getBounds().pad(.15)
    );
}

</script>

</body>

</html>
"""


@app.route("/gps")
@login_required
def gps():

    states = navirec_states()

    vehicles = []

    for state in states:

        vehicle_id = state.get("vehicle")

        if isinstance(vehicle_id, dict):
            vehicle_id = vehicle_id.get("id")

        name = VEHICLES.get(
            vehicle_id,
            str(vehicle_id or "Nieznany")
        )

        location = state.get("location") or {}

        coordinates = location.get("coordinates")

        if not coordinates:
            continue

        status, status_color = vehicle_status(state)

        vehicles.append({
            "id": vehicle_id,
            "name": name,
            "lat": coordinates[1],
            "lon": coordinates[0],
            "status": status,
            "status_color": status_color,
            "speed": round(float(state.get("speed") or 0), 1),
            "fuel": state.get("fuel_level") if state.get("fuel_level") is not None else "—",
            "distance": round(float(state.get("total_distance") or 0), 1),
            "time": state.get("time") or "—",
        })

    return render_template_string(
        GPS_HTML,
        vehicles=vehicles
    )


# =========================
# HEALTH CHECK
# =========================

@app.route("/health")
def health():
    return "O&O TRANS OK"


# =========================
# START
# =========================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            10000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
