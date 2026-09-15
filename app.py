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
    print("O&O TRANS: START STREAM", flush=True)

    try:
        r = requests.get(
            f"{API}/vehicles/",
            headers=HEADERS,
            timeout=20
        )

        print(
            "O&O TRANS: vehicles status =",
            r.status_code,
            flush=True
        )

        vehicles = r.json()

        if isinstance(vehicles, dict):
            vehicles = vehicles.get("results", [])

        print(
            "O&O TRANS: vehicles =",
            len(vehicles),
            flush=True
        )

        if not vehicles:
            print(
                "O&O TRANS: NO VEHICLES",
                flush=True
            )
            return

        account_url = vehicles[0].get("account", "")
        account_id = account_url.rstrip("/").split("/")[-1]

        print(
            "O&O TRANS: account =",
            account_id,
            flush=True
        )

        stream_url = (
            f"{API}/streams/vehicle_states/"
            f"?account={account_id}"
        )

        print(
            "O&O TRANS: connecting to Navirec stream...",
            flush=True
        )

        stream = requests.get(
            stream_url,
            headers=STREAM_HEADERS,
            stream=True,
            timeout=(20, None)
        )

        print(
            "O&O TRANS: stream status =",
            stream.status_code,
            flush=True
        )

        print(
            "O&O TRANS: content type =",
            stream.headers.get("Content-Type"),
            flush=True
        )

        for line in stream.iter_lines(
            decode_unicode=True
        ):

            if not line:
                continue

            print(
                "NAVIREC RAW:",
                line,
                flush=True
            )

            try:
                event = json.loads(line)
            except Exception as e:
                print(
                    "JSON ERROR:",
                    e,
                    flush=True
                )
                continue

            print(
                "NAVIREC EVENT:",
                event,
                flush=True
            )

            event_type = event.get("event")

            if event_type != "vehicle_state":
                continue

            data = event.get("data", {})

            print(
                "VEHICLE DATA:",
                data,
                flush=True
            )

            vehicle = data.get("vehicle")
            location = data.get("location")

            if not vehicle:
                print(
                    "NO VEHICLE ID",
                    flush=True
                )
                continue

            if not location:
                print(
                    "NO LOCATION:",
                    vehicle,
                    flush=True
                )
                continue

            coordinates = location.get("coordinates")

            if not coordinates:
                print(
                    "NO COORDINATES:",
                    vehicle,
                    location,
                    flush=True
                )
                continue

            positions[vehicle] = {
                "longitude": coordinates[0],
                "latitude": coordinates[1]
            }

            print(
                "GPS:",
                vehicle,
                coordinates[1],
                coordinates[0],
                flush=True
            )

    except Exception as e:
        print(
            "O&O TRANS STREAM ERROR:",
            repr(e),
            flush=True
        )


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
