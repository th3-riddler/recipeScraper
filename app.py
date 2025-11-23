from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import recipe_scrapers
import logging
import time
import os
import requests
from io import BytesIO
from PIL import Image
from dotenv import load_dotenv

# Carica variabili da .env
load_dotenv()

# ==========================
# CONFIGURAZIONE BASE
# ==========================
app = Flask(__name__)
CORS(app)

# Log leggibili da systemd
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s in %(module)s: %(message)s'
)

# Autenticazione richiesta - configurare SCRAPER_API_KEY
API_KEY = os.environ.get("SCRAPER_API_KEY")

if not API_KEY:
    logging.error("ERRORE: SCRAPER_API_KEY non configurata nel file .env")
    logging.error("Il servizio non può avviarsi senza autenticazione")
    exit(1)

logging.info("SCRAPER_API_KEY configurata — autenticazione abilitata")

@app.before_request
def require_api_key():
    # Permetti richieste OPTIONS (CORS preflight) senza autenticazione
    if request.method == "OPTIONS":
        return None
    
    if request.path == "/scrape":
        key = request.headers.get("X-API-Key")
        if key != API_KEY:
            logging.warning(f"Tentativo di accesso non autorizzato da {request.remote_addr}")
            return jsonify({"error": "Unauthorized"}), 403


import re

def parse_ingredient(ingredient_text):
    """
    Separa la quantità dal nome dell'ingrediente.
    Ritorna un dizionario con 'quantity' e 'name'.
    Supporta formati:
    - "quantità nome" (es. "2 cups flour", "200g farina", "q.b. sale")
    - "nome quantità" (es. "flour 200 g", "farina 200 g", "sale q.b.")
    """
    if not ingredient_text or not isinstance(ingredient_text, str):
        return {"quantity": "", "name": ""}
    
    text = ingredient_text.strip()
    if not text:
        return {"quantity": "", "name": ""}
    
    # Unità di misura comuni (italiano e inglese)
    units = r'cups?|tablespoons?|tbsps?|teaspoons?|tsps?|grams?|g|kg|mg|ml|l|cl|dl|oz|lbs?|pounds?|' \
            r'tazz[ae]|cucchiai[oi]?|cucchiaini?|pizzic[oi]|q\.?b\.?|pezz[oi]|fett[ae]|' \
            r'spicchi[oi]?|foglie?|ramett[oi]?|manciata|manciate|mazzo|mazzi|unità|unitá|pz\.?|n\.?|' \
            r'cloves?|pinches?|pieces?|slices?'
    
    # Quantità speciali senza numeri (q.b., pizzico, manciata, etc.)
    special_units = r'q\.?b\.?|pizzic[oi]|manciata|manciate|mazzo|mazzi'
    
    # Prova prima a cercare la quantità alla FINE (es. "flour 200 g")
    quantity_at_end = re.compile(
        rf'^(.+?)\s+([0-9\/\-.,]+\s*(?:{units}))$',
        re.IGNORECASE
    )
    
    match_end = quantity_at_end.match(text)
    if match_end:
        name = match_end.group(1).strip()
        quantity = match_end.group(2).strip()
        if name and len(name) > 2:
            return {"quantity": quantity, "name": name}
    
    # Cerca quantità speciali alla FINE (es. "sale q.b.")
    special_at_end = re.compile(
        rf'^(.+?)\s+({special_units})$',
        re.IGNORECASE
    )
    
    match_special_end = special_at_end.match(text)
    if match_special_end:
        name = match_special_end.group(1).strip()
        quantity = match_special_end.group(2).strip()
        if name and len(name) > 2:
            return {"quantity": quantity, "name": name}
    
    # Cerca quantità speciali all'INIZIO (es. "q.b. sale", "qb sale")
    special_quantity = re.compile(
        rf'^({units})\s+(?:di\s+)?(.+)$',
        re.IGNORECASE
    )
    
    match_special = special_quantity.match(text)
    if match_special:
        quantity = match_special.group(1).strip()
        name = match_special.group(2).strip()
        if name and len(name) > 2:
            return {"quantity": quantity, "name": name}
    
    # Altrimenti cerca la quantità all'INIZIO (es. "2 cups flour")
    quantity_at_start = re.compile(
        rf'^([0-9\/\-.,]+\s*(?:{units})?)\s+(?:di\s+)?(.+)$',
        re.IGNORECASE
    )
    
    match_start = quantity_at_start.match(text)
    if match_start:
        quantity = match_start.group(1).strip()
        name = match_start.group(2).strip()
        if name and len(name) > 2:
            return {"quantity": quantity, "name": name}
    
    # Se non trova pattern, assume che sia solo il nome
    return {"quantity": "", "name": text}


