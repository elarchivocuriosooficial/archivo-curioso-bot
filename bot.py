"""El Archivo Curioso – bot: guion -> vídeo -> publicación (bundle.social)."""
from pathlib import Path
import asyncio
import csv
import edge_tts
import io
import json
import os
import random
import re
import requests
import subprocess
import sys
import tempfile
import time

"""Busca imágenes libres: primero Openverse (CC0 / dominio público, sin atribución),
después Wikimedia Commons (guardando el crédito si la licencia lo pide)."""

UA = {"User-Agent": "ElArchivoCuriosoBot/1.0 (elarchivocurioso.oficial@gmail.com)"}


def _descargar(url, destino):
    r = requests.get(url, headers=UA, timeout=40)
    r.raise_for_status()
    if len(r.content) < 15000:
        raise ValueError("imagen demasiado pequeña")
    destino.write_bytes(r.content)


def _openverse(q, destino, usadas):
    r = requests.get(
        "https://api.openverse.org/v1/images/",
        params={"q": q, "license": "cc0,pdm", "page_size": 20, "mature": "false"},
        headers=UA, timeout=12,
    )
    r.raise_for_status()
    res = [x for x in r.json().get("results", [])
           if (x.get("width") or 0) >= 700 and x.get("url") not in usadas
           and (x.get("filetype") or "jpg").lower() in ("jpg", "jpeg", "png", "webp")]
    res.sort(key=lambda x: (x.get("height") or 0) / max(x.get("width") or 1, 1), reverse=True)
    for x in res[:6]:
        try:
            _descargar(x["url"], destino)
            usadas.add(x["url"])
            return {"credito": None}
        except Exception:
            continue
    return None


def _wikimedia(q, destino, usadas):
    r = requests.get(
        "https://commons.wikimedia.org/w/api.php",
        params={
            "action": "query", "format": "json", "generator": "search",
            "gsrsearch": f"filetype:bitmap {q}", "gsrnamespace": 6, "gsrlimit": 15,
            "prop": "imageinfo", "iiprop": "url|size|extmetadata", "iiurlwidth": 1400,
        },
        headers=UA, timeout=30,
    )
    r.raise_for_status()
    pages = sorted((r.json().get("query", {}).get("pages", {})).values(), key=lambda p: p.get("index", 99))
    for p in pages:
        ii = (p.get("imageinfo") or [{}])[0]
        if (ii.get("width") or 0) < 700:
            continue
        url = ii.get("thumburl") or ii.get("url")
        if not url or url in usadas:
            continue
        meta = ii.get("extmetadata", {})
        lic = meta.get("LicenseShortName", {}).get("value", "")
        if not re.search(r"public domain|cc0|cc by|pd", lic, re.I):
            continue
        autor = re.sub("<[^>]+>", "", meta.get("Artist", {}).get("value", "")).strip()[:60]
        try:
            _descargar(url, destino)
        except Exception:
            continue
        usadas.add(url)
        necesita = not re.search(r"public domain|cc0|^pd", lic, re.I)
        return {"credito": f"{autor or 'Autor desconocido'} ({lic}, Wikimedia Commons)" if necesita else None}
    return None


PIXABAY_KEY = os.environ.get("PIXABAY_KEY", "")


def buscar_clips(q, carpeta, usados, n=3):
    """Descarga hasta n clips de vídeo de Pixabay para la búsqueda q. Devuelve rutas."""
    if not PIXABAY_KEY:
        return []
    rutas = []
    for consulta in (q, " ".join(q.split()[:2]), q.split()[0]):
        try:
            r = requests.get("https://pixabay.com/api/videos/",
                             params={"key": PIXABAY_KEY, "q": consulta[:100], "per_page": 20,
                                     "safesearch": "true", "order": "relevant"},
                             headers=UA, timeout=20)
            r.raise_for_status()
            hits = r.json().get("hits", [])
        except Exception as e:
            print(f"[clips] pixabay '{consulta}': {e}")
            continue
        for h in hits:
            if h["id"] in usados or h.get("duration", 0) < 3:
                continue
            v = h.get("videos", {})
            elegido = next((v[k] for k in ("large", "medium", "small")
                            if v.get(k, {}).get("url") and 700 <= v[k].get("height", 0) <= 2200), None)
            if not elegido:
                continue
            destino = Path(carpeta) / f"clip{h['id']}.mp4"
            try:
                rr = requests.get(elegido["url"], headers=UA, timeout=90)
                rr.raise_for_status()
                destino.write_bytes(rr.content)
            except Exception:
                continue
            usados.add(h["id"])
            rutas.append(destino)
            if len(rutas) >= n:
                return rutas
        if rutas:
            return rutas
    return rutas


