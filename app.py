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

STREAM_HEADERS = {
    "Authorization": f"Token {TOKEN}",
    "Accept": "application/x-ndjson; version=1.52.1",
    "User-Agent": "O-O-TRANS/1.0"
}


@app.route("/")
def home():
    return """
    <h1>O&O TRANS</h1>
    <p>Navirec stream diagnostic</p>
    <p><a href="/stream-test">Перевірити потік GPS</a></p>
    """


@app.route("/stream-test")
def stream_test():

    output = []

    try:
        r = requests.get(
            f"{API}/vehicles/",
            headers=HEADERS,
            timeout=20
        )

        vehicles = r.json()

        if isinstance(vehicles, dict):
            vehicles = vehicles.get("results", [])

        output.append(f"VEHICLES: {len(vehicles)}")

        account_url = vehicles[0]["account"]
        account_id = account_url.rstrip("/").split("/")[-1]

        output.append(f"ACCOUNT: {account_id}")

        url = (
            f"{API}/streams/vehicle_states/"
            f"?account={account_id}"
        )

        output.append(f"STREAM STATUS: підключення...")

        response = requests.get(
            url,
            headers=STREAM_HEADERS,
            stream=True,
            timeout=(20, 15)
        )

        output.append(
            f"STREAM STATUS: {response.status_code}"
        )

        output.append(
            f"CONTENT-TYPE: {response.headers.get('Content-Type')}"
        )

        output.append("")
        output.append("=== RAW STREAM ===")

        count = 0

        for line in response.iter_lines(
            decode_unicode=False
        ):

            if line is None:
                continue

            count += 1

            if isinstance(line, bytes):
                text = line.decode(
                    "utf-8",
                    errors="replace"
                )
            else:
                text = str(line)

            output.append(
                f"LINE {count}: {text}"
            )

            if count >= 10:
                break

        output.append("")
        output.append(
            f"Отримано рядків: {count}"
        )

    except Exception as e:

        output.append("")
        output.append(
            f"ERROR: {repr(e)}"
        )

    return "<pre>" + "\n".join(output) + "</pre>"


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=10000
    )
