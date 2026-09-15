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
    <p>O&O TRANS — Navirec vehicle events</p>
    <p><a href="/events-test">Перевірити події</a></p>
    """


@app.route("/events-test")
def events_test():

    try:
        # Отримуємо автомобілі
        r = requests.get(
            f"{API}/vehicles/",
            headers=HEADERS,
            timeout=20
        )

        vehicles = r.json()

        if isinstance(vehicles, dict):
            vehicles = vehicles.get("results", [])

        vehicle = vehicles[0]

        vehicle_id = vehicle["id"]
        vehicle_name = vehicle.get("name", "")

        # Запит vehicle_events
        url = (
            f"{API}/vehicle_events/"
            f"?vehicle={vehicle_id}"
        )

        response = requests.get(
            url,
            headers=HEADERS,
            timeout=30
        )

        return (
            "<pre>"
            f"АВТО: {vehicle_name}\n"
            f"ID: {vehicle_id}\n\n"
            f"URL: {url}\n\n"
            f"STATUS: {response.status_code}\n"
            f"CONTENT-TYPE: "
            f"{response.headers.get('Content-Type')}\n\n"
            f"=== NAVIREC ===\n"
            f"{response.text[:20000]}"
            "</pre>"
        )

    except Exception as e:
        return f"<pre>ПОМИЛКА: {repr(e)}</pre>"


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=10000
    )
