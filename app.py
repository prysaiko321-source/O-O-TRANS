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
    <p>Navirec vehicle_events diagnostic</p>
    <p><a href="/events-check">Запустити перевірку</a></p>
    """


@app.route("/events-check")
def events_check():

    try:
        vehicles_response = requests.get(
            f"{API}/vehicles/",
            headers=HEADERS,
            timeout=20
        )

        vehicles = vehicles_response.json()

        if isinstance(vehicles, dict):
            vehicles = vehicles.get("results", [])

        if not vehicles:
            return "<pre>Автомобілі не знайдені.</pre>"

        account_url = vehicles[0].get("account")
        account_id = account_url.rstrip("/").split("/")[-1]

        vehicle_id = vehicles[0].get("id")

        tests = {
            "account": account_id,
            "primary_account": account_id,
            "vehicle": vehicle_id,
            "vehicle_group": ""
        }

        output = []

        for parameter, value in tests.items():

            url = f"{API}/vehicle_events/"

            if value:
                url += f"?{parameter}={value}"

            try:
                response = requests.get(
                    url,
                    headers=HEADERS,
                    timeout=20
                )

                output.append(
                    f"""
==============================
ПАРАМЕТР: {parameter}
ЗНАЧЕННЯ: {value}
STATUS: {response.status_code}

ВІДПОВІДЬ:
{response.text[:10000]}
"""
                )

            except Exception as e:
                output.append(
                    f"""
==============================
ПАРАМЕТР: {parameter}
ПОМИЛКА:
{repr(e)}
"""
                )

        return "<pre>" + "\n".join(output) + "</pre>"

    except Exception as e:
        return f"<pre>ПОМИЛКА: {repr(e)}</pre>"


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=10000
    )
