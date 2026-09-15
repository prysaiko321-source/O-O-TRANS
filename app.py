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
vehicles = {}


def get_vehicles():
    response = requests.get(
        f"{API}/vehicles/",
        headers=HEADERS,
        timeout=20
    )

    data = response.json()

    if isinstance(data, dict):
        data = data.get("results", [])

    return data


def save_vehicle_state(data):
    if not isinstance(data, dict):
        return

    vehicle_id = data.get("vehicle")

    if isinstance(vehicle_id, dict):
        vehicle_id = vehicle_id.get("id")

    location = data.get("location")

    if not vehicle_id or not location:
        return

    coordinates = location.get("coordinates")

    if not coordinates or len(coordinates) < 2:
        return

    positions[vehicle_id] = {
        "latitude": coordinates[1],
        "longitude": coordinates[0]
    }

    print(
        "GPS:",
        vehicle_id,
        coordinates[1],
        coordinates[0],
        flush=True
    )


def read_stream():

    print("O&O TRANS: START STREAM", flush=True)

    try:
        vehicle_list = get_vehicles()

        print(
            "O&O TRANS: VEHICLES =",
            len(vehicle_list),
            flush=True
        )

        for vehicle in vehicle_list:
            vehicles[vehicle["id"]] = vehicle

        account_url = vehicle_list[0].get("account", "")
        account_id = account_url.rstrip("/").split("/")[-1]

        stream_url = (
            f"{API}/streams/vehicle_states/"
            f"?account={account_id}"
        )

        print(
            "O&O TRANS: STREAM =",
            stream_url,
            flush=True
        )

        response = requests.get(
            stream_url,
            headers=STREAM_HEADERS,
            stream=True,
            timeout=(20, None)
        )

        print(
            "O&O TRANS: STREAM STATUS =",
            response.status_code,
            flush=True
        )

        print(
            "O&O TRANS: CONTENT TYPE =",
            response.headers.get("Content-Type"),
            flush=True
        )

        for line in response.iter_lines():

            if not line:
                continue

            if isinstance(line, bytes):
                line = line.decode("utf-8", errors="replace")

            print(
                "NAVIREC RAW:",
                line,
                flush=True
            )

            try:
                event = json.loads(line)
            except Exception as error:
                print(
                    "JSON ERROR:",
                    error,
                    flush=True
                )
                continue

            event_type = event.get("event")

            print(
                "EVENT:",
                event_type,
                flush=True
            )

            # Початковий стан
            if event_type == "initial_state":

                data = event.get("data")

                if isinstance(data, list):
                    for item in data:
                        save_vehicle_state(item)

                elif isinstance(data, dict):
                    save_vehicle_state(data)

            # Поточний стан автомобіля
            elif event_type == "vehicle_state":

                data = event.get("data", {})

                save_vehicle_state(data)

    except Exception as error:

        print(
            "O&O TRANS STREAM ERROR:",
            repr(error),
            flush=True
        )


@app.route("/")
def home():

    return """
    <h1>O&O TRANS</h1>

    <h2>Navirec GPS</h2>

    <p>Автомобілі: 3</p>

    <p>
        <a href="/positions">
        Переглянути GPS
        </a>
    </p>
    """


@app.route("/positions")
def get_positions():

    result = []

    for vehicle_id, vehicle in vehicles.items():

        item = {
            "id": vehicle_id,
            "name": vehicle.get(
                "name",
                vehicle.get("registration", "")
            ),
            "registration": vehicle.get(
                "registration",
                ""
            )
        }

        if vehicle_id in positions:
            item.update(positions[vehicle_id])
            item["gps"] = True
        else:
            item["gps"] = False

        result.append(item)

    return jsonify(result)


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
