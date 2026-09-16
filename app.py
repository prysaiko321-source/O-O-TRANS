import os
import json
from flask import Flask, redirect, url_for, session, request
import requests

app = Flask(__name__)

app.secret_key = os.getenv("SESSION_SECRET", "change-me")

NAVIREC_TOKEN = os.getenv("NAVIREC_TOKEN")
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")

NAVIREC_API = "https://api.navirec.com"

ACCOUNT_ID = "5c980074-7a71-4c9b-b5a8-a7c45163adf5"

VEHICLES = {
    "aaaa9acd-5bb5-467e-8241-81444292bbfe": "Renault Master SH 9203G",
    "cbb121b6-34dd-41c6-974b-5b7aa3d9a1cb": "Renault Master DX 9034F",
    "f016af91-dee6-4e72-9f86-4b2e27a253c1": "Renault Master DX 5405A",
}


def logged_in():
    return session.get("logged_in") is True


def get_headers():
    return {
        "Authorization": f"Token {NAVIREC_TOKEN}",
        "Accept": "application/json; version=1.52.1",
    }


def get_states():
    response = requests.get(
        f"{NAVIREC_API}/last_vehicle_states/",
        headers=get_headers(),
        params={"account": ACCOUNT_ID},
        timeout=20,
    )

    response.raise_for_status()
    return response.json()


def vehicle_status(state):
    if not isinstance(state, dict):
        return "—"

    for key in ["state", "status", "vehicle_state"]:
        if key in state:
            return str(state[key])

    return "—"


def format_distance(value):
    if value is None:
        return "—"

    try:
        return f"{float(value):,.1f} km".replace(",", " ")
    except Exception:
        return str(value)


def format_fuel(value):
    if value is None:
        return "—"

    try:
        return f"{float(value):,.1f} l".replace(",", " ")
    except Exception:
        return str(value)


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if username == ADMIN_USER and password == ADMIN_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("home"))

        error = "Неправильний логін або пароль"

    return f"""
    <!doctype html>
    <html lang="uk">
    <head>
        <meta charset="utf-8">
        <title>O&O TRANS — Login</title>
        <style>
            body {{
                font-family: Arial;
                background:#111827;
                color:white;
                display:flex;
                justify-content:center;
                align-items:center;
                height:100vh;
            }}
            .box {{
                background:#1f2937;
                padding:30px;
                border-radius:15px;
                width:320px;
            }}
            input {{
                width:100%;
                padding:12px;
                margin:8px 0;
                box-sizing:border-box;
                border-radius:8px;
                border:0;
            }}
            button {{
                width:100%;
                padding:12px;
                margin-top:10px;
                border:0;
                border-radius:8px;
                background:#2563eb;
                color:white;
                font-weight:bold;
            }}
            .error {{color:#f87171;}}
        </style>
    </head>
    <body>
        <div class="box">
            <h2>O&O TRANS</h2>
            <p>Вхід у систему</p>

            <form method="post">
                <input name="username" placeholder="Логін">
                <input name="password" type="password" placeholder="Пароль">
                <button>Увійти</button>
            </form>

            <p class="error">{error}</p>
        </div>
    </body>
    </html>
    """


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def home():
    if not logged_in():
        return redirect(url_for("login"))

    return """
    <!doctype html>
    <html lang="uk">
    <head>
        <meta charset="utf-8">
        <title>O&O TRANS</title>
        <style>
            body {
                font-family:Arial;
                background:#f3f4f6;
                margin:0;
            }
            header {
                background:#111827;
                color:white;
                padding:20px;
            }
            .container {
                padding:25px;
            }
            .menu {
                display:grid;
                grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
                gap:15px;
            }
            a {
                display:block;
                background:white;
                padding:25px;
                border-radius:12px;
                text-decoration:none;
                color:#111827;
                box-shadow:0 2px 8px #ddd;
            }
            a:hover {
                background:#e5e7eb;
            }
        </style>
    </head>
    <body>
        <header>
            <h1>O&O TRANS</h1>
            <p>Внутрішня система компанії</p>
        </header>

        <div class="container">
            <div class="menu">
                <a href="/vehicles">🚚 Самочини</a>
                <a href="/gps">📍 GPS</a>
                <a href="/fuel">⛽ Паливо</a>
                <a href="/tachograph-test">⏱ Тахограф — тест</a>
                <a href="/tachograph-options">🔧 Tachograph OPTIONS</a>
                <a href="/health">❤️ Health</a>
                <a href="/logout">🚪 Вийти</a>
            </div>
        </div>
    </body>
    </html>
    """


@app.route("/vehicles")
def vehicles():
    if not logged_in():
        return redirect(url_for("login"))

    try:
        states = get_states()
    except Exception as e:
        return f"<h2>Помилка Navirec</h2><pre>{e}</pre>"

    return f"""
    <html lang="uk">
    <head>
        <meta charset="utf-8">
        <title>O&O TRANS — Самочини</title>
    </head>
    <body style="font-family:Arial;padding:30px">
        <h1>🚚 Самочини</h1>
        <p>Account: {ACCOUNT_ID}</p>
        <pre>{json.dumps(states, indent=2, ensure_ascii=False)}</pre>
        <a href="/">← Назад</a>
    </body>
    </html>
    """


