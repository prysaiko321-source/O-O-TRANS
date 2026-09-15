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
    <p>Перевірка поїздок Navirec</p>
    <p><a href="/trips-test">Відкрити перевірку</a></p>
    """


@app.route("/trips-test")
def trips_test():

    output = []

    try:
        # Отримуємо автомобілі
        r = requests.get(
            f"{API}/vehicles/",
            headers=HEADERS,
            timeout=20
        )

        output.append(
            f"VEHICLES STATUS: {r.status_code}"
        )

        vehicles = r.json()

        if isinstance(vehicles, dict):
            vehicles = vehicles.get("results", [])

        output.append(
            f"КІЛЬКІСТЬ АВТО: {len(vehicles)}"
        )

        # Перевіряємо перший автомобіль
        vehicle = vehicles[0]

        vehicle_id = vehicle["id"]
        vehicle_name = vehicle.get("name", "")

        output.append("")
        output.append("=" * 60)
        output.append(f"АВТО: {vehicle_name}")
        output.append(f"ID: {vehicle_id}")
        output.append("=" * 60)

        # Запит по vehicle
        url = f"{API}/trips/?vehicle={vehicle_id}"

        output.append(
            f"URL: /trips/?vehicle={vehicle_id}"
        )

        response = requests.get(
            url,
            headers=HEADERS,
            timeout=30
        )

        output.append(
            f"TRIPS STATUS: {response.status_code}"
        )

        output.append(
            f"CONTENT-TYPE: {response.headers.get('Content-Type')}"
        )

        output.append("")
        output.append("=== ВІДПОВІДЬ NAVIREC ===")

        # Показуємо відповідь повністю, але максимум 10000 символів
        output.append(
            response.text[:10000]
        )

    except Exception as e:

        output.append("")
        output.append(
            f"ПОМИЛКА: {repr(e)}"
        )

    return "<pre>" + "\n".join(output) + "</pre>"


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=10000
    )
