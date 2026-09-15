
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
    return "O&O TRANS bot працює!"


@app.route("/debug-stream")
def debug_stream():
    try:
        # Отримуємо автомобілі
        r = requests.get(
            f"{API}/vehicles/",
            headers=HEADERS,
            timeout=15
        )

        result = []
        result.append(f"VEHICLES STATUS: {r.status_code}")

        data = r.json()

        if isinstance(data, dict):
            vehicles = data.get("results", [])
        else:
            vehicles = data

        result.append(f"VEHICLES: {len(vehicles)}")

        if not vehicles:
            return "<pre>" + "\n".join(result) + "</pre>"

        # Беремо account першого автомобіля
        account_url = vehicles[0].get("account", "")
        account_id = account_url.rstrip("/").split("/")[-1]

        result.append(f"ACCOUNT: {account_id}")

        # Перевіряємо stream
        stream_url = f"{API}/streams/vehicle_states/?account={account_id}"

        result.append(f"STREAM URL: {stream_url}")

        stream_headers = {
            "Authorization": f"Token {TOKEN}",
            "Accept": "application/x-ndjson; version=1.52.1",
            "User-Agent": "O-O-TRANS/1.0"
        }

        s = requests.get(
            stream_url,
            headers=stream_headers,
            stream=True,
            timeout=(10, 5)
        )

        result.append(f"STREAM STATUS: {s.status_code}")
        result.append(f"CONTENT-TYPE: {s.headers.get('Content-Type')}")

        try:
            first_line = next(s.iter_lines())
            result.append("FIRST LINE:")
            result.append(first_line.decode("utf-8", errors="replace"))
        except Exception as e:
            result.append(f"FIRST LINE ERROR: {e}")

        return "<pre>" + "\n".join(result) + "</pre>"

    except Exception as e:
        return f"<pre>ERROR: {e}</pre>"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
