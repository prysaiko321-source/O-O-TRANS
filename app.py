import os
import requests
from flask import Flask, render_template_string

app = Flask(__name__)

API = "https://api.navirec.com"
HEADERS = {
    "Authorization": f"Token {os.getenv('NAVIREC_TOKEN')}",
    "Accept": "application/json; version=1.52.1",
    "User-Agent": "O-O-TRANS-Navirec/1.0"
}

HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>O&O TRANS</title>

    <link rel="stylesheet"
          href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">

    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>

    <style>
        body {
            font-family: Arial;
            background: #f4f6f8;
            padding: 20px;
        }

        .vehicle {
            background: white;
            padding: 15px;
            margin: 10px 0;
            border-radius: 10px;
        }

        #map {
            height: 600px;
            margin-top: 20px;
            border-radius: 12px;
        }
    </style>
</head>

<body>

<h1>🚚 O&O TRANS</h1>

<p>Автомобілів: <b>{{ vehicles|length }}</b></p>

{% for v in vehicles %}
<div class="vehicle">
    <b>🚚 {{ v.name }}</b><br>
    Номер: {{ v.registration }}<br>
    ID: {{ v.id }}
</div>
{% endfor %}

<div id="map"></div>

<script>
    const vehicles = {{ vehicles|tojson }};

    const map = L.map('map').setView([52.0, 19.0], 6);

    L.tileLayer(
        'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
        {
            attribution: '© OpenStreetMap'
        }
    ).addTo(map);

    async function loadPositions() {

        for (const vehicle of vehicles) {

            try {

                const response = await fetch(
                    "/position/" + vehicle.id
                );

                const data = await response.json();

                console.log(vehicle.name, data);

                if (data.latitude && data.longitude) {

                    const marker = L.marker([
                        data.latitude,
                        data.longitude
                    ]).addTo(map);

                    marker.bindPopup(
                        "<b>🚚 " +
                        vehicle.name +
                        "</b><br>" +
                        vehicle.registration
                    );

                }

            } catch (error) {

                console.log(
                    "Помилка позиції:",
                    vehicle.name,
                    error
                );

            }
        }
    }

    loadPositions();
</script>

</body>
</html>
"""


@app.route("/")
def home():

    response = requests.get(
        API + "/vehicles/",
        headers=HEADERS,
        timeout=20
    )

    if response.status_code != 200:
        return f"Помилка Navirec: {response.status_code}"

    vehicles = response.json()

    return render_template_string(
        HTML,
        vehicles=vehicles
    )


@app.route("/position/<vehicle_id>")
def position(vehicle_id):

    endpoints = [
        f"/vehicles/{vehicle_id}/position/",
        f"/vehicles/{vehicle_id}/positions/",
        f"/positions/?vehicle={vehicle_id}",
        f"/vehicle_positions/?vehicle={vehicle_id}"
    ]

    results = []

    for endpoint in endpoints:

        try:

            response = requests.get(
                API + endpoint,
                headers=HEADERS,
                timeout=10
            )

            results.append({
                "endpoint": endpoint,
                "status": response.status_code,
                "text": response.text[:1000]
            })

        except Exception as e:

            results.append({
                "endpoint": endpoint,
                "error": str(e)
            })

    return {
        "vehicle_id": vehicle_id,
        "results": results
    }


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=10000
    )
