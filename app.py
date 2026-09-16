import os
import requests
from flask import Flask

app = Flask(__name__)

API = "https://api.navirec.com"
TOKEN = os.getenv("NAVIREC_TOKEN")

ACCOUNT_ID = "5c980074-7a71-4c9b-b5a8-a7c45163adf5"

HEADERS = {
    "Authorization": f"Token {TOKEN}",
    "Accept": "application/json; version=1.52.1",
    "User-Agent": "O-O-TRANS/1.0"
}

VEHICLES = {
    "aaaa9acd-5bb5-467e-8241-81444292bbfe": "Renault Master SH 9203G",
    "cbb121b6-34dd-41c6-974b-5b7aa3d9a1cb": "Renault Master DX 9034F",
    "f016af91-dee6-4e72-9f86-4b2e27a253c1": "Renault Master DX 5405A"
}


@app.route("/")
def home():
    return """
    <h1>🚚 O&O TRANS</h1>
    <h2>Контроль автомобілів</h2>

    <p>
        <a href="/gps">📍 Відкрити карту автомобілів</a>
    </p>
    """


@app.route("/gps")
def gps():

    url = f"{API}/last_vehicle_states/"

    try:
        response = requests.get(
            url,
            headers=HEADERS,
            params={"account": ACCOUNT_ID},
            timeout=30
        )

        if response.status_code != 200:
            return f"""
            <h2>Помилка Navirec</h2>
            <pre>
Статус: {response.status_code}

{response.text}
            </pre>
            """

        data = response.json()

        markers = []

        for vehicle in data:

            vehicle_url = vehicle.get("vehicle", "")
            vehicle_id = vehicle_url.rstrip("/").split("/")[-1]

            name = VEHICLES.get(vehicle_id, vehicle_id)

            time = vehicle.get("time", "—")
            speed = vehicle.get("speed", "—")
            heading = vehicle.get("heading", "—")
            location = vehicle.get("location")

            if not location:
                continue

            coordinates = location.get("coordinates")

            if not coordinates:
                continue

            longitude = coordinates[0]
            latitude = coordinates[1]

            markers.append({
                "name": name,
                "lat": latitude,
                "lon": longitude,
                "time": time,
                "speed": speed,
                "heading": heading
            })

        return f"""
<!DOCTYPE html>
<html>
<head>

<meta charset="UTF-8">

<title>O&O TRANS GPS</title>

<meta name="viewport" content="width=device-width, initial-scale=1.0">

<link
rel="stylesheet"
href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
/>

<script
src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js">
</script>

<style>

body {{
    margin: 0;
    font-family: Arial, sans-serif;
}}

#header {{
    height: 60px;
    background: #222;
    color: white;
    display: flex;
    align-items: center;
    padding-left: 20px;
    font-size: 22px;
    font-weight: bold;
}}

#map {{
    width: 100%;
    height: calc(100vh - 60px);
}}

.info {{
    font-size: 14px;
    line-height: 1.5;
}}

.vehicle {{
    font-size: 16px;
    font-weight: bold;
}}

</style>

</head>

<body>

<div id="header">
🚚 O&O TRANS — GPS
</div>

<div id="map"></div>

<script>

const vehicles = {markers};

let map;

if (vehicles.length > 0) {{

    let first = vehicles[0];

    map = L.map('map').setView(
        [first.lat, first.lon],
        6
    );

}} else {{

    map = L.map('map').setView(
        [51.9, 19.1],
        6
    );

}}

L.tileLayer(
    'https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
    {{
        maxZoom: 19,
        attribution: '&copy; OpenStreetMap'
    }}
).addTo(map);


const bounds = [];


vehicles.forEach(function(vehicle) {{

    const marker = L.marker(
        [vehicle.lat, vehicle.lon]
    ).addTo(map);

    const popup = `
        <div class="info">

            <div class="vehicle">
                🚚 ${{vehicle.name}}
            </div>

            <hr>

            <b>Швидкість:</b>
            ${{vehicle.speed}} км/год
            <br>

            <b>Напрямок:</b>
            ${{vehicle.heading}}°
            <br>

            <b>Останній сигнал:</b>
            ${{vehicle.time}}
            <br>

            <b>GPS:</b>
            ${{vehicle.lat}},
            ${{vehicle.lon}}

        </div>
    `;

    marker.bindPopup(popup);

    bounds.push(
        [vehicle.lat, vehicle.lon]
    );

}});


if (bounds.length > 1) {{

    map.fitBounds(bounds, {{
        padding: [50, 50]
    }});

}}


</script>

</body>
</html>
"""

    except Exception as e:

        return f"""
        <h2>Помилка</h2>
        <pre>{repr(e)}</pre>
        """


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=10000
    )
