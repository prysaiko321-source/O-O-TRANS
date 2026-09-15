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
    <p>Navirec API</p>
    <p><a href="/api-discovery">Відкрити перевірку API</a></p>
    """


@app.route("/api-discovery")
def api_discovery():

    results = []

    if not TOKEN:
        return "<pre>NAVIREC_TOKEN не знайдено в Render.</pre>"

    # Основні можливі розділи Navirec API
    endpoints = [
        "/",
        "/vehicles/",
        "/accounts/",
        "/users/",
        "/vehicle_states/",
        "/positions/",
        "/locations/",
        "/states/",
        "/trips/",
        "/journeys/",
        "/events/",
        "/streams/",
        "/devices/",
        "/trackers/",
        "/gps/",
    ]

    results.append("O&O TRANS — ПЕРЕВІРКА NAVIREC API")
    results.append("=" * 60)

    for endpoint in endpoints:
        try:
            url = API + endpoint
            response = requests.get(
                url,
                headers=HEADERS,
                timeout=10
            )

            results.append("")
            results.append(f"URL: {endpoint}")
            results.append(f"STATUS: {response.status_code}")
            results.append(
                f"CONTENT-TYPE: {response.headers.get('Content-Type')}"
            )

            if response.status_code != 404:
                text = response.text[:2000]
                results.append("ВІДПОВІДЬ:")
                results.append(text)

        except Exception as e:
            results.append("")
            results.append(f"URL: {endpoint}")
            results.append(f"ERROR: {e}")

    return "<pre>" + "\n".join(results) + "</pre>"


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=10000
    )
