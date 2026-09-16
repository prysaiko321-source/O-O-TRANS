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

ACCOUNT_ID = "5c980074-7a71-4c9b-b5a8-a7c45163adf5"


@app.route("/")
def home():
    return """
    <h1>O&O TRANS</h1>
    <p>Navirec GPS test</p>
    <p><a href="/gps">Перевірити GPS</a></p>
    """


@app.route("/gps")
def gps():

    url = f"{API}/streams/vehicle_states/?account={ACCOUNT_ID}"

    try:
        response = requests.get(
            url,
            headers=HEADERS,
            stream=True,
            timeout=(10, 35)
        )

        output = [
            f"STATUS: {response.status_code}",
            f"CONTENT-TYPE: {response.headers.get('Content-Type')}",
            "",
            "=== NAVIREC STREAM ==="
        ]

        for i, line in enumerate(response.iter_lines(decode_unicode=True)):

            if line:
                output.append(f"РЯДОК {i + 1}: {line}")

            if i >= 30:
                break

        return "<pre>" + "\n".join(output) + "</pre>"

    except Exception as e:
        return "<pre>ПОМИЛКА:\n" + repr(e) + "</pre>"


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=10000
    )
