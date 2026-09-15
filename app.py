import os
import requests
from flask import Flask

app = Flask(__name__)

@app.route("/")
def home():
    token = os.getenv("NAVIREC_TOKEN")

    if not token:
        return "NAVIREC_TOKEN НЕ знайдено."

    url = "https://api.navirec.com/vehicles"

    headers = {
        "Authorization": f"Token {token}",
        "Accept": "application/json; version=1.52.1"
    }

    try:
        response = requests.get(url, headers=headers, timeout=20)

        if response.status_code != 200:
            return f"Navirec помилка: {response.status_code}"

        vehicles = response.json()

        result = "O&O TRANS — Navirec<br><br>"
        result += f"Знайдено бусів: {len(vehicles)}<br><br>"

        for vehicle in vehicles:
            registration = vehicle.get("registration", "—")
            name = vehicle.get("name_display") or vehicle.get("name") or "—"
            result += f"🚐 {registration} — {name}<br>"

        return result

    except Exception as e:
        return f"Помилка підключення до Navirec: {str(e)}"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
