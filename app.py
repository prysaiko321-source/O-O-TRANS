import os
import requests
from flask import Flask

app = Flask(__name__)

@app.route("/")
def home():
    token = os.getenv("NAVIREC_TOKEN")

    if not token:
        return "O&O TRANS — Navirec підключення не налаштовано."

    url = "https://api.navirec.com/vehicles/"

    headers = {
        "Authorization": f"Token {token}",
        "Accept": "application/json; version=1.52.1",
        "User-Agent": "O-O-TRANS-Navirec/1.0"
    }

    try:
        response = requests.get(url, headers=headers, timeout=20)

        if response.status_code != 200:
            return "O&O TRANS — не вдалося отримати дані з Navirec."

        vehicles = response.json()

        result = "O&O TRANS — Navirec<br><br>"
        result += f"Знайдено бусів: {len(vehicles)}<br><br>"

        for vehicle in vehicles:
            registration = vehicle.get("registration", "—")
            name = vehicle.get("name_display") or vehicle.get("name") or "—"
            result += f"🚐 {registration} — {name}<br>"

        return result

    except Exception:
        return "O&O TRANS — Navirec тимчасово недоступний."


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
