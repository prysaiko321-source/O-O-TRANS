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
        cleaned = Image.fromarray(array).convert("RGB")
        cleaned = ImageOps.autocontrast(cleaned, cutoff=1)
        output = io.BytesIO()
        cleaned.save(output, "JPEG", quality=90, optimize=True)
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
        upload = r"""
        <div class="card"><h2>Skanuj dokument</h2>
        <p>Skieruj kamerę na dokument. Gdy wszystkie 4 krawędzie są widoczne i obraz jest stabilny, skan wykona się automatycznie.</p>
        <form id="scanForm">__VEHICLE_SELECT__
        <label>Rodzaj dokumentu</label><select name="type"><option value="cmr">CMR</option><option value="lieferschein">Lieferschein</option><option value="fuel">Paragon paliwowy</option><option value="other">Inny dokument</option></select>
        <div style="display:grid;gap:10px;margin-top:12px">
          <button type="button" id="openScanner" style="font-size:18px;padding:14px">📷 Uruchom skaner</button>
          <label style="font-weight:800">albo wybierz gotowy plik</label>
          <input name="file" id="fallbackFile" type="file" accept="image/*,.pdf,application/pdf">
        </div>
        <input type="hidden" name="scan_data" id="scanData">
        <img id="scanPreview" alt="Podgląd" style="display:none;max-width:100%;max-height:55vh;margin:12px 0;border-radius:12px">
        <div id="scanReviewActions" style="display:none;grid-template-columns:1fr 1fr;gap:10px;margin:12px 0">
          <button type="button" id="retakeScan" style="padding:14px;background:#c92a2a;color:#fff;font-weight:900">🗑️ Usuń / zrób ponownie</button>
          <button type="submit" id="sendScan" style="padding:14px;background:#2b8a3e;color:#fff;font-weight:900">✅ Zapisz dokument</button>
        </div>
        <button type="submit" id="sendScanFallback" disabled style="display:none">Wyślij dokument</button><p id="scanStatus" role="status"></p></form></div>

        <div id="scannerModal" style="display:none;position:fixed;inset:0;background:#05080b;z-index:99999;color:white">
          <video id="scannerVideo" playsinline muted style="width:100%;height:100%;object-fit:cover"></video>
          <canvas id="scannerCanvas" style="display:none"></canvas>
          <canvas id="scannerOverlay" style="position:absolute;inset:0;width:100%;height:100%;pointer-events:none"></canvas>
          <div style="position:absolute;left:12px;right:12px;top:12px;display:flex;justify-content:space-between;gap:10px">
            <button type="button" id="closeScanner" style="padding:12px 16px">✕</button>
            <div id="scannerHint" style="background:rgba(0,0,0,.62);padding:10px 14px;border-radius:12px;font-weight:800">Szukam dokumentu…</div>
          </div>
          <div style="position:absolute;left:12px;right:12px;bottom:18px;display:grid;grid-template-columns:1fr 1fr;gap:10px">
            <button type="button" id="manualCapture" style="padding:15px;font-size:17px">📷 Zrób ręcznie</button>
            <button type="button" id="toggleAuto" style="padding:15px;font-size:17px">AUTO: WŁ.</button>
          </div>
        </div>
        <script>
        (()=>{
          const form=document.getElementById('scanForm'), fallback=document.getElementById('fallbackFile'), preview=document.getElementById('scanPreview'), status=document.getElementById('scanStatus'), send=document.getElementById('sendScan'), review=document.getElementById('scanReviewActions');
          const modal=document.getElementById('scannerModal'), video=document.getElementById('scannerVideo'), canvas=document.getElementById('scannerCanvas'), overlay=document.getElementById('scannerOverlay'), hint=document.getElementById('scannerHint');
          let stream=null, timer=null, auto=true, stable=0, stableSince=0, lastBox=null, capturedBlob=null, captureBox=null;
          const octx=overlay.getContext('2d');
          function ready(){const ok=!!(capturedBlob || (fallback.files&&fallback.files[0]));send.disabled=!ok;review.style.display=ok?'grid':'none';}
          fallback.onchange=()=>{capturedBlob=null;captureBox=null;if(fallback.files[0]&&fallback.files[0].type.startsWith('image/')){preview.src=URL.createObjectURL(fallback.files[0]);preview.style.display='block'}else preview.style.display='none';ready()};
          async function open(){
            try{stream=await navigator.mediaDevices.getUserMedia({video:{facingMode:{ideal:'environment'},width:{ideal:1920},height:{ideal:1080}},audio:false});video.srcObject=stream;await video.play();modal.style.display='block';stable=0;stableSince=0;lastBox=null;captureBox=null;loop();}
            catch(e){status.textContent='Nie udało się uruchomić kamery. Sprawdź uprawnienia przeglądarki.';}
          }
          function close(){if(timer)cancelAnimationFrame(timer);timer=null;if(stream){stream.getTracks().forEach(t=>t.stop());stream=null}modal.style.display='none';}
          function boxDistance(a,b){if(!a||!b)return 999;return Math.abs(a.x-b.x)+Math.abs(a.y-b.y)+Math.abs(a.w-b.w)+Math.abs(a.h-b.h)}
          function detect(){
            if(!video.videoWidth)return null;
            const maxW=420, scale=Math.min(1,maxW/video.videoWidth), w=Math.round(video.videoWidth*scale), h=Math.round(video.videoHeight*scale);
            canvas.width=w;canvas.height=h;const c=canvas.getContext('2d',{willReadFrequently:true});c.drawImage(video,0,0,w,h);const im=c.getImageData(0,0,w,h), d=im.data;
            const gray=new Uint8Array(w*h);for(let i=0,j=0;i<d.length;i+=4,j++)gray[j]=(d[i]*77+d[i+1]*150+d[i+2]*29)>>8;
            let minX=w,minY=h,maxX=0,maxY=0,count=0;const step=2;
            for(let y=2;y<h-2;y+=step)for(let x=2;x<w-2;x+=step){let i=y*w+x;let gx=Math.abs(gray[i+1]-gray[i-1]),gy=Math.abs(gray[i+w]-gray[i-w]);if(gx+gy>70){minX=Math.min(minX,x);maxX=Math.max(maxX,x);minY=Math.min(minY,y);maxY=Math.max(maxY,y);count++;}}
            if(count<160)return null;
            let bw=maxX-minX,bh=maxY-minY,area=bw*bh,ratio=area/(w*h),aspect=bw/bh;
            const mx=minX/w,my=minY/h,mr=(w-maxX)/w,mb=(h-maxY)/h;
            if(ratio<.24||ratio>.82||bw<w*.38||bh<h*.38)return null;
            if(mx<.035||my<.035||mr<.035||mb<.035)return null;
            if(aspect<.42||aspect>2.35)return null;
            return {x:minX/w,y:minY/h,w:bw/w,h:bh/h};
          }
          function draw(b){overlay.width=innerWidth;overlay.height=innerHeight;octx.clearRect(0,0,overlay.width,overlay.height);if(!b)return;let vw=video.videoWidth,vh=video.videoHeight,sw=innerWidth,sh=innerHeight,s=Math.max(sw/vw,sh/vh),rw=vw*s,rh=vh*s,ox=(sw-rw)/2,oy=(sh-rh)/2;octx.strokeStyle=stableSince&&performance.now()-stableSince>350?'#40c057':'#ffd43b';octx.lineWidth=5;octx.strokeRect(ox+b.x*rw,oy+b.y*rh,b.w*rw,b.h*rh);}
          function loop(){let b=detect();draw(b);if(b){let dist=boxDistance(b,lastBox);captureBox=b;if(lastBox&&dist<.055){if(!stableSince)stableSince=performance.now();stable=Math.min(30,stable+1)}else{stable=0;stableSince=0}lastBox=b;let held=stableSince?performance.now()-stableSince:0;hint.textContent=held>500?'Cały dokument złapany — skanuję…':'Widzę cały dokument — przytrzymaj chwilę';if(auto&&held>1050&&stable>=6){capture();return}}else{stable=0;stableSince=0;lastBox=null;captureBox=null;hint.textContent='Pokaż CAŁY dokument — wszystkie 4 krawędzie';}timer=requestAnimationFrame(loop)}
          async function capture(){
            if(!video.videoWidth)return; if(timer)cancelAnimationFrame(timer);timer=null;
            const full=document.createElement('canvas');const vw=video.videoWidth,vh=video.videoHeight;let sx=0,sy=0,sw=vw,sh=vh;if(captureBox){const pad=.035;sx=Math.max(0,(captureBox.x-pad)*vw);sy=Math.max(0,(captureBox.y-pad)*vh);sw=Math.min(vw-sx,(captureBox.w+pad*2)*vw);sh=Math.min(vh-sy,(captureBox.h+pad*2)*vh);}full.width=Math.max(1,Math.round(sw));full.height=Math.max(1,Math.round(sh));full.getContext('2d').drawImage(video,sx,sy,sw,sh,0,0,full.width,full.height);hint.textContent='Skanuję…';
            capturedBlob=await new Promise(r=>full.toBlob(r,'image/jpeg',.95)); if(!capturedBlob){loop();return}
            preview.src=URL.createObjectURL(capturedBlob);preview.style.display='block';fallback.value='';ready();close();status.textContent='Skan gotowy. Sprawdź podgląd i wyślij.';
          }
          document.getElementById('openScanner').onclick=open;document.getElementById('closeScanner').onclick=close;document.getElementById('manualCapture').onclick=capture;document.getElementById('retakeScan').onclick=()=>{capturedBlob=null;captureBox=null;fallback.value='';preview.removeAttribute('src');preview.style.display='none';status.textContent='';ready();open();};
          document.getElementById('toggleAuto').onclick=function(){auto=!auto;this.textContent='AUTO: '+(auto?'WŁ.':'WYŁ.')};
          form.onsubmit=async(e)=>{e.preventDefault();status.textContent='Wysyłanie…';try{let fd=new FormData(form);if(capturedBlob){fd.delete('file');fd.append('file',capturedBlob,'scan.jpg')}let r=await fetch('/api/documents',{method:'POST',body:fd}),d=await r.json();if(!r.ok)throw Error(d.error||'Błąd');status.textContent='Dokument zapisany.'+(d.document.cropped?' Krawędzie poprawione.':'');form.reset();capturedBlob=null;captureBox=null;preview.removeAttribute('src');preview.style.display='none';ready();loadDocs()}catch(err){status.textContent=err.message}};
        })();
        </script>
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
