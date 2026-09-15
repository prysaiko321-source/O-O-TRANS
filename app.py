import os
import requests
from flask import Flask

app = Flask(__name__)

@app.route("/")
def home():
    token = os.getenv("NAVIREC_TOKEN")

    if not token:
        return "NAVIREC_TOKEN не знайдено."

    url = "https://api.navirec.com/vehicles/"

    headers = {
        "Authorization": f"Token {token}",
        "Accept": "application/json; version=1.52.1",
        "User-Agent": "O-O-TRANS-Navirec/1.0"
    }

    try:
        response = requests.get(url, headers=headers, timeout=20)

        return f"<pre>{response.status_code}\n{response.text}</pre>"

    except Exception as e:
        return f"<pre>Помилка: {e}</pre>"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
