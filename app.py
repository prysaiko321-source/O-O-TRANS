import os
import json
from flask import Flask, redirect, url_for, session, request
import requests

app = Flask(__name__)

app.secret_key = os.environ.get(
    "SESSION_SECRET",
    "change-this-secret"
)

NAVIREC_TOKEN = os.environ.get("NAVIREC_TOKEN", "")
ADMIN_USER = os.environ.get("ADMIN_USER", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

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


def get_states():

    if not NAVIREC_TOKEN:
        return []

    try:

        response = requests.get(
            f"{NAVIREC_API}/last_vehicle_states/",
            headers=get_headers(),
            params={"account": ACCOUNT_ID},
            timeout=20,
        )

        if response.status_code != 200:
            return []

        data = response.json()

        if isinstance(data, dict):
            return data.get("results", [])

        return data

    except Exception:
        return []


def vehicle_status(vehicle_id, states):

    for state in states:

        if str(state.get("vehicle")) == str(vehicle_id):
            return state

    return None


def format_distance(value):

    if value is None:
        return "—"

    try:
        return f"{float(value):,.0f} km".replace(",", " ")

    except Exception:
        return str(value)


def format_fuel(value):

    if value is None:
        return "—"

    try:
        return f"{float(value):.0f}%"

    except Exception:
        return str(value)


CSS = """
<style>

body {
    margin: 0;
    font-family: Arial, sans-serif;
    background: #f3f5f7;
    color: #17202a;
}

header {
    background: #111827;
    color: white;
    padding: 18px 28px;
}

header h1 {
    margin: 0;
    font-size: 24px;
}

nav {
    background: #1f2937;
    padding: 12px 28px;
}

nav a {
    color: white;
    text-decoration: none;
    margin-right: 20px;
    font-weight: bold;
}

.container {
    max-width: 1400px;
    margin: 25px auto;
    padding: 0 20px;
}

.cards {
    display: grid;
    grid-template-columns:
        repeat(auto-fit, minmax(280px, 1fr));
    gap: 18px;
}

.card {
    background: white;
    border-radius: 14px;
    padding: 22px;
    box-shadow:
        0 3px 12px rgba(0,0,0,0.08);
}

.card h2 {
    margin-top: 0;
}

.info {
    background: white;
    border-radius: 14px;
    padding: 20px;
    margin-bottom: 20px;
    box-shadow:
        0 3px 12px rgba(0,0,0,0.08);
}

.vehicle-link {
    display: block;
    text-decoration: none;
    color: inherit;
}

.map {
    width: 100%;
    height: 600px;
    border-radius: 14px;
    overflow: hidden;
    margin-top: 20px;
}

.test-json {
    background: #111827;
    color: #e5e7eb;
    padding: 20px;
    border-radius: 10px;
    overflow-x: auto;
    white-space: pre-wrap;
    word-break: break-word;
}

input {
    padding: 10px;
    width: 280px;
    margin-bottom: 10px;
}

button {
    padding: 10px 18px;
    cursor: pointer;
}

</style>
"""


def page(title, content):

    nav = """
    <nav>

        <a href="/">
            🏠 Dashboard
        </a>

        <a href="/vehicles">
            🚚 Samochody
        </a>

        <a href="/gps">
            🗺️ GPS
        </a>

        <a href="/fuel">
            ⛽ Paliwo
        </a>

        <a href="/tachograph-test">
            ⏱️ Tachograf
        </a>

        <a href="/tachograph-options">
            🔎 OPTIONS
        </a>

        <a href="/health">
            ❤️ Health
        </a>

        <a href="/logout">
            Wyloguj
        </a>

    </nav>
    """

    return f"""
    <!DOCTYPE html>

    <html lang="pl">

    <head>

        <meta charset="UTF-8">

        <meta
            name="viewport"
            content="width=device-width, initial-scale=1.0"
        >

        <title>
            {title} — O&O TRANS
        </title>

        {CSS}

    </head>

    <body>

        <header>

            <h1>
                O&O TRANS
            </h1>

        </header>

        {nav}

        <div class="container">

            {content}

        </div>

    </body>

    </html>
    """


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

            return redirect(
                url_for("home")
            )

        return page(
            "Logowanie",
            """
            <h1>
                Logowanie
            </h1>

            <div class="info">
                Nieprawidłowy login lub hasło.
            </div>

            <form method="post">

                <input
                    type="text"
                    name="username"
                    placeholder="Login"
                >

                <br>

                <input
                    type="password"
                    name="password"
                    placeholder="Hasło"
                >

                <br>

                <button type="submit">
                    Zaloguj
                </button>

            </form>
            """
        )

    return page(
        "Logowanie",
        """
        <h1>
            🔐 O&O TRANS
        </h1>

        <div class="info">

            <form method="post">

                <input
                    type="text"
                    name="username"
                    placeholder="Login"
                >

                <br>

                <input
                    type="password"
                    name="password"
                    placeholder="Hasło"
                >

                <br>

                <button type="submit">
                    Zaloguj
                </button>

            </form>

        </div>
        """
    )


@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("login")
    )


