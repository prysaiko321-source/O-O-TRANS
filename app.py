
import os
import requests
from flask import Flask

app = Flask(__name__)

NAVIREC_URL = "https://api.navirec.com/vehicles/"

HEADERS = {
    "Authorization": f"Token {os.getenv('NAVIREC_TOKEN')}",
    "Accept": "application/json; version=1.52.1",
    "User-Agent": "O-O-TRANS-Navirec/1.0"
}


@app.route("/")
def home():
    token = os.getenv("NAVIREC_TOKEN")

    if not token:
        return "<h2>❌ NAVIREC_TOKEN не знайдено</h2>"

    try:
        response = requests.get(
            NAVIREC_URL,
            headers=HEADERS,
            timeout=20
        )

        if response.status_code != 200:
            return f"""
            <h2>❌ Помилка Navirec: {response.status_code}</h2>
            <pre>{response.text}</pre>
            """

        vehicles = response.json()

        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <title>O&O TRANS — Автомобілі</title>

            <style>
                body {
                    font-family: Arial, sans-serif;
                    background: #f4f6f8;
                    margin: 0;
                    padding: 25px;
                }

                h1 {
                    margin-bottom: 5px;
                }

                .vehicle {
                    background: white;
                    padding: 18px;
                    margin: 15px 0;
                    border-radius: 12px;
                    box-shadow: 0 2px 8px rgba(0,0,0,0.1);
                }

                .name {
                    font-size: 21px;
                    font-weight: bold;
                }

                .active {
                    color: green;
                    font-weight: bold;
                }

                .inactive {
                    color: red;
                    font-weight: bold;
                }

                #map {
                    height: 500px;
                    margin-top: 25px;
                    border-radius: 12px;
                }
            </style>

            <link
                rel="stylesheet"
                href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
            />

            <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
        </head>

        <body>

        <h1>🚚 O&O TRANS — Автомобілі</h1>

        <p>
            Знайдено автомобілів:
            <b>{{ count }}</b>
        </p>

        """

        for vehicle in vehicles:

            name = vehicle.get("name", "Без назви")
            registration = vehicle.get("registration", "—")
            active = vehicle.get("active", False)

            status = (
                '<span class="active">🟢 Активний</span>'
                if active
                else '<span class="inactive">🔴 Неактивний</span>'
            )

            html += f"""
            <div class="vehicle">
                <div class="name">🚚 {name}</div>
                <div>Номер: <b>{registration}</b></div>
                <div>Статус: {status}</div>
            </div>
            """

        html += """
        <div id="map"></div>

        <script>
            const vehicles = {{ vehicles|tojson }};

            let map = L.map('map').setView([52.0, 19.0], 6);

            L.tileLayer(
                'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
                {
                    attribution: '&copy; OpenStreetMap contributors'
                }
            ).addTo(map);

            let markers = [];

            vehicles.forEach(vehicle => {

                let lat = vehicle.latitude;
                let lon = vehicle.longitude;

                if (
                    lat !== null &&
                    lon !== null &&
                    lat !== undefined &&
                    lon !== undefined
                ) {

                    let marker = L.marker([lat, lon]).addTo(map);

                    marker.bindPopup(
                        "<b>🚚 " +
                        vehicle.name +
                        "</b><br>" +
                        "Numer: " +
                        vehicle.registration
                    );

                    markers.push(marker);
                }
            });

            if (markers.length > 0) {

                let group = L.featureGroup(markers);

                map.fitBounds(group.getBounds(), {
                    padding: [30, 30]
                });
            }
        </script>

        </body>
        </html>
        """

        from flask import render_template_string

        return render_template_string(
            html,
            count=len(vehicles),
            vehicles=vehicles
        )

    except Exception as e:

        return f"""
        <h2>❌ Помилка</h2>
        <pre>{e}</pre>
        """


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=10000
    )
