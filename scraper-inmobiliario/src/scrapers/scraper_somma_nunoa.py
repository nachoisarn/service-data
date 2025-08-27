import re
import json
import time
import random
from typing import Dict, Any, List
from collections import defaultdict

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter, Retry

# ---------- Helpers de red (con fallbacks opcionales) ----------
_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

def _make_session() -> requests.Session:
    s = requests.Session()
    # No reintentar 403
    retries = Retry(
        total=2,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.mount("http://", HTTPAdapter(max_retries=retries))
    return s

def _requests_fetch(url: str, timeout: int) -> str:
    ses = _make_session()
    headers = {
        "User-Agent": random.choice(_UAS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": "https://www.google.com/",
    }
    time.sleep(random.uniform(0.4, 1.0))
    r = ses.get(url, headers=headers, timeout=timeout)
    if r.status_code == 403:
        raise requests.HTTPError("403 Forbidden", response=r)
    r.raise_for_status()
    return r.text

def _cloudscraper_fetch(url: str, timeout: int) -> str:
    import cloudscraper  # pip install cloudscraper
    scraper = cloudscraper.create_scraper(browser={
        "browser": "chrome",
        "platform": "windows",
        "mobile": False
    })
    r = scraper.get(url, timeout=timeout)
    if r.status_code == 403:
        raise requests.HTTPError("403 Forbidden", response=r)
    r.raise_for_status()
    return r.text

def _playwright_fetch(url: str, timeout: int) -> str:
    # pip install playwright && playwright install
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale="es-CL", user_agent=random.choice(_UAS))
        page.set_extra_http_headers({"Referer": "https://www.google.com/"})
        page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        page.wait_for_timeout(1200)
        html = page.content()
        browser.close()
        return html

def _fetch_html(url: str, timeout: int, use_browser: bool, use_cloudscraper: bool) -> str:
    if use_browser:
        return _playwright_fetch(url, timeout)
    try:
        return _requests_fetch(url, timeout)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 403:
            if use_cloudscraper:
                return _cloudscraper_fetch(url, timeout)
        raise

# ---------- Helpers de parsing ----------
NUM_RE = re.compile(r"(\d+(?:[.,]\d+)?)")
UNITS_RE = re.compile(r"(\d+)\s+Unidades?\s+disponibles", re.I)

def _text(el) -> str:
    return el.get_text(" ", strip=True) if el else ""

def _safe_int(s: str, default: int = 0) -> int:
    try:
        return int(s)
    except Exception:
        return default

def _extract_m2(dynamic_after_text: str) -> str:
    # Ej: "47+ m2", "45+ m2", "37 m²"
    m = NUM_RE.search(dynamic_after_text.replace(",", "."))
    return f"{m.group(1)} m2" if m else ""

def _extract_units(availability_text: str) -> int:
    if not availability_text:
        return 0
    m = UNITS_RE.search(availability_text)
    if m:
        return _safe_int(m.group(1), 0)
    # Frases tipo "Disponible 26 de agosto de 2025" -> 1
    if "disponible" in availability_text.lower():
        return 1
    return 0

def _extract_bed_bath(block: BeautifulSoup) -> (str, str):
    # Dentro de .fp-details li.dynamic-text hay varios spans.small-abbr con el patrón:
    # [ "2", "Dormitorio", "/", "1", "Baño" ]
    li = block.select_one("ul.fp-details li.dynamic-text")
    if not li:
        return "", ""
    abbrs = [t.get_text(strip=True) for t in li.select(".small-abbr")]
    # Buscar primer número como dormitorios y segundo número como baños
    nums = [a for a in abbrs if a.isdigit()]
    dorms = nums[0] if len(nums) >= 1 else ""
    baths = nums[1] if len(nums) >= 2 else ""
    return dorms, baths

# ---------- Función principal ----------
def scrape_departamentos(
    url: str,
    output_path: str,
    timeout: int = 30,
    use_browser: bool = False,
    use_cloudscraper: bool = True
) -> Dict[str, Any]:
    """
    Lee cards .fp-card y devuelve/agrega información por dormitorios.

    Retorna y escribe:
    {
      "fuente": url,
      "departamentos": [ { dormitorios, unidades_disponibles, baños, m2_utiles }... ],
      "detalle_plantas": [
        { planta, dormitorios, baños, m2_utiles, precio_mensual, availability_text, unidades_disponibles, link_detalle, img_url }
      ]
    }
    """
    html = _fetch_html(url, timeout, use_browser, use_cloudscraper)
    soup = BeautifulSoup(html, "html.parser")

    tiles: List[Dict[str, Any]] = []
    for card in soup.select(".fp-group .fp-card"):
        title = _text(card.select_one(".inner-card-container .fp-title"))

        # camas/baños
        dorms, baths = _extract_bed_bath(card)

        # m2 útiles (parte derecha de la línea)
        dynamic_after = _text(card.select_one("ul.fp-details li.dynamic-text .dynamic-text-after"))
        m2_utiles = _extract_m2(dynamic_after)

        # precio (si existe)
        precio_mensual = _text(card.select_one(".fee-transparency-wrapper .fee-transparency-text"))

        # availability (puede decir "X Unidades disponibles" o "Disponible <fecha>")
        availability_text = _text(card.select_one(".right-content .availability"))
        if not availability_text:
            # A veces lo ponen en otro lugar del card
            availability_text = _text(card.select_one(".availability"))

        unidades_disponibles = _extract_units(availability_text)

        # link detalle e imagen
        link_detalle = ""
        a_detalle = card.select_one(".right-content a.primary.btn[href]")
        if a_detalle and a_detalle.has_attr("href"):
            link_detalle = a_detalle["href"]

        img_url = ""
        a_img = card.select_one(".fp-img a[data-url]")
        if a_img and a_img.has_attr("data-url"):
            img_url = a_img["data-url"]

        tiles.append({
            "planta": title,
            "dormitorios": dorms,
            "baños": baths,
            "m2_utiles": m2_utiles,
            "precio_mensual": precio_mensual,
            "availability_text": availability_text,
            "unidades_disponibles": unidades_disponibles,
            "link_detalle": link_detalle,
            "img_url": img_url
        })

    # --- Agregado por número de dormitorios (tu formato) ---
    agg = defaultdict(lambda: {"unidades_disponibles": 0, "baños": "", "m2_utiles": ""})
    for t in tiles:
        d = t["dormitorios"] or ""
        agg[d]["unidades_disponibles"] += t["unidades_disponibles"]
        # dejamos baños/m2 vacíos en el agregado (pueden variar por planta)

    def _sort_key(item):
        k = item[0]
        return (int(k) if k.isdigit() else 999, k)

    departamentos = []
    for d, vals in sorted(agg.items(), key=_sort_key):
        departamentos.append({
            "dormitorios": d,  # "0" podría representar Estudio si existiera
            "unidades_disponibles": str(vals["unidades_disponibles"]),
            "baños": vals["baños"],
            "m2_utiles": vals["m2_utiles"]
        })

    resultado: Dict[str, Any] = {
        "fuente": url,
        "departamentos": departamentos,
        "detalle_plantas": tiles
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(resultado, f, ensure_ascii=False, indent=2)

    return resultado


if __name__ == "__main__":
    data = scrape_departamentos(
        "https://www.sommaplazanunoa.cl/santiago/somma-plaza-%C3%B1u%C3%B1oa/conventional/",
        "data//raw//sommanunoa.json"
    )
