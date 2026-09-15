import os
import requests
from flask import Flask
from datetime import datetime, timezone

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
        # Автомобілі
        r = requests.get(
            f"{API}/vehicles/",
            headers=HEADERS,
            timeout=20
        )

        vehicles = r.json()

        if isinstance(vehicles, dict):
            vehicles = vehicles.get("results", [])

        output.append(
            f"КІЛЬКІСТЬ АВТО: {len(vehicles)}"
        )

        # Перший автомобіль
        vehicle = vehicles[0]

        vehicle_id = vehicle["id"]
        vehicle_name = vehicle.get("name", "")

        output.append("")
        output.append("=" * 60)
        output.append(f"АВТО: {vehicle_name}")
        output.append(f"ID: {vehicle_id}")
        output.append("=" * 60)

        # Сьогодні
        start = datetime.now(timezone.utc).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0
        )

        end = datetime.now(timezone.utc)

        start_str = start.isoformat().replace("+00:00", "Z")
        end_str = end.isoformat().replace("+00:00", "Z")

        url = (
            f"{API}/trips/"
            f"?vehicle={vehicle_id}"
            f"&time__range_start={start_str}"
            f"&time__range_end={end_str}"
        )

        output.append("")
        output.append("ПЕРІОД:")
        output.append(f"{start_str}")
        output.append(f"{end_str}")

        output.append("")
        output.append(
            "ВИКОНУЄМО ЗАПИТ ДО /trips/"
        )

        response = requests.get(
            url,
            headers=HEADERS,
            timeout=30
        )

        output.append("")
        output.append(
            f"TRIPS STATUS: {response.status_code}"
        )

        output.append(
            f"CONTENT-TYPE: {response.headers.get('Content-Type')}"
        )

        output.append("")
        output.append("=== ВІДПОВІДЬ NAVIREC ===")
        output.append(
            response.text[:15000]
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
