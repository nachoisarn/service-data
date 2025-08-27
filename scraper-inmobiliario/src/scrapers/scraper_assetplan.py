import os
import json
import re
import time
from typing import List, Dict, Any
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ASSETPLAN_BASE = "https://www.assetplan.cl"

def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
    })
    retries = Retry(
        total=4, backoff_factor=0.8,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"]
    )
    adapter = HTTPAdapter(max_retries=retries)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s

def scrape_assetplan(url: str, output_path: str):
    """
    Scrapea el listado y fichas de Assetplan.
    Escribe JSON (y opcional JSONL) en disco manteniendo tu firma original.
    """
    session = _make_session()

    # Detectar base de paginación
    if "page=" in url:
        base_url = re.sub(r"page=\d+", "page={}", url)
    else:
        base_url = url + ("&page={}" if "?" in url else "?page={}")

    page = 1
    properties: List[Dict[str, Any]] = []

    # para evitar loops infinitos si la web repite la misma página
    seen_page_html_hash = set()

    while True:
        page_url = base_url.format(page)
        print(f"[assetplan] Scrapeando página {page} → {page_url}")
        try:
            resp = session.get(page_url, timeout=30)
        except Exception as e:
            print(f"[assetplan] Error de red en {page_url}: {e}")
            break

        if resp.status_code != 200:
            print(f"[assetplan] HTTP {resp.status_code} en {page_url}; fin.")
            break

        # Evitar repetir la misma página
        h = hash(resp.text[:10000])
        if h in seen_page_html_hash:
            print("[assetplan] Contenido repetido; fin de paginación.")
            break
        seen_page_html_hash.add(h)

        soup = BeautifulSoup(resp.text, "html.parser")

        # Cards del listado
        anuncios = soup.select("div.w-full.px-4.py-2.mt-2.bg-white")
        if not anuncios:
            print("[assetplan] No se encontraron más anuncios en el listado; fin.")
            break

        count_items = 0
        for anuncio in anuncios:
            # --- título + link (absoluto) ---
            nombre_tag = anuncio.select_one("a.block.overflow-hidden.text-lg.font-bold") or anuncio.select_one("a[href]")
            nombre = nombre_tag.get_text(strip=True) if nombre_tag else ""
            link_rel = nombre_tag.get("href") if nombre_tag and nombre_tag.has_attr("href") else ""
            link_full = urljoin(ASSETPLAN_BASE, link_rel) if link_rel else ""

            # --- dirección (y potencial comuna) ---
            direccion_tag = anuncio.select_one("span.mb-1.text-sm.text-neutral-500")
            direccion = direccion_tag.get_text(strip=True) if direccion_tag else ""

            # --- precio “global” mostrado en card ---
            precio_proyecto_txt = ""
            precio_tag = anuncio.find("p", class_="font-bold")
            if precio_tag:
                precio_proyecto_txt = precio_tag.get_text(strip=True)

            # --- detalle: comodidades + tipologías ---
            detalles: List[str] = []
            departamentos: List[Dict[str, Any]] = []

            if link_full:
                try:
                    dresp = session.get(link_full, timeout=30)
                    if dresp.status_code == 200:
                        dsoup = BeautifulSoup(dresp.text, "html.parser")

                        # Comodidades
                        comodidades = dsoup.select(
                            "div.grid.max-w-screen-lg.grid-cols-1.px-3.mx-auto.text-gray-800"
                        )
                        if comodidades:
                            for comod in comodidades[0].select("div.flex.flex-row.items-center p.text-sm"):
                                txt = comod.get_text(strip=True)
                                if txt:
                                    detalles.append(txt)

                        # Tipologías (grid + fallback)
                        grids = dsoup.find_all("div", class_="grid")
                        grid = None
                        for g in grids:
                            classes = g.get("class", [])
                            if "gap-6" in classes and "px-4" in classes:
                                grid = g
                                break
                        if grid:
                            for depto in grid.find_all("div", class_="flex"):
                                dclasses = depto.get("class", [])
                                if "border" not in dclasses:
                                    continue
                                info = depto.find(
                                    "div",
                                    class_="flex flex-col justify-between w-full p-4 text-gray-700 bg-white grow",
                                ) or depto

                                # Dormitorios
                                dormitorios = ""
                                dormitorios_div = info.find(
                                    "div", class_="flex flex-row space-x-0.5 text-sm font-semibold"
                                )
                                if dormitorios_div:
                                    ps = dormitorios_div.find_all("p")
                                    if len(ps) >= 2 and "dormitorio" in ps[1].get_text(strip=True).lower():
                                        dormitorios = ps[0].get_text(strip=True)

                                # Baños
                                banos = ""
                                banos_div = info.find("div", class_="inline-flex items-center space-x-1")
                                if banos_div:
                                    banos_p = banos_div.find_all("p")
                                    banos = banos_p[0].get_text(strip=True) if len(banos_p) > 0 else ""

                                # m² útiles
                                m2_utiles = ""
                                m2_divs = info.find_all("div", class_="inline-flex items-center space-x-2.5")
                                for m in m2_divs:
                                    if m.find("p") and ("m² útiles" in m.get_text() or "m2 útiles" in m.get_text().lower()):
                                        m2_utiles = m.find("p").get_text(strip=True)
                                        break

                                # Precio tipología
                                precio = ""
                                for p in info.find_all("p"):
                                    p_classes = p.get("class", [])
                                    if all(cls in p_classes for cls in ["text-lg", "font-semibold", "leading-7"]):
                                        precio = p.get_text(strip=True)
                                        break

                                # Unidades disponibles + link (absoluto)
                                unidades = ""
                                link_tip = ""
                                a_tag = info.find(
                                    "a",
                                    class_=(
                                        "bg-blue-600 text-white hover:bg-blue-700 "
                                        "focus:ring focus:bg-blue-700 focus:ring-blue-600 "
                                        "py-2.5 w-full rounded font-medium text-base text-center "
                                        "cursor-pointer mt-2"
                                    ),
                                )
                                if a_tag:
                                    match = re.search(r"Ver\s+([\d\+]+)\s+disponibles?", a_tag.get_text())
                                    if match:
                                        unidades = match.group(1)
                                    href = a_tag.get("href")
                                    if href:
                                        link_tip = urljoin(ASSETPLAN_BASE, href)

                                departamentos.append(
                                    {
                                        "dormitorios": dormitorios,
                                        "banos": banos,
                                        "m2_utiles": m2_utiles,
                                        "precio": precio,
                                        "unidades_disponibles": unidades,
                                        "link": link_tip or link_full,
                                    }
                                )
                    else:
                        print(f"[assetplan] detalle HTTP {dresp.status_code} en {link_full}")
                except Exception as e:
                    print(f"[assetplan] Error al acceder a ficha {link_full}: {e}")

            properties.append(
                {
                    "Operador": "assetplan",
                    "nombre": nombre,
                    "direccion": direccion,
                    "precio": precio_proyecto_txt,
                    "link": link_full,     # <— ahora absoluto
                    "comodidades": detalles,
                    "departamentos": departamentos,
                    "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            )
            count_items += 1

        if count_items == 0:
            print("[assetplan] Página sin items útiles; fin.")
            break

        page += 1
        time.sleep(0.8)  # delay amable

    # ---- Escritura en disco ----
    outdir = os.path.dirname(output_path)
    if outdir:
        os.makedirs(outdir, exist_ok=True)

    json_output = output_path.replace(".txt", ".json")
    with open(json_output, "w", encoding="utf-8") as f:
        json.dump(properties, f, ensure_ascii=False, indent=2)
    print(f"[assetplan] Scraping completado. Resultados en {json_output}")

    # (Opcional) también deja un JSONL por si lo quieres ingestar fácil
    jsonl_output = output_path.replace(".txt", ".jsonl")
    try:
        with open(jsonl_output, "w", encoding="utf-8") as jf:
            for item in properties:
                jf.write(json.dumps(item, ensure_ascii=False) + "\n")
        print(f"[assetplan] JSONL generado en {jsonl_output}")
    except Exception:
        pass
