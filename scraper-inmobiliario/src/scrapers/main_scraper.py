from src.scrapers.scraper_assetplan import scrape_assetplan

# Aquí puedes importar más scrapers, por ejemplo:
# from scraper_sitio2 import scrape_sitio2_site

SCRAPERS = {
    "assetplan": scrape_assetplan
}

def run_scrapers():
    sitios = [
        {
            "nombre": "assetplan",
            "url": "https://www.assetplan.cl/arriendo/departamento/-70.62983131583,-33.472787945848?page=1&servicioPro=1",
            "output": "data/raw/assetplan.txt"
        }
    ]
    for sitio in sitios:
        print(f"Scrapeando {sitio['nombre']}...")
        SCRAPERS[sitio["nombre"]](sitio["url"], sitio["output"])
