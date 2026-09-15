
import os
import requests
from flask import Flask

app = Flask(__name__)


@app.route("/")
def home():
    token = os.getenv("NAVIREC_TOKEN")

    if not token:
        return "<h2>❌ NAVIREC_TOKEN не знайдено</h2>"

    url = "https://api.navirec.com/vehicles/"

    headers = {
        "Authorization": f"Token {token}",
        "Accept": "application/json; version=1.52.1",
        "User-Agent": "O-O-TRANS-Navirec/1.0"
    }

    try:
        response = requests.get(url, headers=headers, timeout=20)

        if response.status_code != 200:
            return f"<h2>❌ Помилка API: {response.status_code}</h2><pre>{response.text}</pre>"

        vehicles = response.json()

        html = """
        <html>
        <head>
            <meta charset="UTF-8">
            <title>O&O TRANS — Автомобілі</title>
            <style>
                body {
                    font-family: Arial, sans-serif;
                    background: #f4f6f8;
                    padding: 30px;
                }
                h1 {
                    color: #222;
                }
                .vehicle {
                    background: white;
                    padding: 20px;
                    margin: 15px 0;
                    border-radius: 12px;
                    box-shadow: 0 2px 8px rgba(0,0,0,0.1);
                }
                .name {
                    font-size: 22px;
                    font-weight: bold;
                    margin-bottom: 10px;
                }
                .status {
                    color: green;
                    font-weight: bold;
                }
            </style>
        </head>
        <body>
            <h1>🚚 O&O TRANS — Автомобілі</h1>
        """

        html += f"<p>Знайдено автомобілів: <b>{len(vehicles)}</b></p>"

        for vehicle in vehicles:
            name = vehicle.get("name", "Без назви")
            registration = vehicle.get("registration", "—")
            active = vehicle.get("active", False)

            status = "🟢 Активний" if active else "🔴 Неактивний"

            html += f"""
            <div class="vehicle">
                <div class="name">🚚 {name}</div>
                <div>Номер: <b>{registration}</b></div>
                <div class="status">{status}</div>
            </div>
            """

        html += """
        </body>
        </html>
        """

        return html

    except Exception as e:
        return f"<h2>❌ Помилка</h2><pre>{e}</pre>"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