@app.route("/")
def home():

    if not logged_in():
        return redirect(
            url_for("login")
        )

    states = get_states()

    cards = ""

    for vehicle_id, vehicle_name in VEHICLES.items():

        state = vehicle_status(
            vehicle_id,
            states
        )

        if state:

            speed = state.get("speed")

            if speed is not None:
                speed_text = f"{speed} km/h"
            else:
                speed_text = "—"

            fuel = format_fuel(
                state.get("fuel_level")
            )

            distance = format_distance(
                state.get("total_distance")
            )

            time_value = state.get(
                "time",
                "—"
            )

        else:

            speed_text = "—"
            fuel = "—"
            distance = "—"
            time_value = "Brak danych"

        cards += f"""

        <a
            class="vehicle-link"
            href="/vehicle/{vehicle_id}"
        >

            <div class="card">

                <h2>
                    🚚 {vehicle_name}
                </h2>

                <p>
                    <b>Prędkość:</b>
                    {speed_text}
                </p>

                <p>
                    <b>Paliwo:</b>
                    {fuel}
                </p>

                <p>
                    <b>Przebieg:</b>
                    {distance}
                </p>

                <p>
                    <b>Ostatni sygnał:</b>
                    {time_value}
                </p>

            </div>

        </a>

        """

    return page(
        "Dashboard",
        f"""

        <h1>
            🏠 Dashboard
        </h1>

        <div class="cards">

            {cards}

        </div>

        """
    )


@app.route("/vehicles")
def vehicles():

    if not logged_in():
        return redirect(
            url_for("login")
        )

    states = get_states()

    cards = ""

    for vehicle_id, vehicle_name in VEHICLES.items():

        state = vehicle_status(
            vehicle_id,
            states
        )

        if state:

            speed = state.get(
                "speed"
            )

            if speed is not None:
                speed_text = f"{speed} km/h"
            else:
                speed_text = "—"

            fuel = format_fuel(
                state.get("fuel_level")
            )

            distance = format_distance(
                state.get("total_distance")
            )

        else:

            speed_text = "—"
            fuel = "—"
            distance = "—"

        cards += f"""

        <a
            class="vehicle-link"
            href="/vehicle/{vehicle_id}"
        >

            <div class="card">

                <h2>
                    🚚 {vehicle_name}
                </h2>

                <p>
                    Prędkość: {speed_text}
                </p>

                <p>
                    Paliwo: {fuel}
                </p>

                <p>
                    Przebieg: {distance}
                </p>

            </div>

        </a>

        """

    return page(
        "Samochody",
        f"""

        <h1>
            🚚 Samochody
        </h1>

        <div class="cards">

            {cards}

        </div>

        """
    )


