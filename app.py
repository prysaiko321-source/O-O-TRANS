import os
import requests
from flask import Flask

app = Flask(__name__)

API = "https://api.navirec.com"
TOKEN = os.getenv("NAVIREC_TOKEN")

ACCOUNT_ID = "5c980074-7a71-4c9b-b5a8-a7c45163adf5"

HEADERS = {
    "Authorization": f"Token {TOKEN}",
    "Accept": "application/json; version=1.52.1",
    "User-Agent": "O-O-TRANS/1.0"
}


@app.route("/")
def home():
    return """
    <h1>O&O TRANS</h1>
    <p>Navirec GPS</p>
    <p><a href="/gps">Відкрити GPS автомобілів</a></p>
    """


@app.route("/gps")
def gps():

    url = f"{API}/last_vehicle_states/"
    params = {
        "account": ACCOUNT_ID
    }

    try:
        response = requests.get(
            url,
            headers=HEADERS,
            params=params,
            timeout=30
        )

        if response.status_code != 200:
            return f"""
            <pre>
СТАТУС: {response.status_code}

ВІДПОВІДЬ NAVIREC:
{response.text}
            </pre>
            """

        data = response.json()

        output = [
            "=== O&O TRANS — GPS ===",
            ""
        ]

        for vehicle in data:

            vehicle_url = vehicle.get("vehicle", "")
            time = vehicle.get("time")
            speed = vehicle.get("speed")
            heading = vehicle.get("heading")
            location = vehicle.get("location")

            output.append("────────────────────────")
            output.append(f"VEHICLE: {vehicle_url}")
            output.append(f"ЧАС: {time}")
            output.append(f"ШВИДКІСТЬ: {speed} km/h")
            output.append(f"НАПРЯМОК: {heading}")
            output.append(f"GPS: {location}")
            output.append("")

        output.append("────────────────────────")
        output.append(f"КІЛЬКІСТЬ АВТО: {len(data)}")

        return "<pre>" + "\n".join(output) + "</pre>"

    except Exception as e:
        return "<pre>ПОМИЛКА:\n" + repr(e) + "</pre>"


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=10000
    )
