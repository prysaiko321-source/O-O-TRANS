import os
import requests
from flask import Flask

app = Flask(__name__)

API = "https://api.navirec.com"
TOKEN = os.getenv("NAVIREC_TOKEN")

HEADERS = {
    "Authorization": f"Token {TOKEN}",
    "Accept": "application/json; version=1.52.1",
    "User-Agent": "O-O-TRANS/1.0"
}


@app.route("/")
def home():
    return """
    <h1>O&O TRANS</h1>
    <p>Navirec API test</p>
    <p><a href="/gps-test">Перевірити GPS</a></p>
    """


@app.route("/gps-test")
def gps_test():

    results = []

    # Отримуємо автомобілі
    try:
        r = requests.get(
            f"{API}/vehicles/",
            headers=HEADERS,
            timeout=15
        )

        results.append(
            f"VEHICLES: {r.status_code}"
        )

        data = r.json()

        if isinstance(data, dict):
            vehicles = data.get("results", [])
        else:
            vehicles = data

    except Exception as e:
        return f"<pre>VEHICLES ERROR: {e}</pre>"

    results.append(
        f"КІЛЬКІСТЬ АВТО: {len(vehicles)}"
    )

    # Перевіряємо можливі GPS endpoint-и
    for vehicle in vehicles:

        vehicle_id = vehicle.get("id")
        name = vehicle.get("name", "без назви")

        results.append("")
        results.append("=" * 50)
        results.append(
            f"AUTO: {name}"
        )
        results.append(
            f"ID: {vehicle_id}"
        )

        endpoints = [
            f"/vehicles/{vehicle_id}/state/",
            f"/vehicles/{vehicle_id}/states/",
            f"/vehicles/{vehicle_id}/location/",
            f"/vehicles/{vehicle_id}/locations/",
            f"/vehicle_states/?vehicle={vehicle_id}",
            f"/vehicle_states/?vehicle={vehicle_id}/",
            f"/positions/?vehicle={vehicle_id}",
            f"/locations/?vehicle={vehicle_id}",
            f"/states/?vehicle={vehicle_id}",
        ]

        for endpoint in endpoints:

            try:
                url = API + endpoint

                response = requests.get(
                    url,
                    headers=HEADERS,
                    timeout=5
                )

                results.append(
                    f"{endpoint} -> {response.status_code}"
                )

                if response.status_code == 200:
                    results.append(
                        ">>> ЗНАЙДЕНО ВІДПОВІДЬ:"
                    )
                    results.append(
                        response.text[:3000]
                    )

            except Exception as e:
                results.append(
                    f"{endpoint} -> ERROR {e}"
                )

    return "<pre>" + "\n".join(results) + "</pre>"


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=10000
    )