@app.route("/vehicle/<vehicle_id>")
def vehicle(vehicle_id):

    if not logged_in():
        return redirect(
            url_for("login")
        )

    vehicle_name = VEHICLES.get(
        vehicle_id,
        "Nieznany samochód"
    )

    states = get_states()

    state = vehicle_status(
        vehicle_id,
        states
    )

    latitude = None
    longitude = None

    speed = "—"
    fuel = "—"
    distance = "—"
    signal_time = "—"

    if state:

        location = state.get(
            "location"
        )

        if isinstance(
            location,
            dict
        ):

            coordinates = location.get(
                "coordinates"
            )

            if (
                isinstance(
                    coordinates,
                    list
                )
                and len(coordinates) >= 2
            ):

                longitude = coordinates[0]
                latitude = coordinates[1]

        if state.get("speed") is not None:

            speed = (
                f"{state.get('speed')} km/h"
            )

        fuel = format_fuel(
            state.get("fuel_level")
        )

        distance = format_distance(
            state.get("total_distance")
        )

        signal_time = state.get(
            "time",
            "—"
        )

    if (
        latitude is not None
        and longitude is not None
    ):

        map_html = f"""

        <div
            id="map"
            class="map"
        ></div>

        <link
            rel="stylesheet"
            href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
        >

        <script
            src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
        ></script>

        <script>

        const map = L.map('map')
            .setView(
                [{latitude}, {longitude}],
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

        L.marker(
            [{latitude}, {longitude}]
        )
        .addTo(map)
        .bindPopup(
            "{vehicle_name}"
        )
        .openPopup();

        </script>

        """

    else:

        map_html = """

        <div class="info">

            Brak aktualnej lokalizacji GPS.

        </div>

        """

    return page(
        vehicle_name,
        f"""

        <h1>
            🚚 {vehicle_name}
        </h1>

        <div class="cards">

            <div class="card">

                <h2>
                    Prędkość
                </h2>

                <p>
                    {speed}
                </p>

            </div>

            <div class="card">

                <h2>
                    Paliwo
                </h2>

                <p>
                    {fuel}
                </p>

            </div>

            <div class="card">

                <h2>
                    Przebieg
                </h2>

                <p>
                    {distance}
                </p>

            </div>

            <div class="card">

                <h2>
                    Ostatni sygnał
                </h2>

                <p>
                    {signal_time}
                </p>

            </div>

        </div>

        {map_html}

        <script>

        setTimeout(
            function() {{
                location.reload();
            }},
            30000
        );

        </script>

        """
    )


@app.route("/fuel")
def fuel():

    if not logged_in():
        return redirect(
            url_for("login")
        )

    states = get_states()

    rows = ""

    for vehicle_id, vehicle_name in VEHICLES.items():

        state = vehicle_status(
            vehicle_id,
            states
        )

        if state:

            fuel_level = format_fuel(
                state.get("fuel_level")
            )

            distance = format_distance(
                state.get("total_distance")
            )

            signal_time = state.get(
                "time",
                "—"
            )

        else:

            fuel_level = "—"
            distance = "—"
            signal_time = "—"

        rows += f"""

        <div class="card">

            <h2>
                🚚 {vehicle_name}
            </h2>

            <p>
                <b>Paliwo:</b>
                {fuel_level}
            </p>

            <p>
                <b>Przebieg:</b>
                {distance}
            </p>

            <p>
                <b>Ostatni sygnał:</b>
                {signal_time}
            </p>

        </div>

        """

    return page(
        "Paliwo",
        f"""

        <h1>
            ⛽ Paliwo
        </h1>

        <div class="cards">

            {rows}

        </div>

        """
    )


@app.route("/gps")
def gps():

    if not logged_in():
        return redirect(
            url_for("login")
        )

    states = get_states()

    markers = ""

    for vehicle_id, vehicle_name in VEHICLES.items():

        state = vehicle_status(
            vehicle_id,
            states
        )

        if not state:
            continue

        location = state.get(
            "location"
        )

        if not isinstance(
            location,
            dict
        ):
            continue

        coordinates = location.get(
            "coordinates"
        )

        if (
            not isinstance(
                coordinates,
                list
            )
            or len(coordinates) < 2
        ):
            continue

        longitude = coordinates[0]
        latitude = coordinates[1]

        markers += f"""

        L.marker(
            [{latitude}, {longitude}]
        )
        .addTo(map)
        .bindPopup(
            "{vehicle_name}"
        );

        """

    return page(
        "GPS",
        f"""

        <h1>
            🗺️ GPS — wszystkie samochody
        </h1>

        <div
            id="map"
            class="map"
        ></div>

        <link
            rel="stylesheet"
            href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
        >

        <script
            src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
        ></script>

        <script>

        const map = L.map('map')
            .setView(
                [51.1, 17.0],
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

        {markers}

        </script>

        """
    )


