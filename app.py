import os
from flask import Flask

app = Flask(__name__)

@app.route("/")
def home():
    token = os.getenv("NAVIREC_TOKEN")

    if token:
        return "O&O TRANS bot працює! NAVIREC_TOKEN отримано."
    else:
        return "O&O TRANS bot працює, але NAVIREC_TOKEN НЕ знайдено."

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