def format_yields(yields_value):
    """
    Estrae solo il numero dal valore delle porzioni.
    Es: "4 servings" -> "4"
        "1 serving" -> "1"
        "6" -> "6"
    """
    if not yields_value or yields_value == "N/A":
        return "N/A"
    
    text = str(yields_value).strip()
    
    # Estrai il numero usando regex
    number_match = re.search(r'(\d+)', text)
    if number_match:
        return number_match.group(1)
    
    # Se non trova un numero, ritorna N/A
    return "N/A"


def safe_call(method):
    try:
        return method()
    except (NotImplementedError, AttributeError, recipe_scrapers._exceptions.SchemaOrgException):
        return "N/A"
    except Exception as e:
        logging.error(f"Errore in safe_call: {e}")
        return "N/A"


# ==========================
# ENDPOINT PRINCIPALE
# ==========================
@app.route("/scrape", methods=["GET"])
def scrape_recipe():
    url = request.args.get("url")
    if not url:
        return jsonify({"error": "Missing URL parameter"}), 400

    logging.info(f"Scraping: {url}")
    try:
        scraper = recipe_scrapers.scrape_me(url)
    except Exception as e:
        logging.error(f"Errore iniziale di scraping: {e}")
        return jsonify({"error": str(e), "url": url}), 500

    image_url = safe_call(getattr(scraper, "image", lambda: "N/A"))
    
    # Ottieni gli ingredienti e parsali
    raw_ingredients = safe_call(getattr(scraper, "ingredients", lambda: "N/A"))
    parsed_ingredients = []
    
    if raw_ingredients != "N/A" and isinstance(raw_ingredients, list):
        for ing in raw_ingredients:
            parsed_ingredients.append(parse_ingredient(ing))
    
    data = {
        "title": safe_call(getattr(scraper, "title", lambda: "N/A")),
        "cook_time": safe_call(getattr(scraper, "cook_time", lambda: "N/A")),
        "prep_time": safe_call(getattr(scraper, "prep_time", lambda: "N/A")),
        "total_time": safe_call(getattr(scraper, "total_time", lambda: "N/A")),
        "yields": format_yields(safe_call(getattr(scraper, "yields", lambda: "N/A"))),
        "ingredients": parsed_ingredients,
        "instructions": safe_call(getattr(scraper, "instructions", lambda: "N/A")),
        "url": url,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "author": safe_call(getattr(scraper, "author", lambda: "N/A")),
        "category": safe_call(getattr(scraper, "category", lambda: "N/A")),
        "cuisine": safe_call(getattr(scraper, "cuisine", lambda: "N/A")),
        "description": safe_call(getattr(scraper, "description", lambda: "N/A")),
        "image": image_url,
    }

    return jsonify(data)


# ==========================
# PROXY IMMAGINI
# ==========================
@app.route("/image-proxy", methods=["GET"])
def image_proxy():
    """Proxy per scaricare e convertire immagini in formati compatibili"""
    url = request.args.get("url")
    if not url:
        return jsonify({"error": "Missing URL parameter"}), 400
    
    try:
        # Scarica l'immagine
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
            'Accept-Language': 'it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7',
            'Referer': url.rsplit('/', 1)[0] if '/' in url else url,
        }
        
        response = requests.get(url, headers=headers, timeout=10, stream=True)
        response.raise_for_status()
        
        # Converti in formato PNG o JPEG standard
        img = Image.open(BytesIO(response.content))
        
        # Converti in RGB se necessario (per gestire PNG con trasparenza o altri formati)
        if img.mode in ('RGBA', 'LA', 'P'):
            background = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        
        # Ridimensiona se troppo grande (max 1200px)
        max_size = 1200
        if img.width > max_size or img.height > max_size:
            img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
        
        # Salva in JPEG
        img_io = BytesIO()
        img.save(img_io, 'JPEG', quality=85, optimize=True)
        img_io.seek(0)
        
        return send_file(img_io, mimetype='image/jpeg', download_name='image.jpg')
        
    except requests.RequestException as e:
        logging.error(f"Errore nel download dell'immagine: {e}")
        return jsonify({"error": f"Failed to download image: {str(e)}"}), 500
    except Exception as e:
        logging.error(f"Errore nella conversione dell'immagine: {e}")
        return jsonify({"error": f"Failed to process image: {str(e)}"}), 500


# ==========================
# HEALTH CHECK
# ==========================
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "recipe-scraper"}), 200


# ==========================
# ENTRYPOINT
# ==========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)