import os
import requests
from flask import Flask

app = Flask(__name__)

API = "https://api.navirec.com"
TOKEN = os.getenv("NAVIREC_TOKEN")

ACCOUNT_ID = "5c980074-7a71-4c9b-b5a8-a7c45163adf5"

HEADERS = {
    "Authorization": f"Token {TOKEN}",
    "Accept": "application/json; version=1.52.1",
    "User-Agent": "O-O-TRANS/1.0"
}

VEHICLES = {
    "aaaa9acd-5bb5-467e-8241-81444292bbfe": "Renault Master SH 9203G",
    "cbb121b6-34dd-41c6-974b-5b7aa3d9a1cb": "Renault Master DX 9034F",
    "f016af91-dee6-4e72-9f86-4b2e27a253c1": "Renault Master DX 5405A"
}


@app.route("/")
def home():
    return """
    <h1>O&O TRANS</h1>
    <h2>Navirec GPS</h2>
    <p><a href="/gps">Відкрити GPS автомобілів</a></p>
    """


@app.route("/gps")
def gps():

    url = f"{API}/last_vehicle_states/"

    try:
        response = requests.get(
            url,
            headers=HEADERS,
            params={"account": ACCOUNT_ID},
            timeout=30
        )

        if response.status_code != 200:
            return f"""
            <h2>Помилка Navirec</h2>
            <pre>Статус: {response.status_code}

{response.text}</pre>
            """

        data = response.json()

        rows = ""

        for vehicle in data:

            vehicle_url = vehicle.get("vehicle", "")
            vehicle_id = vehicle_url.rstrip("/").split("/")[-1]

            name = VEHICLES.get(vehicle_id, vehicle_id)

            time = vehicle.get("time", "—")
            speed = vehicle.get("speed", "—")
            heading = vehicle.get("heading", "—")
            location = vehicle.get("location")

            latitude = ""
            longitude = ""

            if location and "coordinates" in location:
                longitude = location["coordinates"][0]
                latitude = location["coordinates"][1]

            maps = ""

            if latitude and longitude:
                maps = f'''
                <a href="https://www.google.com/maps?q={latitude},{longitude}"
                   target="_blank">
                   Відкрити карту
                </a>
                '''

            rows += f"""
            <tr>
                <td><b>{name}</b></td>
                <td>{time}</td>
                <td>{speed} км/год</td>
                <td>{heading}°</td>
                <td>{latitude}, {longitude}</td>
                <td>{maps}</td>
            </tr>
            """

        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <title>O&O TRANS GPS</title>

            <style>
                body {{
                    font-family: Arial, sans-serif;
                    margin: 30px;
                    background: #f5f5f5;
                }}

                h1 {{
                    color: #222;
                }}

                table {{
                    width: 100%;
                    border-collapse: collapse;
                    background: white;
                }}

                th, td {{
                    padding: 14px;
                    border: 1px solid #ddd;
                    text-align: left;
                }}

                th {{
                    background: #222;
                    color: white;
                }}

                a {{
                    color: #0066cc;
                    font-weight: bold;
                }}
            </style>
        </head>

        <body>

        <h1>🚚 O&O TRANS — GPS</h1>

        <table>
            <tr>
                <th>Автомобіль</th>
                <th>Останній сигнал</th>
                <th>Швидкість</th>
                <th>Напрямок</th>
                <th>Координати</th>
                <th>Карта</th>
            </tr>

            {rows}

        </table>

        </body>
        </html>
        """

    except Exception as e:
        return f"""
        <h2>Помилка</h2>
        <pre>{repr(e)}</pre>
        """


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=10000
    )