def buscar_imagen(q, destino, usadas):
    for fuente in (_wikimedia, _openverse):
        for consulta in (q, " ".join(q.split()[:2])):
            try:
                info = fuente(consulta, destino, usadas)
                if info is not None:
                    return info
            except Exception as e:
                print(f"[imagen] {fuente.__name__} '{consulta}': {e}")
    return None

"""Genera un vídeo vertical 9:16 a partir de un guion.

Guion (dict):
{
  "id": "2026-11-14-faraon",
  "titulo": "...",
  "escenas": [ {"texto": "Frase narrada", "imagen": "búsqueda en inglés"} , ...],
  "caption": "Texto + #hashtags"
}
"""


W, H, FPS = 1080, 1920, 30
VOZ = os.environ.get("VOZ", "es-ES-AlvaroNeural")
VELOCIDAD = os.environ.get("VELOCIDAD", "+10%")
FUENTE = os.environ.get("FUENTE", "DejaVu Sans")


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Fallo: {' '.join(cmd[:6])}...\n{r.stderr[-2000:]}")
    return r.stdout


def duracion(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", str(path)])
    return float(out.strip())


async def tts(texto, salida):
    """Devuelve lista de palabras [(inicio_s, fin_s, palabra)]."""
    com = edge_tts.Communicate(texto, VOZ, rate=VELOCIDAD, boundary="WordBoundary")
    palabras = []
    with open(salida, "wb") as f:
        async for chunk in com.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                ini = chunk["offset"] / 1e7
                fin = ini + chunk["duration"] / 1e7
                palabras.append((ini, fin, chunk["text"]))
    return palabras


def ts_ass(s):
    h = int(s // 3600)
    m = int(s % 3600 // 60)
    sec = s % 60
    return f"{h}:{m:02d}:{sec:05.2f}"


def crear_ass(palabras, total, gancho, salida):
    """Subtítulos grandes al centro, de 3 en 3 palabras, la actual en amarillo."""
    cab = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{FUENTE},92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,3,5,70,70,0,1
Style: Gancho,{FUENTE},74,&H00FFFFFF,&H00FFFFFF,&H00000000,&HB4000000,-1,0,0,0,100,100,0,0,3,18,0,8,60,60,230,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lineas = []
    if gancho:
        lineas.append(f"Dialogue: 1,{ts_ass(0)},{ts_ass(min(3.0, total))},Gancho,,0,0,0,,{gancho.upper()}")
    grupos, actual = [], []
    for p in palabras:
        actual.append(p)
        if len(actual) == 3 or p[2].rstrip("»\"'").endswith((".", "?", "!", ",", ":", "…")):
            grupos.append(actual)
            actual = []
    if actual:
        grupos.append(actual)
    for gi, g in enumerate(grupos):
        for wi, (ini, fin, _) in enumerate(g):
            sig = g[wi + 1][0] if wi + 1 < len(g) else (
                grupos[gi + 1][0][0] if gi + 1 < len(grupos) else total)
            partes = []
            for wj, (_, _, txt) in enumerate(g):
                t = txt.upper().replace("{", "").replace("}", "")
                partes.append("{\\c&H00E6FF&}" + t + "{\\c&HFFFFFF&}" if wj == wi else t)
            pop = "{\\fscx108\\fscy108\\t(0,90,\\fscx100\\fscy100)}"
            lineas.append(f"Dialogue: 0,{ts_ass(ini)},{ts_ass(max(sig, ini + 0.05))},Sub,,0,0,0,,{pop}{' '.join(partes)}")
    Path(salida).write_text(cab + "\n".join(lineas) + "\n", encoding="utf-8")


def clip_escena(imagen, dur, salida, idx):
    frames = int(dur * FPS) + 2
    zoom_in = idx % 2 == 0
    z = f"min(1+0.0009*on,1.25)" if zoom_in else f"max(1.25-0.0009*on,1)"
    vf = (
        f"[0:v]format=rgb24,split[a][b];"
        f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:3,eq=brightness=-0.15[bg];"
        f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,scale={W*2}:{H*2},"
        f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS},"
        f"format=yuv420p"
    )
    run(["ffmpeg", "-y", "-i", str(imagen), "-filter_complex", vf,
         "-t", f"{dur:.3f}", "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "20", "-an", str(salida)])


def tramo_video(clip, dur, salida, idx):
    """Recorta un clip a 9:16, con un leve empuje de cámara, y lo deja en dur segundos."""
    total = duracion(clip)
    ini = max(0.0, min(total - dur - 0.1, total * 0.15)) if total > dur + 0.5 else 0.0
    zi = idx % 2 == 0
    z = f"(1.04+0.05*t/{dur:.2f})" if zi else f"(1.09-0.05*t/{dur:.2f})"
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
          f"scale=w='trunc({W}*{z}/2)*2':h='trunc({H}*{z}/2)*2':eval=frame,"
          f"crop={W}:{H},eq=contrast=1.06:saturation=1.12,fps={FPS},format=yuv420p")
    run(["ffmpeg", "-y", "-ss", f"{ini:.2f}", "-stream_loop", "-1", "-i", str(clip), "-t", f"{dur:.3f}",
         "-vf", vf, "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(salida)])


def fondo_liso(salida):
    color = random.choice(["0x1b1f3a", "0x2a1b3a", "0x13293d", "0x3a1b1b"])
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s={W}x{H}", "-frames:v", "1", str(salida)])


def generar(guion, carpeta_salida="salida"):
    out_dir = Path(carpeta_salida)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    creditos = []
    clips, audios, todas = [], [], []
    t0 = 0.0
    usadas = set()
    usadas_vid = set()
    for i, esc in enumerate(guion["escenas"]):
        mp3 = tmp / f"a{i}.mp3"
        palabras = asyncio.run(tts(esc["texto"], mp3))
        dur = duracion(mp3) + 0.15
        todas += [(t0 + a, t0 + b, w) for a, b, w in palabras]
        consulta = esc.get("video") or esc.get("imagen") or guion["titulo"]
        n_cortes = max(1, min(3, round(dur / 2.4)))
        fuentes = buscar_clips(consulta, tmp, usadas_vid, n_cortes)
        if fuentes:
            trozo = dur / n_cortes
            for k in range(n_cortes):
                seg = tmp / f"c{i}_{k}.mp4"
                tramo_video(fuentes[k % len(fuentes)], trozo, seg, i + k)
                clips.append(seg)
        else:
            img = tmp / f"i{i}.jpg"
            info = buscar_imagen(esc.get("imagen") or consulta, img, usadas)
            if info is None:
                fondo_liso(img)
            elif info.get("credito"):
                creditos.append(info["credito"])
            clip = tmp / f"c{i}.mp4"
            clip_escena(img, dur, clip, i)
            clips.append(clip)
        audios.append((mp3, dur))
        t0 += dur

    # Unir vídeo
    lista = tmp / "lista.txt"
    lista.write_text("".join(f"file '{c}'\n" for c in clips))
    video = tmp / "video.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lista), "-c", "copy", str(video)])

    # Unir audio (cada escena con su hueco)
    entradas, filtros = [], []
    for j, (mp3, dur) in enumerate(audios):
        entradas += ["-i", str(mp3)]
        filtros.append(f"[{j}:a]apad,atrim=0:{dur:.3f}[a{j}]")
    filtro = ";".join(filtros) + ";" + "".join(f"[a{j}]" for j in range(len(audios))) + \
        f"concat=n={len(audios)}:v=0:a=1,loudnorm=I=-14:TP=-1.5[a]"
    audio = tmp / "audio.m4a"
    run(["ffmpeg", "-y", *entradas, "-filter_complex", filtro, "-map", "[a]", "-c:a", "aac", "-b:a", "160k", str(audio)])

    ass = tmp / "subs.ass"
    crear_ass(todas, t0, guion.get("gancho", ""), ass)

    final = out_dir / f"{guion['id']}.mp4"
    barra = f"drawbox=x=0:y=0:w='iw*t/{t0:.2f}':h=14:color=0xFFE600@0.95:t=fill"
    run(["ffmpeg", "-y", "-i", str(video), "-i", str(audio), "-vf", f"ass={ass},{barra}",
         "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
         "-pix_fmt", "yuv420p", "-c:a", "copy", "-shortest", "-movflags", "+faststart", str(final)])
    return final, creditos


"""Sube el vídeo a bundle.social y crea la publicación en TikTok."""
import datetime as dt


API = "https://api.bundle.social/api/v1"


def _h():
    return {"x-api-key": os.environ["BUNDLE_API_KEY"]}


def publicaciones_restantes():
    r = requests.get(f"{API}/organization/usage/posts", headers=_h(), timeout=30)
    r.raise_for_status()
    return r.json().get("remaining", 0)


def _subir(video_path, team_id):
    with open(video_path, "rb") as f:
        r = requests.post(f"{API}/upload/", headers=_h(),
                          data={"teamId": team_id},
                          files={"file": (os.path.basename(video_path), f, "video/mp4")},
                          timeout=600)
    if r.status_code >= 300:
        raise RuntimeError(f"Subida fallida {r.status_code}: {r.text[:500]}")
    return r.json()["id"]


def publicar(video_path, caption, titulo, plataformas=("TIKTOK",)):
    team_id = os.environ["BUNDLE_TEAM_ID"]
    upload_id = _subir(video_path, team_id)
    cuando = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    data = {}
    if "TIKTOK" in plataformas:
        data["TIKTOK"] = {"type": "VIDEO", "text": caption[:2200], "uploadIds": [upload_id],
                          "privacy": "PUBLIC_TO_EVERYONE", "isAiGenerated": True}
    if "YOUTUBE" in plataformas:
        data["YOUTUBE"] = {"type": "SHORT", "text": titulo[:100], "description": caption[:4900],
                           "uploadIds": [upload_id], "privacy": "PUBLIC", "madeForKids": False}
    if "INSTAGRAM" in plataformas:
        data["INSTAGRAM"] = {"type": "REEL", "text": caption[:2200], "uploadIds": [upload_id]}
    body = {"teamId": team_id, "title": titulo[:100], "postDate": cuando, "status": "SCHEDULED",
            "socialAccountTypes": list(plataformas), "data": data}
    r = requests.post(f"{API}/post/", headers={**_h(), "Content-Type": "application/json"}, json=body, timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"Publicación fallida {r.status_code}: {r.text[:800]}")
    post = r.json()
    post_id = post.get("id")
    # Esperar a que se publique (hasta ~8 min)
    for _ in range(32):
        time.sleep(15)
        g = requests.get(f"{API}/post/{post_id}", headers=_h(), timeout=30)
        if g.ok:
            st = g.json().get("status")
            print(f"[publicar] estado: {st}")
            if st in ("POSTED", "ERROR", "FAILED"):
                if st != "POSTED":
                    raise RuntimeError(f"Publicación con error: {g.text[:800]}")
                return post_id
    print("[publicar] sigue en proceso; lo dejo programado en bundle.social")
    return post_id

"""Instagram directo (API oficial de Meta, app propia en modo desarrollo: gratis, ~100 posts/24 h)."""
IG_USER_ID = os.environ.get("IG_USER_ID", "17841446134327858")  # @elarchivocurioso.oficial


def _ig_base():
    """Token EAA... (inicio de sesión unificado/Facebook) -> graph.facebook.com/{ig_id};
    token IGAA... (inicio de sesión con Instagram) -> graph.instagram.com/me."""
    tok = os.environ.get("IG_ACCESS_TOKEN", "")
    if tok.startswith("EAA"):
        return "https://graph.facebook.com/v23.0", IG_USER_ID
    return "https://graph.instagram.com/v23.0", "me"


def ig_disponible():
    return bool(os.environ.get("IG_ACCESS_TOKEN"))


def publicar_instagram(video_path, caption, publicar_ya=True):
    tok = os.environ["IG_ACCESS_TOKEN"]
    IG_API, yo = _ig_base()
    r = requests.post(f"{IG_API}/{yo}/media", data={"media_type": "REELS", "upload_type": "resumable",
                                                  "caption": caption[:2200], "share_to_feed": "true",
                                                  "access_token": tok}, timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"IG contenedor {r.status_code}: {r.text[:500]}")
    cid = r.json()["id"]
    datos = Path(video_path).read_bytes()
    fallos = []
    for url_up in (f"https://rupload.facebook.com/ig-api-upload/v23.0/{cid}",
                   f"https://rupload.facebook.com/ig-api-upload/{cid}"):
        u = requests.post(url_up, headers={"Authorization": f"OAuth {tok}", "offset": "0",
                                           "file_size": str(len(datos)),
                                           "Content-Type": "application/octet-stream"},
                          data=datos, timeout=600)
        if u.status_code < 300:
            print(f"[instagram] subida OK ({url_up.split('ig-api-upload/')[1]})")
            break
        fallos.append(f"{u.status_code}: {u.text[:300]}")
    else:
        raise RuntimeError(f"IG subida: {' | '.join(fallos)}")
    for _ in range(40):
        time.sleep(15)
        s = requests.get(f"{IG_API}/{cid}", params={"fields": "status_code,status", "access_token": tok}, timeout=30).json()
        print(f"[instagram] estado: {s.get('status_code')}")
        if s.get("status_code") == "FINISHED":
            break
        if s.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"IG procesado con error: {s}")
    if not publicar_ya:
        print("[instagram] prueba OK: contenedor listo, NO se publica")
        return cid
    p = requests.post(f"{IG_API}/{yo}/media_publish", data={"creation_id": cid, "access_token": tok}, timeout=60)
    if p.status_code >= 300:
        raise RuntimeError(f"IG publicar {p.status_code}: {p.text[:500]}")
    return p.json().get("id")


def renovar_token_instagram():
    """Alarga la validez del token (60 días). Si cambiara, lo deja en salida/ para avisar."""
    tok = os.environ.get("IG_ACCESS_TOKEN")
    if not tok or tok.startswith("EAA"):
        return
    try:
        r = requests.get("https://graph.instagram.com/refresh_access_token",
                         params={"grant_type": "ig_refresh_token", "access_token": tok}, timeout=30)
        d = r.json()
        print(f"[instagram] token renovado, caduca en {int(d.get('expires_in', 0)) // 86400} días")
        if d.get("access_token") and d["access_token"] != tok:
            print("[instagram] AVISO: el token renovado es distinto; hay que actualizar el secreto IG_ACCESS_TOKEN")
    except Exception as e:
        print(f"[instagram] no se pudo renovar el token: {e}")


"""YouTube extra con Upload-Post (plan gratis: 10 subidas/mes)."""
UP_LIMITE_MES = int(os.environ.get("UP_LIMITE_MES", "10"))


def publicar_youtube_uploadpost(video_path, titulo, caption):
    with open(video_path, "rb") as f:
        r = requests.post("https://api.upload-post.com/api/upload",
                          headers={"Authorization": f"Apikey {os.environ['UPLOADPOST_API_KEY']}"},
                          data={"user": os.environ.get("UPLOADPOST_USER", "default"), "platform[]": "youtube",
                                "title": (titulo[:90] + " #shorts"), "description": caption[:4900]},
                          files={"video": (os.path.basename(video_path), f, "video/mp4")}, timeout=600)
    if r.status_code >= 300:
        raise RuntimeError(f"Upload-Post {r.status_code}: {r.text[:500]}")
    return "uploadpost"


def usar_bundle_hoy():
    """Reparte las 20 publicaciones gratis de bundle.social a lo largo del mes."""
    import calendar
    import datetime as dt
    hoy = dt.date.today()
    dias_restantes = calendar.monthrange(hoy.year, hoy.month)[1] - hoy.day + 1
    rest = publicaciones_restantes()
    usar = rest > 0 and (rest >= dias_restantes or rest / dias_restantes >= 0.6)
    print(f"[reparto] bundle: quedan {rest} para {dias_restantes} días -> {'sí' if usar else 'no'}")
    return usar


PROMPT_GUION = 'Eres el guionista de "El Archivo Curioso", una cuenta de TikTok en español (España) de curiosidades, historia y misterios reales.\n\nEscribe UN guion para un vídeo vertical de 35-50 segundos narrado por voz en off. Reglas:\n- Gancho brutal en la primera frase (menos de 12 palabras): una pregunta o afirmación que obligue a quedarse. Nada de "¿Sabías que...?".\n- Datos 100% reales y verificables. Nada de leyendas presentadas como hechos; si es leyenda, dilo.\n- Frases cortas, ritmo rápido, tono cercano de España (tú, no usted). Sin emojis en la narración.\n- 6 a 9 escenas. Cada escena = 1-2 frases narradas + "video": búsqueda EN INGLÉS de 1-3 palabras para un banco de VÍDEOS de stock genéricos (ej. "desert sand", "old ship ocean", "soldiers marching", "ancient ruins", "dark forest fog") + "imagen": búsqueda EN INGLÉS concreta de respaldo (ej. "Titanic ship 1912 photo"). Evita personas vivas.\n- Última escena: giro final o dato que sorprenda + llamada suave ("Sígueme para más archivos curiosos").\n- Caption: 1 frase intrigante + 4-6 hashtags (#curiosidades #historia #sabiasque #datoscuriosos + 1-2 del tema).\n- "gancho": texto corto (máx 6 palabras) que aparecerá escrito en pantalla los 3 primeros segundos.\n\nDevuelve SOLO JSON con esta forma:\n{"titulo": "...", "gancho": "...", "escenas": [{"texto": "...", "video": "...", "imagen": "..."}], "caption": "..."}\n'

"""Orquestador: coge el siguiente guion pendiente, genera el vídeo y lo publica.

Fuentes de guiones (en este orden):
  1. Hoja de Google publicada como CSV (SHEET_CSV_URL), columnas: id, estado, guion_json
  2. data/cola.json en el repo
  3. Si no hay ninguno: GitHub Models escribe uno nuevo (gratis con GITHUB_TOKEN)
"""


RAIZ = Path(__file__).resolve().parent
PUBLICADOS = RAIZ / "data" / "publicados.json"
COLA = RAIZ / "data" / "cola.json"


def cargar_publicados():
    return json.loads(PUBLICADOS.read_text(encoding="utf-8")) if PUBLICADOS.exists() else []


def guiones_hoja():
    url = os.environ.get("SHEET_CSV_URL")
    if not url:
        return []
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    r.encoding = "utf-8"
    out = []
    for fila in csv.DictReader(io.StringIO(r.text)):
        if (fila.get("estado") or "").strip().lower() not in ("", "pendiente"):
            continue
        try:
            g = json.loads(fila["guion_json"])
            g["id"] = fila.get("id") or g["id"]
            out.append(g)
        except Exception as e:
            print(f"[cola] fila inválida {fila.get('id')}: {e}")
    return out


def guiones_repo():
    return json.loads(COLA.read_text(encoding="utf-8")) if COLA.exists() else []


def guion_ia(titulos_previos):
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return None
    prompt = PROMPT_GUION
    prompt += "\n\nTemas YA publicados (no repitas):\n" + "\n".join(f"- {t}" for t in titulos_previos[-60:])
    intentos = [("https://models.github.ai/inference/chat/completions", os.environ.get("MODELO", "openai/gpt-4.1")),
                ("https://models.github.ai/inference/chat/completions", "openai/gpt-4o-mini"),
                ("https://models.inference.ai.azure.com/chat/completions", "gpt-4o-mini")]
    for i, (url, modelo) in enumerate(intentos):
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                              json={"model": modelo, "messages": [{"role": "user", "content": prompt}],
                                    "response_format": {"type": "json_object"}, "temperature": 0.9},
                              timeout=120)
            if r.status_code >= 300 or not r.text.strip():
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]!r}")
            g = json.loads(r.json()["choices"][0]["message"]["content"])
            if g.get("escenas") and g.get("titulo"):
                print(f"[ia] guion generado con {modelo}")
                return g
            raise RuntimeError("respuesta sin escenas")
        except Exception as e:
            print(f"[ia] fallo con {modelo}: {e}")
            time.sleep(10 * (i + 1))
    return None


def siguiente_guion(publicados):
    hechos = {p["id"] for p in publicados}
    for g in guiones_hoja() + guiones_repo():
        if g["id"] not in hechos:
            return g, "cola"
    g = guion_ia([p.get("titulo", "") for p in publicados])
    if g:
        import datetime as dt
        g["id"] = dt.date.today().isoformat() + "-ia-" + str(len(publicados))
        return g, "ia"
    return None, None


def main():
    modo = os.environ.get("MODO", "prueba")  # "prueba" o "publicar"
    publicados = cargar_publicados()
    guion, origen = siguiente_guion(publicados)
    if not guion:
        print("No hay guiones pendientes.")
        return
    print(f"[main] guion '{guion['titulo']}' ({origen})")
    video, creditos = generar(guion, "salida")
    caption = guion["caption"].strip()
    if creditos:
        caption += "\n\nImágenes: " + "; ".join(dict.fromkeys(creditos))
    Path("salida/caption.txt").write_text(caption, encoding="utf-8")
    Path("salida/guion.json").write_text(json.dumps(guion, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[main] vídeo listo: {video}")

    if modo != "publicar":
        print("[main] modo prueba: no se publica")
        if ig_disponible():
            try:
                publicar_instagram(str(video), caption, publicar_ya=False)
            except Exception as e:
                print(f"[instagram] prueba fallida: {e}")
        return

    import datetime as dt
    mes = dt.date.today().strftime("%Y-%m")
    plataformas = [p.strip() for p in os.environ.get("PLATAFORMAS", "TIKTOK").split(",") if p.strip()]
    hechos, errores = {}, []

    # 1) Instagram directo con la API de Meta (todos los días)
    if "INSTAGRAM" in plataformas and ig_disponible():
        renovar_token_instagram()
        try:
            hechos["INSTAGRAM"] = publicar_instagram(str(video), caption)
            plataformas.remove("INSTAGRAM")
        except Exception as e:
            # si falla (p. ej. token caducado), Instagram vuelve a ir por bundle
            print(f"[instagram] ERROR API propia, se intentará por bundle: {e}")
            errores.append(f"instagram-api: {e}")

    # 2) bundle.social (TikTok + YouTube + IG si no hay API propia) cuando toca por cupo
    if plataformas and usar_bundle_hoy():
        try:
            pid = publicar(str(video), caption, guion["titulo"], tuple(plataformas))
            for p in plataformas:
                hechos[p] = pid
        except Exception as e:
            errores.append(f"bundle: {e}")
    elif "YOUTUBE" in plataformas and os.environ.get("UPLOADPOST_API_KEY"):
        # 3) Días sin bundle: YouTube con Upload-Post mientras quede cupo
        usados = sum(1 for p in publicados if p.get("fecha", "").startswith(mes) and p.get("youtube_via") == "uploadpost")
        if usados < UP_LIMITE_MES:
            try:
                hechos["YOUTUBE"] = publicar_youtube_uploadpost(str(video), guion["titulo"], caption)
            except Exception as e:
                errores.append(f"upload-post: {e}")

    print(f"[main] publicado en: {', '.join(hechos) or 'ninguna'}")
    for e in errores:
        print(f"[main] ERROR {e}")
    if not hechos:
        if errores:
            raise RuntimeError("No se pudo publicar en ninguna red")
        print("[main] hoy no toca publicar (sin cupo); el guion queda para mañana")
        return
    publicados.append({"id": guion["id"], "titulo": guion["titulo"], "redes": list(hechos),
                       "youtube_via": "uploadpost" if hechos.get("YOUTUBE") == "uploadpost" else "bundle",
                       "fecha": dt.datetime.utcnow().isoformat(timespec="minutes"), "origen": origen})
    PUBLICADOS.parent.mkdir(exist_ok=True)
    PUBLICADOS.write_text(json.dumps(publicados, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
