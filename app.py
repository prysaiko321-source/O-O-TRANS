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
    <p>Перевірка API Navirec</p>
    <p><a href="/events-test">Відкрити</a></p>
    """


@app.route("/events-test")
def events_test():

    try:
        response = requests.get(
            f"{API}/vehicle_events/",
            headers=HEADERS,
            timeout=30
        )

        return (
            "<pre>"
            f"STATUS: {response.status_code}\n\n"
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
