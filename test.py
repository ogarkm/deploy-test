import requests
from bs4 import BeautifulSoup
import re
import logging
import json
import os
import time

# Configure Console Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("WeebCentralTest")

LOG_FILE = "responses_test.txt"

def log_to_file(url, method, status, req_headers, req_body, resp_text):
    """Logs full request and response details to responses_test.txt."""
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("="*100 + "\n")
            f.write(f"URL: {url}\n")
            f.write(f"METHOD: {method}\n")
            f.write(f"STATUS: {status}\n")
            f.write("-" * 40 + "\n")
            f.write("REQUEST HEADERS:\n")
            for k, v in req_headers.items():
                f.write(f"  {k}: {v}\n")
            if req_body:
                f.write("-" * 40 + "\n")
                f.write(f"REQUEST BODY: {req_body}\n")
            f.write("-" * 40 + "\n")
            f.write("RESPONSE BODY:\n")
            f.write(resp_text if resp_text else "[EMPTY BODY]")
            f.write("\n" + "="*100 + "\n\n")
    except Exception as e:
        print(f"Logging error: {e}")

class WeebCentralProvider:
    def __init__(self):
        self.base_url = "https://weebcentral.com"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        })

    def get_mal_titles(self, mal_id):
        """Fetches titles from MyAnimeList API."""
        logger.info(f"Fetching MAL metadata for ID: {mal_id}")
        url = f"https://api.jikan.moe/v4/manga/{mal_id}"
        resp = requests.get(url)
        
        # Log MAL response too
        log_to_file(url, "GET", resp.status_code, resp.request.headers, None, resp.text)
        
        if resp.status_code != 200:
            logger.error(f"MAL API failed with status {resp.status_code}")
            return None, None
            
        data = resp.json().get('data', {})
        romaji = data.get('title')
        english = data.get('title_english')
        return romaji, english

    def search(self, query: str):
        if not query: return []
        logger.info(f"--- Searching WeebCentral for: '{query}' ---")
        search_url = f"{self.base_url}/search/simple?location=main"
        
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "HX-Request": "true",
            "HX-Trigger": "quick-search-input",
            "HX-Trigger-Name": "text",
            "HX-Target": "quick-search-result",
            "HX-Current-URL": f"{self.base_url}/",
        }
        data = {"text": query}

        response = self.session.post(search_url, headers=headers, data=data)
        
        # Log Search details
        log_to_file(search_url, "POST", response.status_code, {**self.session.headers, **headers}, data, response.text)
        
        if response.status_code != 200:
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        results = []
        items = soup.select("a")

        for item in items:
            link = item.get("href", "")
            if "/series/" not in link: continue

            title_el = item.select_one(".flex-1")
            title = title_el.get_text(strip=True) if title_el else "Unknown"
            
            id_match = re.search(r"/series/([^/]+)", link)
            manga_id = id_match.group(1) if id_match else None

            # Logic: If query is in the site title, or vice-versa
            if manga_id and (query.lower() in title.lower() or title.lower() in query.lower()):
                results.append({
                    "id": manga_id,
                    "title": title,
                    "url": link
                })

        return results

    def find_chapters(self, manga_id: str):
        logger.info(f"--- Finding Chapters for Manga ID: {manga_id} ---")
        chapter_url = f"{self.base_url}/series/{manga_id}/full-chapter-list"
        
        headers = {
            "HX-Request": "true",
            "HX-Target": "chapter-list",
            "HX-Current-URL": f"{self.base_url}/series/{manga_id}",
            "Referer": f"{self.base_url}/series/{manga_id}",
        }

        response = self.session.get(chapter_url, headers=headers)
        log_to_file(chapter_url, "GET", response.status_code, {**self.session.headers, **headers}, None, response.text)
        
        soup = BeautifulSoup(response.text, "html.parser")
        chapters = []
        rows = soup.select("div.flex.items-center")
        
        for row in rows:
            a = row.find("a")
            if not a: continue
            
            href = a.get("href", "")
            title_span = a.select_one("span.grow > span")
            title = title_span.get_text(strip=True) if title_span else ""
            
            id_match = re.search(r"/chapters/([^/]+)", href)
            if not id_match: continue
            
            num_match = re.search(r"(\d+(?:\.\d+)?)", title)
            chapter_num = num_match.group(1) if num_match else "0"

            chapters.append({
                "id": id_match.group(1),
                "title": title,
                "chapter": chapter_num
            })

        chapters.reverse()
        return chapters

    def find_chapter_pages(self, chapter_id: str):
        logger.info(f"--- Finding Pages for Chapter ID: {chapter_id} ---")
        url = f"{self.base_url}/chapters/{chapter_id}/images?is_prev=False&reading_style=long_strip"
        
        headers = {
            "HX-Request": "true",
            "HX-Current-URL": f"{self.base_url}/chapters/{chapter_id}",
            "Referer": f"{self.base_url}/chapters/{chapter_id}",
        }

        response = self.session.get(url, headers=headers)
        log_to_file(url, "GET", response.status_code, {**self.session.headers, **headers}, None, response.text)
        
        soup = BeautifulSoup(response.text, "html.parser")
        images = soup.select("section.flex-1 img") or soup.find_all("img")
        
        pages = []
        for idx, img in enumerate(images):
            src = img.get("src")
            if src: pages.append(src)

        return pages

def run_test():
    TEST_MAL_ID = 23390  # Attack on Titan
    
    if os.path.exists(LOG_FILE):
        os.remove(LOG_FILE)
    
    provider = WeebCentralProvider()

    # 1. Get titles from MAL
    romaji, english = provider.get_mal_titles(TEST_MAL_ID)
    if not romaji and not english:
        logger.error("Could not fetch titles from MAL.")
        return

    # 2. Try Search (First English, then Romaji)
    search_results = []
    for title in [english, romaji]:
        if not title: continue
        search_results = provider.search(title)
        if search_results:
            break
        logger.warning(f"No results for '{title}', trying next title...")

    if not search_results:
        logger.error(f"Failed to find manga on WeebCentral for ID {TEST_MAL_ID}. Check {LOG_FILE}")
        return

    target = search_results[0]
    logger.info(f"Matched: {target['title']} (ID: {target['id']})")

    # 3. Get Chapters
    chapters = provider.find_chapters(target['id'])
    if not chapters:
        logger.error("Chapter list extraction failed.")
        return
    
    logger.info(f"Found {len(chapters)} chapters. Fetching pages for last chapter: {chapters[-1]['title']}")

    # 4. Get Pages
    pages = provider.find_chapter_pages(chapters[-1]['id'])
    
    if pages:
        logger.info(f"SUCCESS! Found {len(pages)} pages.")
        logger.info(f"Full transaction logs: {os.path.abspath(LOG_FILE)}")
    else:
        logger.error("Failed to extract pages.")

if __name__ == "__main__":
    run_test()