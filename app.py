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


def logged_in():
    return session.get("logged_in") is True


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
        <title>O&O TRANS</title>
        <style>
            body {{
                font-family:Arial;
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
            }}
            button {{
                width:100%;
                padding:12px;
                background:#2563eb;
                color:white;
                border:0;
                border-radius:8px;
            }}
            .error {{
                color:#f87171;
            }}
        </style>
    </head>
    <body>
        <div class="box">
            <h2>O&O TRANS</h2>

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
                padding:30px;
            }
            .box {
                background:white;
                padding:20px;
                margin:15px 0;
                border-radius:12px;
            }
            a {
                text-decoration:none;
                color:#2563eb;
            }
        </style>
    </head>
    <body>

        <h1>O&O TRANS</h1>

        <div class="box">
            <h2>🚚 Navirec</h2>
            <p>
                <a href="/tachograph-test">
                    ⏱ Tachograph Stream Test
                </a>
            </p>
        </div>

        <div class="box">
            <a href="/logout">🚪 Вийти</a>
        </div>

    </body>
    </html>
    """


@app.route("/tachograph-test")
def tachograph_test():

    if not logged_in():
        return redirect(url_for("login"))

    headers = {
        "Authorization": f"Token {NAVIREC_TOKEN}",
        "Accept": "application/x-ndjson; version=1.52.1",
    }

    events = []

    try:
        response = requests.get(
            f"{NAVIREC_API}/streams/driver_states/",
            headers=headers,
            params={
                "account": ACCOUNT_ID
            },
            stream=True,
            timeout=(10, 20),
        )

        for line in response.iter_lines(decode_unicode=True):

            if not line:
                continue

            try:
                data = json.loads(line)
            except Exception:
                data = line

            events.append(data)

            if len(events) >= 20:
                break

        return f"""
        <!doctype html>
        <html lang="uk">
        <head>
            <meta charset="utf-8">
            <title>O&O TRANS — Driver Stream</title>

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
                    background:#111827;
                    color:#e5e7eb;
                    padding:20px;
                    border-radius:10px;
                    white-space:pre-wrap;
                    word-break:break-word;
                }}

                .ok {{
                    color:green;
                    font-weight:bold;
                }}
            </style>
        </head>

        <body>

            <h1>⏱ Driver States Stream</h1>

            <div class="box">

                <p>
                    <b>HTTP:</b>
                    <span class="ok">{response.status_code}</span>
                </p>

                <p>
                    <b>Content-Type:</b>
                    {response.headers.get("Content-Type")}
                </p>

                <p>
                    <b>Account:</b>
                    {ACCOUNT_ID}
                </p>

                <p>
                    <b>Отримано подій:</b>
                    {len(events)}
                </p>

            </div>

            <div class="box">

                <h2>Події Navirec</h2>

                <pre>{json.dumps(
                    events,
                    indent=2,
                    ensure_ascii=False
                )}</pre>

            </div>

            <p>
                <a href="/">← Назад</a>
            </p>

        </body>
        </html>
        """

    except Exception as e:

        return f"""
        <html lang="uk">
        <head>
            <meta charset="utf-8">
            <title>Navirec Error</title>
        </head>

        <body style="font-family:Arial;padding:30px">

            <h1>❌ Помилка</h1>

            <pre>{e}</pre>

            <a href="/">← Назад</a>

        </body>
        </html>
        """


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
