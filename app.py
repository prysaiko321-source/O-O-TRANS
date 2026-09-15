import os
import json
import threading
import requests
from flask import Flask, jsonify

app = Flask(__name__)

API = "https://api.navirec.com"
TOKEN = os.getenv("NAVIREC_TOKEN")

HEADERS = {
    "Authorization": f"Token {TOKEN}",
    "Accept": "application/json; version=1.52.1",
    "User-Agent": "O-O-TRANS/1.0"
}

STREAM_HEADERS = {
    "Authorization": f"Token {TOKEN}",
    "Accept": "application/x-ndjson; version=1.52.1",
    "User-Agent": "O-O-TRANS/1.0"
}

positions = {}


def read_stream():
    try:
        r = requests.get(
            f"{API}/vehicles/",
            headers=HEADERS,
            timeout=20
        )

        vehicles = r.json()

        if isinstance(vehicles, dict):
            vehicles = vehicles.get("results", [])

        if not vehicles:
            print("O&O TRANS: немає автомобілів")
            return

        account_url = vehicles[0].get("account", "")
        account_id = account_url.rstrip("/").split("/")[-1]

        stream_url = f"{API}/streams/vehicle_states/?account={account_id}"

        print("O&O TRANS: підключення до Navirec stream...")

        stream = requests.get(
            stream_url,
            headers=STREAM_HEADERS,
            stream=True,
            timeout=(20, None)
        )

        print("O&O TRANS: stream status =", stream.status_code)

        for line in stream.iter_lines(decode_unicode=True):

            if not line:
                continue

            try:
                event = json.loads(line)
            except Exception:
                continue

            print("NAVIREC:", event)

            if event.get("event") != "vehicle_state":
                continue

            data = event.get("data", {})

            vehicle = data.get("vehicle")
            location = data.get("location")

            if not vehicle or not location:
                continue

            coordinates = location.get("coordinates")

            if not coordinates:
                continue

            positions[vehicle] = {
                "longitude": coordinates[0],
                "latitude": coordinates[1]
            }

            print(
                "GPS:",
                vehicle,
                coordinates[1],
                coordinates[0]
            )

    except Exception as e:
        print("O&O TRANS STREAM ERROR:", e)


@app.route("/")
def home():
    return """
    <h1>O&O TRANS</h1>
    <p>Navirec GPS працює.</p>
    <p><a href="/positions">Переглянути GPS</a></p>
    """


@app.route("/positions")
def get_positions():
    return jsonify(positions)


if __name__ == "__main__":
    thread = threading.Thread(
        target=read_stream,
        daemon=True
    )
    thread.start()

    app.run(
        host="0.0.0.0",
        port=10000
    )