@app.route("/vehicle/<vehicle_id>")
def vehicle(vehicle_id):
    if not logged_in():
        return redirect(url_for("login"))

    name = VEHICLES.get(vehicle_id, vehicle_id)

    return f"""
    <html lang="uk">
    <head>
        <meta charset="utf-8">
        <title>{name}</title>
    </head>
    <body style="font-family:Arial;padding:30px">
        <h1>🚚 {name}</h1>
        <p>Vehicle ID: {vehicle_id}</p>
        <p>Тут буде детальна інформація по автомобілю.</p>
        <a href="/vehicles">← Назад</a>
    </body>
    </html>
    """


@app.route("/fuel")
def fuel():
    if not logged_in():
        return redirect(url_for("login"))

    return """
    <html lang="uk">
    <head>
        <meta charset="utf-8">
        <title>O&O TRANS — Паливо</title>
    </head>
    <body style="font-family:Arial;padding:30px">
        <h1>⛽ Паливо</h1>
        <p>Модуль палива готується.</p>
        <a href="/">← Назад</a>
    </body>
    </html>
    """


@app.route("/gps")
def gps():
    if not logged_in():
        return redirect(url_for("login"))

    return """
    <html lang="uk">
    <head>
        <meta charset="utf-8">
        <title>O&O TRANS — GPS</title>
    </head>
    <body style="font-family:Arial;padding:30px">
        <h1>📍 GPS</h1>
        <p>Модуль GPS готується.</p>
        <a href="/">← Назад</a>
    </body>
    </html>
    """


@app.route("/tachograph-test")
def tachograph_test():
    if not logged_in():
        return redirect(url_for("login"))

    stream_headers = {
        "Authorization": f"Token {NAVIREC_TOKEN}",
        "Accept": "application/x-ndjson; version=1.52.1",
    }

    try:
        response = requests.get(
            f"{NAVIREC_API}/streams/driver_states/",
            headers=stream_headers,
            params={"account": ACCOUNT_ID},
            stream=True,
            timeout=(10, 20),
        )

        lines = []

        for line in response.iter_lines(decode_unicode=True):
            if line:
                lines.append(line)

            if len(lines) >= 10:
                break

        parsed = []

        for line in lines:
            try:
                parsed.append(json.loads(line))
            except Exception:
                parsed.append(line)

        return f"""
        <!doctype html>
        <html lang="uk">
        <head>
            <meta charset="utf-8">
            <title>O&O TRANS — Tachograph Test</title>
            <style>
                body {{
                    font-family:Arial;
                    background:#f3f4f6;
                    padding:30px;
                }}
                .box {{
                    background:white;
                    padding:20px;
                    border-radius:12px;
                    margin-bottom:20px;
                }}
                pre {{
                    white-space:pre-wrap;
                    word-break:break-word;
                    background:#111827;
                    color:#e5e7eb;
                    padding:20px;
                    border-radius:10px;
                }}
                .ok {{
                    color:green;
                    font-weight:bold;
                }}
            </style>
        </head>
        <body>

            <h1>⏱ Tachograph Stream Test</h1>

            <div class="box">
                <p><b>HTTP status:</b> {response.status_code}</p>
                <p><b>Content-Type:</b> {response.headers.get("Content-Type")}</p>
                <p><b>Accept:</b> application/x-ndjson; version=1.52.1</p>
                <p><b>Account:</b> {ACCOUNT_ID}</p>
            </div>

            <div class="box">
                <h2>Відповідь Navirec</h2>
                <pre>{json.dumps(parsed, indent=2, ensure_ascii=False)}</pre>
            </div>

            <p><a href="/">← Назад</a></p>

        </body>
        </html>
        """

    except Exception as e:
        return f"""
        <html lang="uk">
        <head>
            <meta charset="utf-8">
            <title>Tachograph Error</title>
        </head>
        <body style="font-family:Arial;padding:30px">
            <h1>❌ Помилка</h1>
            <pre>{e}</pre>
            <a href="/">← Назад</a>
        </body>
        </html>
        """


@app.route("/tachograph-options")
def tachograph_options():
    if not logged_in():
        return redirect(url_for("login"))

    try:
        response = requests.options(
            f"{NAVIREC_API}/streams/driver_states/",
            headers={
                "Authorization": f"Token {NAVIREC_TOKEN}",
                "Accept": "application/x-ndjson; version=1.52.1",
            },
            timeout=20,
        )

        return f"""
        <html lang="uk">
        <head>
            <meta charset="utf-8">
            <title>Navirec OPTIONS</title>
        </head>
        <body style="font-family:Arial;padding:30px">

            <h1>🔧 Navirec OPTIONS</h1>

            <p><b>Status:</b> {response.status_code}</p>
            <p><b>Content-Type:</b> {response.headers.get("Content-Type")}</p>
            <p><b>Allow:</b> {response.headers.get("Allow")}</p>

            <h2>Headers</h2>
            <pre>{json.dumps(dict(response.headers), indent=2, ensure_ascii=False)}</pre>

            <h2>Body</h2>
            <pre>{response.text}</pre>

            <a href="/">← Назад</a>

        </body>
        </html>
        """

    except Exception as e:
        return f"<h2>Помилка</h2><pre>{e}</pre>"


@app.route("/health")
def health():
    return {
        "status": "ok",
        "service": "O&O TRANS bot"
    }


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000"))
    )
