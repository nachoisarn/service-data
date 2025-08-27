from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from bs4 import BeautifulSoup
from urllib.parse import urljoin
import time
import json
import os
import re
import random

BASE = "https://bluehome.cl"

NUM_RE = re.compile(r"(\d+(?:[.,]\d+)?)")
INT_RE = re.compile(r"(\d+)")
WHITESPACE_RE = re.compile(r"\s+")

def _ua():
    return random.choice([
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    ])

def _clean_text(s):
    return WHITESPACE_RE.sub(" ", s).strip() if s else ""

def _first_text(el, selector):
    n = el.select_one(selector)
    return _clean_text(n.get_text()) if n else ""

def _find_int(text, default=""):
    m = INT_RE.search(text or "")
    return m.group(1) if m else default

def _find_num(text, default=""):
    m = NUM_RE.search((text or "").replace(",", "."))
    return m.group(1) if m else default

def _full_url(href):
    if not href:
        return ""
    return urljoin(BASE, href)

def _build_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--window-size=1366,900")
    chrome_options.add_argument(f"user-agent={_ua()}")
    # básicos anti-det
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option("useAutomationExtension", False)
    drv = webdriver.Chrome(options=chrome_options)
    try:
        # quitar webdriver flag vía CDP (best effort)
        drv.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """
        })
    except Exception:
        pass
    return drv

def scrape_bluehome(url, output_path, wait_secs=12, detail_delay=(0.6, 1.4)):
    """
    Scrapea el listado de edificios y, por cada edificio:
      - nombre, direccion, precio
      - comodidades (desde la primera tipología que tenga detalle)
      - departamentos: [{ dormitorios, unidades_disponibles, link, baños, m2_utiles }]
    Guarda JSON en output_path y retorna la lista.
    """
    driver = _build_driver()
    edificios = []
    try:
        driver.get(url)
        # Espera explícita a que aparezca al menos un contenedor de edificio
        WebDriverWait(driver, wait_secs).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.row.p-0"))
        )

        # Si hay lazy load / infinite scroll, puedes scrollear:
        # _infinite_scroll(driver, max_scrolls=6, pause=(0.6,1.0))

        soup = BeautifulSoup(driver.page_source, "html.parser")

        # Cada “edificio” en listados Bluehome típicamente está en .row.p-0 con sub-bloques .info y .building-rooms
        for container in soup.select("div.row.p-0"):
            info = container.select_one("div.info")
            if not info:
                continue

            nombre = _first_text(info, "h4.text-2.mb-1")
            direccion = _first_text(info, "p.address.mt-2")
            precio = _first_text(info, "p.price.mt-2")  # puede venir vacío según tu versión

            # Tipologías en el edificio
            departamentos = []
            rooms = container.select_one("div.building-rooms")
            if rooms:
                for item in rooms.select("div.building-rooms--items"):
                    a = item.find("a")
                    if not a:
                        continue

                    # Texto general: "2 Dormitorios | Ver unidades (3)" o similar
                    texto = _clean_text(a.get_text(separator="|"))
                    # Dormitorios: tomar el primer número del texto (antes de '|')
                    dorms_token = texto.split("|", 1)[0] if "|" in texto else texto
                    dormitorios = _find_int(dorms_token, default="")

                    # Unidades disponibles: en <span class="d-inline-block">N</span>
                    span = a.find("span", class_="d-inline-block")
                    unidades = _find_int(span.get_text() if span else "", default="")

                    href = a.get("href") or ""
                    link = _full_url(href)

                    departamentos.append({
                        "dormitorios": dormitorios,
                        "unidades_disponibles": unidades,
                        "link": link
                    })

            # --- Detalle por tipología (dorms/baños/m2) + comodidades del edificio ---
            comodidades = []
            for idx, dept in enumerate(departamentos):
                link = dept.get("link")
                # inicializa campos detalle (por si fallan selectores)
                dept.setdefault("baños", "")
                dept.setdefault("m2_utiles", "")

                if not link:
                    continue

                driver.get(link)
                # espera explícita a algún bloque estable de la ficha
                try:
                    WebDriverWait(driver, wait_secs).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "ul.rework__features-list"))
                    )
                except Exception:
                    # si no cargó ese bloque, aún así intenta parsear todo el HTML
                    pass

                det_soup = BeautifulSoup(driver.page_source, "html.parser")

                # 1) Amenidades solo una vez por edificio (idx=0), si existen
                if idx == 0 and not comodidades:
                    ul_amen = det_soup.select_one("ul.rework__description-amenities")
                    if ul_amen:
                        for li in ul_amen.select("li"):
                            span = li.find("span")
                            if span:
                                txt = _clean_text(span.get_text())
                                if txt:
                                    comodidades.append(txt)

                # 2) Features: dormitorios/baños/m2 en icon list
                features = det_soup.select_one("ul.rework__features-list")
                if features:
                    # Dormitorios
                    bed = features.select_one("li.rework__feature--bed span.text")
                    if bed:
                        dept["dormitorios"] = _find_int(bed.get_text(), default=dept["dormitorios"])
                    # Baños
                    bath = features.select_one("li.rework__feature--bathtub span.text")
                    if bath:
                        dept["baños"] = _find_int(bath.get_text(), default="")
                    # m2 útiles
                    m2 = features.select_one("li.rework__feature--texture span.text")
                    if m2:
                        m2v = _find_num(m2.get_text(), default="")
                        dept["m2_utiles"] = f"{m2v} m2" if m2v else ""

                # rate limiting corto y aleatorio
                time.sleep(random.uniform(*detail_delay))

            edificios.append({
                "nombre": nombre,
                "direccion": direccion,
                "precio": precio,
                "comodidades": comodidades,
                "departamentos": departamentos
            })

        # Guardar JSON
        outdir = os.path.dirname(output_path)
        if outdir:
            os.makedirs(outdir, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(edificios, f, ensure_ascii=False, indent=2)

        print(f"Scraping completado. {len(edificios)} edificios guardados en {output_path}")
        return edificios

    finally:
        try:
            driver.quit()
        except Exception:
            pass

# --- (Opcional) si la página tiene infinite scroll, descomenta y llama antes de parsear:
# def _infinite_scroll(driver, max_scrolls=6, pause=(0.5, 0.9)):
#     last_height = driver.execute_script("return document.body.scrollHeight")
#     for _ in range(max_scrolls):
#         driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
#         time.sleep(random.uniform(*pause))
#         new_height = driver.execute_script("return document.body.scrollHeight")
#         if new_height == last_height:
#             break
#         last_height = new_height

if __name__ == "__main__":
    url = "https://bluehome.cl/departamento"
    output_path = "data/raw/bluehome.json"
    scrape_bluehome(url, output_path)
