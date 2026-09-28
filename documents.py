"""Role-scoped trip document upload and inbox."""
import io
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from html import escape

from flask import abort, jsonify, request, send_file, session
from PIL import Image, ImageOps
import cv2
import numpy as np

try:
    import psycopg
except ImportError:
    psycopg = None

_LOCK = threading.RLock()
_MAX_BYTES = 12 * 1024 * 1024
_TYPES = {"cmr": "CMR", "lieferschein": "Lieferschein", "fuel": "Paragon paliwowy", "other": "Inny dokument"}


def register_document_routes(app, page, routes_file, vehicles):
    root = os.environ.get("DOCUMENTS_DIR", "").strip() or os.path.join(os.path.dirname(routes_file), "tranviq_documents")
    index_path = os.path.join(root, "index.json")
    driver_vehicle = str(vehicles[0]["id"])
    database_url = os.environ.get("DATABASE_URL", "").strip()
    use_database = bool(database_url and psycopg)
    durable_files = bool(os.environ.get("DOCUMENTS_DIR", "").strip() or os.path.realpath(root).startswith("/var/data/"))
    schema_ready = False

    def ensure_schema():
        nonlocal schema_ready
        if not use_database or schema_ready:
            return
        with _LOCK:
            if schema_ready:
                return
            with psycopg.connect(database_url, connect_timeout=8) as connection:
                connection.execute("""CREATE TABLE IF NOT EXISTS tranviq_trip_documents (
                    id text PRIMARY KEY, metadata jsonb NOT NULL, content bytea NOT NULL
                )""")
            schema_ready = True

    def store_document(item, content):
        if use_database:
            ensure_schema()
            with psycopg.connect(database_url, connect_timeout=8) as connection:
                connection.execute(
                    "INSERT INTO tranviq_trip_documents (id, metadata, content) VALUES (%s, %s::jsonb, %s)",
                    (item["id"], json.dumps(item, ensure_ascii=False), content),
                )
            return
        if not durable_files:
            raise RuntimeError("Brak trwałego miejsca na dokumenty. Skontaktuj się z administratorem.")
        os.makedirs(root, exist_ok=True)
        path = os.path.join(root, item["filename"])
        with open(path, "xb") as handle:
            handle.write(content)
        items = read_index()
        items.append(item)
        try:
            save_index(items)
        except Exception:
            os.unlink(path)
            raise

    def file_content(item):
        if use_database:
            ensure_schema()
            with psycopg.connect(database_url, connect_timeout=8) as connection:
                row = connection.execute("SELECT content FROM tranviq_trip_documents WHERE id = %s", (item["id"],)).fetchone()
            if row is None:
                abort(404)
            return io.BytesIO(bytes(row[0]))
        return os.path.join(root, item["filename"])

    def role():
        return session.get("role") if session.get("logged_in") else None

    def read_index():
        if use_database:
            ensure_schema()
            with psycopg.connect(database_url, connect_timeout=8) as connection:
                rows = connection.execute("SELECT metadata FROM tranviq_trip_documents ORDER BY metadata->>'created_at'").fetchall()
            return [row[0] for row in rows]
        try:
            with open(index_path, encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def save_index(items):
        os.makedirs(root, exist_ok=True)
        temporary = index_path + "." + uuid.uuid4().hex + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(items, handle, ensure_ascii=False)
        os.replace(temporary, index_path)

    def visible(item):
        if role() == "director":
            return True
        if role() == "dispatcher":
            return item.get("type") == "cmr" or item.get("uploaded_by") == "dispatcher"
        return role() == "driver" and item.get("vehicle_id") == driver_vehicle

    def current_trip(vehicle_id):
        try:
            with open(routes_file, encoding="utf-8") as handle:
                route = json.load(handle).get(vehicle_id) or {}
            return str(route.get("saved_at") or route.get("updated_at") or route.get("created_at") or "active")[:80]
        except (OSError, ValueError, AttributeError):
            return "unassigned"

    def scan_image(raw):
        image = Image.open(io.BytesIO(raw))
        image = ImageOps.exif_transpose(image).convert("RGB")
        if image.width * image.height > 36_000_000:
            image.thumbnail((6000, 6000))
        if min(image.size) < 600:
            raise ValueError("Zdjęcie jest zbyt małe. Zrób je ponownie.")
        array = np.array(image)
        gray = cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)
        if cv2.Laplacian(gray, cv2.CV_64F).var() < 35:
            raise ValueError("Zdjęcie jest niewyraźne. Zrób je ponownie.")
        reduced = cv2.resize(array, (min(1200, image.width), round(image.height * min(1200, image.width) / image.width)))
        scale = image.width / reduced.shape[1]
        mono = cv2.cvtColor(reduced, cv2.COLOR_RGB2GRAY)
        edges = cv2.Canny(cv2.GaussianBlur(mono, (5, 5), 0), 60, 180)
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        quad = None
        area = reduced.shape[0] * reduced.shape[1]
        for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:25]:
            approx = cv2.approxPolyDP(contour, .02 * cv2.arcLength(contour, True), True)
            if len(approx) == 4 and cv2.contourArea(approx) > .24 * area:
                quad = approx.reshape(4, 2).astype("float32") * scale
                break
        if quad is not None:
            points = quad[np.argsort(quad.sum(axis=1))]
            tl, br = points[0], points[-1]
            remaining = points[1:3]
            tr, bl = sorted(remaining, key=lambda p: p[1] - p[0])
            width = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
            height = int(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))
            if width > 200 and height > 200:
                target = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype="float32")
                array = cv2.warpPerspective(array, cv2.getPerspectiveTransform(np.array([tl, tr, br, bl], dtype="float32"), target), (width, height))
        output = io.BytesIO()
        Image.fromarray(array).save(output, "JPEG", quality=88, optimize=True)
        return output.getvalue(), quad is not None

    @app.route("/documents")
    def trip_documents():
        if role() not in {"driver", "dispatcher", "director"}:
            abort(403)
        vehicle_select = ""
        if role() == "dispatcher":
            options = "".join(
                '<option value="{}">{}</option>'.format(escape(str(v["id"])), escape(str(v.get("plate") or v.get("name") or v["id"])))
                for v in vehicles
            )
            vehicle_select = '<label>Pojazd / trasa</label><select name="vehicle_id" required>' + options + '</select>'
        upload = """
        <div class="card"><h2>Skanuj dokument</h2><p>Wybierz rodzaj i zrób zdjęcie dokumentu. Sprawdź podgląd przed wysłaniem.</p>
        <form id="scanForm">__VEHICLE_SELECT__<label>Rodzaj dokumentu</label><select name="type"><option value="cmr">CMR</option><option value="lieferschein">Lieferschein</option><option value="fuel">Paragon paliwowy</option><option value="other">Inny dokument</option></select>
        <input name="file" type="file" accept="image/*,.pdf,application/pdf" capture="environment" required>
        <img id="scanPreview" alt="Podgląd" style="display:none;max-width:100%;max-height:55vh;margin:12px 0">
        <button type="submit">Wyślij dokument</button><p id="scanStatus" role="status"></p></form></div>
        <script>const form=document.getElementById('scanForm'),input=form.elements.file,preview=document.getElementById('scanPreview');input.onchange=()=>{if(input.files[0]&&input.files[0].type.startsWith('image/')){preview.src=URL.createObjectURL(input.files[0]);preview.style.display='block'}else preview.style.display='none'};form.onsubmit=async(e)=>{e.preventDefault();let status=document.getElementById('scanStatus');status.textContent='Wysyłanie…';try{let r=await fetch('/api/documents',{method:'POST',body:new FormData(form)}),d=await r.json();if(!r.ok)throw Error(d.error||'Błąd');status.textContent='Dokument zapisany.'+(d.document.cropped?' Krawędzie poprawione.':'');form.reset();preview.style.display='none';loadDocs()}catch(err){status.textContent=err.message}};</script>
        """.replace("__VEHICLE_SELECT__", vehicle_select) if role() in {"driver", "dispatcher"} else ""
        body = upload + '<div class="card"><h2>Dokumenty z trasy <span id="docCount"></span></h2><div id="docList">Ładowanie…</div></div>' + '''<script>
        async function loadDocs(){let r=await fetch('/api/documents',{cache:'no-store'});if(!r.ok)return;let d=await r.json(),box=document.getElementById('docList');document.getElementById('docCount').textContent='('+d.documents.length+')';box.replaceChildren();for(let x of d.documents){let p=document.createElement('p'),a=document.createElement('a');a.href='/api/documents/'+encodeURIComponent(x.id)+'/file';a.textContent=x.label+' · '+x.vehicle_label+' · '+new Date(x.created_at).toLocaleString()+' · otwórz';a.target='_blank';p.appendChild(a);let download=document.createElement('a');download.href='/api/documents/'+encodeURIComponent(x.id)+'/file?download=1';download.textContent=' ⬇ Pobierz';download.style.marginLeft='14px';download.style.fontWeight='bold';p.appendChild(download);box.appendChild(p)}if(!d.documents.length)box.textContent='Brak dokumentów.'}loadDocs();setInterval(loadDocs,30000);</script>'''
        return page("Dokumenty", body, "documents")

    @app.route("/api/documents", methods=["GET", "POST"])
    def trip_documents_api():
        if role() not in {"driver", "dispatcher", "director"}:
            abort(403)
        if request.method == "GET":
            with _LOCK:
                items = [dict(x) for x in read_index() if visible(x)]
            return jsonify({"documents": [{k: v for k, v in x.items() if k != "filename"} for x in items[-300:][::-1]]})
        if role() not in {"driver", "dispatcher"}:
            abort(403)
        vehicle_id = driver_vehicle if role() == "driver" else str(request.form.get("vehicle_id") or "")
        vehicle = next((v for v in vehicles if str(v["id"]) == vehicle_id), None)
        if vehicle is None:
            return jsonify({"error": "Wybierz pojazd."}), 400
        kind = request.form.get("type", "")
        if kind not in _TYPES:
            return jsonify({"error": "Nieznany rodzaj dokumentu."}), 400
        upload = request.files.get("file")
        if not upload:
            return jsonify({"error": "Wybierz plik."}), 400
        raw = upload.stream.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            return jsonify({"error": "Plik przekracza 12 MB."}), 413
        is_pdf = raw.startswith(b"%PDF-")
        try:
            if is_pdf:
                from pypdf import PdfReader
                reader = PdfReader(io.BytesIO(raw))
                if not 1 <= len(reader.pages) <= 20:
                    raise ValueError("PDF musi mieć 1–20 stron.")
                result, cropped, suffix, mime = raw, False, ".pdf", "application/pdf"
            else:
                result, cropped = scan_image(raw)
                suffix, mime = ".jpg", "image/jpeg"
        except Exception as exc:
            return jsonify({"error": str(exc) if isinstance(exc, ValueError) else "Nie można odczytać zdjęcia lub PDF."}), 400
        item_id = uuid.uuid4().hex
        filename = item_id + suffix
        item = {"id": item_id, "type": kind, "label": _TYPES[kind], "vehicle_id": vehicle_id,
                "vehicle_label": vehicle.get("plate") or vehicle.get("name"),
                "uploaded_by": role(), "trip": current_trip(vehicle_id), "created_at": datetime.now(timezone.utc).isoformat(),
                "cropped": cropped, "mime": mime, "filename": filename}
        try:
            with _LOCK:
                store_document(item, result)
        except Exception as exc:
            app.logger.exception("Document storage failed")
            return jsonify({"error": str(exc) if isinstance(exc, RuntimeError) else "Nie udało się zapisać dokumentu."}), 503
        return jsonify({"ok": True, "document": {k: v for k, v in item.items() if k != "filename"}}), 201

    @app.route("/api/documents/unread")
    def trip_documents_unread():
        if role() not in {"director", "dispatcher"}:
            abort(403)
        with _LOCK:
            count = sum(visible(x) and x.get("uploaded_by") != role() and x.get("created_at", "") > session.get("documents_seen_at", "") for x in read_index())
        return jsonify({"count": count})

    @app.route("/api/documents/seen", methods=["POST"])
    def trip_documents_seen():
        if role() not in {"director", "dispatcher"}:
            abort(403)
        session["documents_seen_at"] = datetime.now(timezone.utc).isoformat()
        return jsonify({"ok": True})

    @app.route("/api/documents/<item_id>/file")
    def trip_document_file(item_id):
        if len(item_id) != 32 or any(c not in "0123456789abcdef" for c in item_id):
            abort(404)
        with _LOCK:
            item = next((x for x in read_index() if x.get("id") == item_id), None)
        if not item or not visible(item):
            abort(404)
        filename = item.get("filename", "")
        if filename not in {item_id + ".jpg", item_id + ".pdf"}:
            abort(404)
        return send_file(file_content(item), mimetype=item["mime"], as_attachment=request.args.get("download") == "1", download_name=item["label"].replace(" ", "-") + "-" + item["vehicle_label"].replace(" ", "-") + "-" + filename)
