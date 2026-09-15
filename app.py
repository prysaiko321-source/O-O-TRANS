import os
import json
import threading
import time
import requests

from flask import Flask, render_template_string

app = Flask(__name__)

API = "https://api.navirec.com"

TOKEN = os.getenv("NAVIREC_TOKEN")

HEADERS = {
    "Authorization": f"Token {TOKEN}",
    "Accept": "application/json; version=1.52.1",
    "User-Agent": "O-O-TRANS-Navirec/1.0"
}

STREAM_HEADERS = {
    "Authorization": f"Token {TOKEN}",
    "Accept": "application/x-ndjson; version=1.52.1",
    "User-Agent": "O-O-TRANS-Navirec/1.0"
}

vehicles = []
positions = {}
stream_started = False


def start_stream():
    global stream_started

    if stream_started:
        return

    stream_started = True

    while True:
        try:
            response = requests.get(
                API + "/vehicles/",
                headers=HEADERS,
                timeout=20
            )

            if response.status_code != 200:
                time.sleep(10)
                continue

            data = response.json()

            if not data:
                time.sleep(10)
                continue

            account_url = data[0].get("account")

            if not account_url:
                time.sleep(10)
                continue

            account_id = account_url.rstrip("/").split("/")[-1]

            print("O&O TRANS: account =", account_id)

            stream_url = (
                API
                + "/streams/vehicle_states/?account="
                + account_id
            )

            print("O&O TRANS: connecting to Navirec stream...")

            with requests.get(
                stream_url,
                headers=STREAM_HEADERS,
                stream=True,
                timeout=(20, 60)
            ) as stream:

                print(
                    "O&O TRANS: stream status =",
                    stream.status_code
                )

                if stream.status_code != 200:
                    time.sleep(10)
                    continue

                for line in stream.iter_lines():

                    if not line:
                        continue

                    try:
                        event = json.loads(
                            line.decode("utf-8")
                        )

                        if event.get("event") != "vehicle_state":
                            continue

                        state = event.get("data", {})

                        vehicle_url = state.get("vehicle")

                        if not vehicle_url:
                            continue

                        vehicle_id = (
                            vehicle_url
                            .rstrip("/")
                            .split("/")[-1]
                        )

                        location = state.get("location")

                        if not location:
                            continue

                        coordinates = location.get(
                            "coordinates"
                        )

                        if not coordinates:
                            continue

                        if len(coordinates) < 2:
                            continue

                        longitude = coordinates[0]
                        latitude = coordinates[1]

                        positions[vehicle_id] = {
                            "latitude": latitude,
                            "longitude": longitude,
                            "speed": state.get("speed"),
                            "heading": state.get("heading"),
                            "activity": state.get("activity"),
                            "updated_at": state.get("updated_at")
                        }

                        print(
                            "POSITION:",
                            vehicle_id,
                            latitude,
                            longitude
                        )

                    except Exception as e:
                        print(
                            "Stream event error:",
                            e
                        )

        except Exception as e:

            print(
                "Navirec stream error:",
                e
            )

            time.sleep(10)


HTML = """
<!DOCTYPE html>

<html>

<head>

<meta charset="UTF-8">

<title>O&O TRANS</title>

<link
rel="stylesheet"
href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
/>

<script
src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js">
</script>

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

<p>
Автомобілів:
<b>{{ vehicles|length }}</b>
</p>

{% for v in vehicles %}

<div class="vehicle">

<b>🚚 {{ v.name }}</b>

<br>

Номер:
<b>{{ v.registration }}</b>

<br>

Статус:
{% if v.active %}
<span style="color:green">
🟢 Активний
</span>
{% else %}
<span style="color:red">
🔴 Неактивний
</span>
{% endif %}

</div>

{% endfor %}

<div id="map"></div>

<script>

const vehicles = {{ vehicles|tojson }};

const map = L.map("map").setView(
    [52.0, 19.0],
    6
);

L.tileLayer(
    "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    {
        attribution: "© OpenStreetMap"
    }
).addTo(map);


async function updatePositions() {

    try {

        const response = await fetch(
            "/positions"
        );

        const data = await response.json();

        const bounds = [];

        for (const vehicle of vehicles) {

            const position =
                data[vehicle.id];

            if (!position) {
                continue;
            }

            const lat =
                position.latitude;

            const lon =
                position.longitude;

            const marker =
                L.marker([lat, lon])
                .addTo(map);

            marker.bindPopup(
                "<b>🚚 "
                + vehicle.name
                + "</b><br>"
                + vehicle.registration
                + "<br>Швидкість: "
                + (position.speed ?? "—")
                + " km/h"
            );

            bounds.push([lat, lon]);
        }

        if (bounds.length > 0) {

            map.fitBounds(
                bounds,
                {
                    padding: [40, 40]
                }
            );
        }

    } catch (error) {

        console.log(error);

    }
}


updatePositions();

setInterval(
    updatePositions,
    30000
);

</script>

</body>

</html>
"""


@app.route("/")
def home():

    global vehicles

    response = requests.get(
        API + "/vehicles/",
        headers=HEADERS,
        timeout=20
    )

    if response.status_code != 200:
        return (
            "Помилка Navirec: "
            + str(response.status_code)
        )

    vehicles = response.json()

    return render_template_string(
        HTML,
        vehicles=vehicles
    )


@app.route("/positions")
def get_positions():

    return positions


threading.Thread(
    target=start_stream,
    daemon=True
).start()


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=10000
    )
