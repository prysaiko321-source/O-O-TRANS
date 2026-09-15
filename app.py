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
    <p>Navirec API diagnostic</p>
    <p><a href="/events-schema">Перевірити структуру vehicle_events</a></p>
    """


@app.route("/events-schema")
def events_schema():

    try:
        url = f"{API}/vehicle_events/"

        response = requests.options(
            url,
            headers=HEADERS,
            timeout=20
        )

        return (
            "<pre>"
            f"URL: {url}\n\n"
            f"STATUS: {response.status_code}\n"
            f"CONTENT-TYPE: {response.headers.get('Content-Type')}\n\n"
            f"=== HEADERS ===\n"
            f"{dict(response.headers)}\n\n"
            f"=== NAVIREC RESPONSE ===\n"
            f"{response.text[:30000]}"
            "</pre>"
        )

    except Exception as e:
        return f"<pre>ПОМИЛКА: {repr(e)}</pre>"


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=10000
    )