@app.route("/tachograph-test")
def tachograph_test():

    if not logged_in():
        return redirect(
            url_for("login")
        )

    if not NAVIREC_TOKEN:

        return page(
            "Tachograph Test",
            """
            <h1>
                ⏱️ Test tachografu
            </h1>

            <div class="info">

                Brak NAVIREC_TOKEN
                w ustawieniach Render.

            </div>
            """
        )

    try:

        # TEST:
        # Accept = application/x-ndjson
        # Content-Type = application/x-ndjson
        # account = USUNIĘTY

        stream_headers = {
            "Authorization":
                f"Token {NAVIREC_TOKEN}",

            "Accept":
                "application/x-ndjson",

            "Content-Type":
                "application/x-ndjson",
        }

        response = requests.get(
            f"{NAVIREC_API}/streams/driver_states/",
            headers=stream_headers,
            stream=True,
            timeout=(10, 20),
        )

        content_type = response.headers.get(
            "Content-Type",
            ""
        )

        lines = []

        if response.status_code == 200:

            for line in response.iter_lines(
                decode_unicode=True
            ):

                if line:
                    lines.append(line)

                if len(lines) >= 10:
                    break

        else:

            error_text = response.text

            if error_text:
                lines.append(error_text)

        response.close()

        parsed = []

        for line in lines:

            try:

                parsed.append(
                    json.loads(line)
                )

            except Exception:

                parsed.append(line)

        if parsed:

            formatted = json.dumps(
                parsed,
                indent=2,
                ensure_ascii=False
            )

        else:

            formatted = "Brak danych."

        content = f"""

        <h1>
            ⏱️ Test tachografu
        </h1>

        <div class="info">

            <h2>
                Odpowiedź Navirec
            </h2>

            <p>
                HTTP status:
                <b>{response.status_code}</b>
            </p>

            <p>
                Content-Type:
                <b>{content_type}</b>
            </p>

            <p>
                Test:
                <b>Accept + Content-Type =
                application/x-ndjson</b>
            </p>

            <p>
                Parametr account:
                <b>nie został wysłany</b>
            </p>

        </div>

        <div class="info">

            <h2>
                driver_states
            </h2>

            <pre class="test-json">
{formatted}
            </pre>

        </div>

        """

        return page(
            "Tachograph Test",
            content
        )

    except Exception as error:

        return page(
            "Tachograph Test",
            f"""

            <h1>
                ⏱️ Test tachografu
            </h1>

            <div class="info">

                <h2>
                    Błąd
                </h2>

                <pre class="test-json">
{str(error)}
                </pre>

            </div>

            """
        )


@app.route("/tachograph-options")
def tachograph_options():

    if not logged_in():
        return redirect(
            url_for("login")
        )

    if not NAVIREC_TOKEN:

        return page(
            "Tachograph OPTIONS",
            """
            <h1>
                🔎 OPTIONS — driver_states
            </h1>

            <div class="info">

                Brak NAVIREC_TOKEN
                w ustawieniach Render.

            </div>
            """
        )

    try:

        response = requests.options(
            f"{NAVIREC_API}/streams/driver_states/",
            headers={
                "Authorization":
                    f"Token {NAVIREC_TOKEN}",

                "Accept":
                    "application/x-ndjson",

                "Content-Type":
                    "application/x-ndjson",
            },
            timeout=20,
        )

        response_headers = {}

        for key, value in response.headers.items():

            response_headers[key] = value

        try:

            body_json = response.json()

            body = json.dumps(
                body_json,
                indent=2,
                ensure_ascii=False
            )

        except Exception:

            body = response.text

        headers_text = json.dumps(
            response_headers,
            indent=2,
            ensure_ascii=False
        )

        content = f"""

        <h1>
            🔎 OPTIONS — driver_states
        </h1>

        <div class="info">

            <h2>
                Status
            </h2>

            <p>
                HTTP status:
                <b>{response.status_code}</b>
            </p>

        </div>

        <div class="info">

            <h2>
                Response Headers
            </h2>

            <pre class="test-json">
{headers_text}
            </pre>

        </div>

        <div class="info">

            <h2>
                Response Body
            </h2>

            <pre class="test-json">
{body}
            </pre>

        </div>

        """

        return page(
            "Tachograph OPTIONS",
            content
        )

    except Exception as error:

        return page(
            "Tachograph OPTIONS",
            f"""

            <h1>
                🔎 OPTIONS — driver_states
            </h1>

            <div class="info">

                <h2>
                    Błąd
                </h2>

                <pre class="test-json">
{str(error)}
                </pre>

            </div>

            """
        )


@app.route("/health")
def health():

    return "O&O TRANS bot працює!"


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=10000
    )
