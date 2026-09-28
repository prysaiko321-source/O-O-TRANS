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
_TYPE_LABELS = {
    "uk": {"cmr": "CMR", "lieferschein": "Lieferschein", "fuel": "Паливний чек", "other": "Інший документ"},
    "pl": {"cmr": "CMR", "lieferschein": "Lieferschein", "fuel": "Paragon paliwowy", "other": "Inny dokument"},
    "en": {"cmr": "CMR", "lieferschein": "Lieferschein", "fuel": "Fuel receipt", "other": "Other document"},
    "de": {"cmr": "CMR", "lieferschein": "Lieferschein", "fuel": "Tankbeleg", "other": "Anderes Dokument"},
}
_TYPES = set(_TYPE_LABELS["uk"])


def register_document_routes(app, page, routes_file, vehicles):
    root = os.environ.get("DOCUMENTS_DIR", "").strip() or os.path.join(os.path.dirname(routes_file), "tranviq_documents")
    index_path = os.path.join(root, "index.json")
    def driver_vehicle_id():
        assigned = str(session.get("driver_vehicle_id") or "")
        valid = {str(v["id"]) for v in vehicles}
        return assigned if assigned in valid else str(vehicles[0]["id"])
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
        if hidden_for_role(item):
            return False
        if role() == "dispatcher":
            return item.get("type") == "cmr" or item.get("uploaded_by") == "dispatcher"
        return role() == "driver" and item.get("vehicle_id") == driver_vehicle_id()

    def lang():
        value = str(session.get("language") or "uk").lower()
        return value if value in {"uk", "pl", "en", "de"} else "uk"

    def ui():
        strings = {
            "uk": {"vehicle":"Автомобіль / рейс","scan":"Сканувати документ","scan_help":"Вибери тип і зроби фото документа. Перевір перед відправленням.","kind":"Тип документа","send":"Надіслати документ","sending":"Надсилання…","saved":"Документ збережено.","fixed":" Краї виправлено.","route_docs":"Документи з рейсу","loading":"Завантаження…","open":"відкрити","download":"Завантажити","empty":"Документів немає.","delete":"Видалити","delete_all":"Видалити повністю","confirm_hide":"Прибрати цей документ тільки з твого кабінету? Копія залишиться у директора.","confirm_all":"Видалити цей документ повністю з усієї системи?","deleted":"Документ видалено.","bad_vehicle":"Вибери автомобіль.","bad_type":"Невідомий тип документа.","choose_file":"Вибери файл.","too_big":"Файл перевищує 12 МБ.","bad_pdf":"PDF має містити 1–20 сторінок.","bad_file":"Не вдалося прочитати фото або PDF.","save_failed":"Не вдалося зберегти документ."},
            "pl": {"vehicle":"Pojazd / trasa","scan":"Skanuj dokument","scan_help":"Wybierz rodzaj i zrób zdjęcie dokumentu. Sprawdź przed wysłaniem.","kind":"Rodzaj dokumentu","send":"Wyślij dokument","sending":"Wysyłanie…","saved":"Dokument zapisany.","fixed":" Krawędzie poprawione.","route_docs":"Dokumenty z trasy","loading":"Ładowanie…","open":"otwórz","download":"Pobierz","empty":"Brak dokumentów.","delete":"Usuń","delete_all":"Usuń całkowicie","confirm_hide":"Usunąć ten dokument tylko z Twojego widoku? Kopia pozostanie u dyrektora.","confirm_all":"Usunąć ten dokument całkowicie z systemu?","deleted":"Dokument usunięty.","bad_vehicle":"Wybierz pojazd.","bad_type":"Nieznany rodzaj dokumentu.","choose_file":"Wybierz plik.","too_big":"Plik przekracza 12 MB.","bad_pdf":"PDF musi mieć 1–20 stron.","bad_file":ui()["bad_file"],"save_failed":ui()["save_failed"]},
            "en": {"vehicle":"Vehicle / trip","scan":"Scan document","scan_help":"Choose the type and take a photo. Check it before sending.","kind":"Document type","send":"Send document","sending":"Sending…","saved":"Document saved.","fixed":" Edges corrected.","route_docs":"Trip documents","loading":"Loading…","open":"open","download":"Download","empty":"No documents.","delete":"Delete","delete_all":"Delete permanently","confirm_hide":"Remove this document only from your view? The director keeps a copy.","confirm_all":"Delete this document permanently from the whole system?","deleted":"Document deleted.","bad_vehicle":"Choose a vehicle.","bad_type":"Unknown document type.","choose_file":"Choose a file.","too_big":"File exceeds 12 MB.","bad_pdf":"PDF must contain 1–20 pages.","bad_file":"Could not read image or PDF.","save_failed":"Could not save document."},
            "de": {"vehicle":"Fahrzeug / Tour","scan":"Dokument scannen","scan_help":"Dokumenttyp wählen, Foto machen und vor dem Senden prüfen.","kind":"Dokumenttyp","send":"Dokument senden","sending":"Wird gesendet…","saved":"Dokument gespeichert.","fixed":" Kanten korrigiert.","route_docs":"Tour-Dokumente","loading":"Laden…","open":"öffnen","download":"Herunterladen","empty":"Keine Dokumente.","delete":"Löschen","delete_all":"Vollständig löschen","confirm_hide":"Dieses Dokument nur aus deiner Ansicht entfernen? Der Direktor behält eine Kopie.","confirm_all":"Dieses Dokument vollständig aus dem System löschen?","deleted":"Dokument gelöscht.","bad_vehicle":"Fahrzeug wählen.","bad_type":"Unbekannter Dokumenttyp.","choose_file":"Datei wählen.","too_big":"Datei ist größer als 12 MB.","bad_pdf":"PDF muss 1–20 Seiten haben.","bad_file":"Bild oder PDF konnte nicht gelesen werden.","save_failed":"Dokument konnte nicht gespeichert werden."},
        }
        return strings[lang()]

    def hidden_for_role(item):
        hidden = item.get("hidden_for") or []
        return role() in hidden and item.get("uploaded_by") == role()

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
        s = ui()
        vehicle_select = ""
        if role() == "dispatcher":
            options = "".join(
                '<option value="{}">{}</option>'.format(escape(str(v["id"])), escape(str(v.get("plate") or v.get("name") or v["id"])))
                for v in vehicles
            )
            vehicle_select = '<label>{}</label><select name="vehicle_id" required>{}</select>'.format(escape(s["vehicle"]), options)
        type_labels = _TYPE_LABELS[lang()]
        options = "".join('<option value="{}">{}</option>'.format(k, escape(type_labels[k])) for k in ("cmr","lieferschein","fuel","other"))
        upload = """
        <div class="card"><h2>__SCAN__</h2><p>__HELP__</p>
        <form id="scanForm">__VEHICLE_SELECT__<label>__KIND__</label><select name="type">__OPTIONS__</select>
        <input name="file" type="file" accept="image/*,.pdf,application/pdf" capture="environment" required>
        <img id="scanPreview" alt="" style="display:none;max-width:100%;max-height:55vh;margin:12px 0">
        <button type="submit">__SEND__</button><p id="scanStatus" role="status"></p></form></div>
        <script>
        const form=document.getElementById('scanForm'),input=form.elements.file,preview=document.getElementById('scanPreview');
        input.onchange=()=>{if(input.files[0]&&input.files[0].type.startsWith('image/')){preview.src=URL.createObjectURL(input.files[0]);preview.style.display='block'}else preview.style.display='none'};
        form.onsubmit=async(e)=>{e.preventDefault();let status=document.getElementById('scanStatus');status.textContent=__SENDING__;
        try{let r=await fetch('/api/documents',{method:'POST',body:new FormData(form)}),d=await r.json();if(!r.ok)throw Error(d.error||'Error');
        status.textContent=__SAVED__+(d.document.cropped?__FIXED__:'');form.reset();preview.style.display='none';loadDocs()}catch(err){status.textContent=err.message}};
        </script>
        """.replace("__SCAN__", escape(s["scan"])).replace("__HELP__", escape(s["scan_help"])).replace("__VEHICLE_SELECT__", vehicle_select).replace("__KIND__", escape(s["kind"])).replace("__OPTIONS__", options).replace("__SEND__", escape(s["send"])).replace("__SENDING__", json.dumps(s["sending"])).replace("__SAVED__", json.dumps(s["saved"])).replace("__FIXED__", json.dumps(s["fixed"])) if role() in {"driver", "dispatcher"} else ""

        js_strings = json.dumps({k:s[k] for k in ("open","download","empty","delete","delete_all","confirm_hide","confirm_all")}, ensure_ascii=False)
        body = upload + '<div class="card"><h2>{} <span id="docCount"></span></h2><div id="docList">{}</div></div>'.format(escape(s["route_docs"]), escape(s["loading"])) + """
        <script>
        const DOC_UI=__DOC_UI__;
        async function deleteDoc(x){
          const full=x.can_delete_all===true;
          if(!confirm(full?DOC_UI.confirm_all:DOC_UI.confirm_hide))return;
          let r=await fetch('/api/documents/'+encodeURIComponent(x.id),{method:'DELETE'});
          let d=await r.json().catch(()=>({}));
          if(!r.ok){alert(d.error||'Error');return}
          loadDocs();
        }
        async function loadDocs(){
          let r=await fetch('/api/documents',{cache:'no-store'});if(!r.ok)return;
          let d=await r.json(),box=document.getElementById('docList');
          document.getElementById('docCount').textContent='('+d.documents.length+')';box.replaceChildren();
          for(let x of d.documents){
            let p=document.createElement('p'),a=document.createElement('a');
            a.href='/api/documents/'+encodeURIComponent(x.id)+'/file';
            a.textContent=x.label+' · '+x.vehicle_label+' · '+new Date(x.created_at).toLocaleString()+' · '+DOC_UI.open;
            a.target='_blank';p.appendChild(a);
            let download=document.createElement('a');download.href=a.href+'?download=1';download.textContent=' ⬇ '+DOC_UI.download;
            download.style.marginLeft='14px';download.style.fontWeight='bold';p.appendChild(download);
            if(x.can_delete){
              let del=document.createElement('button');del.type='button';del.textContent=' 🗑 '+(x.can_delete_all?DOC_UI.delete_all:DOC_UI.delete);
              del.style.marginLeft='14px';del.style.background='#c92a2a';del.onclick=()=>deleteDoc(x);p.appendChild(del);
            }
            box.appendChild(p)
          }
          if(!d.documents.length)box.textContent=DOC_UI.empty;
        }
        loadDocs();setInterval(loadDocs,30000);
        </script>
        """.replace("__DOC_UI__", js_strings)
        return page(s["route_docs"], body, "documents")

    @app.route("/api/documents", methods=["GET", "POST"])
    def trip_documents_api():
        if role() not in {"driver", "dispatcher", "director"}:
            abort(403)
        if request.method == "GET":
            with _LOCK:
                items = [dict(x) for x in read_index() if visible(x)]
            docs = []
            for x in items[-300:][::-1]:
                public = {k: v for k, v in x.items() if k != "filename"}
                public["label"] = _TYPE_LABELS[lang()].get(x.get("type"), x.get("label") or x.get("type"))
                public["can_delete_all"] = role() == "director"
                public["can_delete"] = role() == "director" or x.get("uploaded_by") == role()
                docs.append(public)
            return jsonify({"documents": docs})
        if role() not in {"driver", "dispatcher"}:
            abort(403)
        vehicle_id = driver_vehicle_id() if role() == "driver" else str(request.form.get("vehicle_id") or "")
        vehicle = next((v for v in vehicles if str(v["id"]) == vehicle_id), None)
        if vehicle is None:
            return jsonify({"error": ui()["bad_vehicle"]}), 400
        kind = request.form.get("type", "")
        if kind not in _TYPES:
            return jsonify({"error": ui()["bad_type"]}), 400
        upload = request.files.get("file")
        if not upload:
            return jsonify({"error": ui()["choose_file"]}), 400
        raw = upload.stream.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            return jsonify({"error": ui()["too_big"]}), 413
        is_pdf = raw.startswith(b"%PDF-")
        try:
            if is_pdf:
                from pypdf import PdfReader
                reader = PdfReader(io.BytesIO(raw))
                if not 1 <= len(reader.pages) <= 20:
                    raise ValueError(ui()["bad_pdf"])
                result, cropped, suffix, mime = raw, False, ".pdf", "application/pdf"
            else:
                result, cropped = scan_image(raw)
                suffix, mime = ".jpg", "image/jpeg"
        except Exception as exc:
            return jsonify({"error": str(exc) if isinstance(exc, ValueError) else ui()["bad_file"]}), 400
        item_id = uuid.uuid4().hex
        filename = item_id + suffix
        item = {"id": item_id, "type": kind, "label": _TYPE_LABELS[lang()][kind], "vehicle_id": vehicle_id,
                "vehicle_label": vehicle.get("plate") or vehicle.get("name"),
                "uploaded_by": role(), "trip": current_trip(vehicle_id), "created_at": datetime.now(timezone.utc).isoformat(),
                "cropped": cropped, "mime": mime, "filename": filename}
        try:
            with _LOCK:
                store_document(item, result)
        except Exception as exc:
            app.logger.exception("Document storage failed")
            return jsonify({"error": str(exc) if isinstance(exc, RuntimeError) else ui()["save_failed"]}), 503
        return jsonify({"ok": True, "document": {k: v for k, v in item.items() if k != "filename"}}), 201

    @app.route("/api/documents/<item_id>", methods=["DELETE"])
    def trip_document_delete(item_id):
        if role() not in {"driver", "dispatcher", "director"}:
            abort(403)
        with _LOCK:
            items = read_index()
            item = next((x for x in items if x.get("id") == item_id), None)
            if item is None:
                abort(404)
            if role() == "director":
                if use_database:
                    ensure_schema()
                    with psycopg.connect(database_url, connect_timeout=8) as connection:
                        connection.execute("DELETE FROM tranviq_trip_documents WHERE id = %s", (item_id,))
                else:
                    path = os.path.join(root, item.get("filename", ""))
                    items = [x for x in items if x.get("id") != item_id]
                    save_index(items)
                    try:
                        os.unlink(path)
                    except FileNotFoundError:
                        pass
                return jsonify({"ok": True, "deleted_everywhere": True})
            if item.get("uploaded_by") != role():
                abort(403)
            hidden = list(item.get("hidden_for") or [])
            if role() not in hidden:
                hidden.append(role())
            item["hidden_for"] = hidden
            if use_database:
                ensure_schema()
                with psycopg.connect(database_url, connect_timeout=8) as connection:
                    connection.execute(
                        "UPDATE tranviq_trip_documents SET metadata = %s::jsonb WHERE id = %s",
                        (json.dumps(item, ensure_ascii=False), item_id),
                    )
            else:
                save_index(items)
            return jsonify({"ok": True, "deleted_everywhere": False})

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
