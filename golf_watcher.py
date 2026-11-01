#!/usr/bin/env python3
"""
Golf GTI watcher - co kilka minut sprawdza Twoje wyszukiwania na portalach
z ogłoszeniami (otomoto, OLX, autoplac, automarket, gratka i inne)
i daje znać, gdy pojawi się NOWE ogłoszenie.

Jak to działa: filtry (GTI, DSG, rocznik, przebieg, cena, kraj pochodzenia)
ustawiasz RAZ na stronie danego portalu, kopiujesz adres z paska przeglądarki
i wklejasz go do SEARCH_URLS poniżej (po jednym adresie na portal).
Skrypt tylko pilnuje, czy na liście pojawiły się nowe linki do ogłoszeń.

Uruchomienie:
    pip install requests
    python golf_watcher.py
"""

import html
import json
import os
import platform
import re
import smtplib
import sys
import threading
import time
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import cloudscraper
# ====== USTAWIENIA ==========================================================

# Wklej tu adresy swoich wyszukiwań - po jednym na portal, w cudzysłowie,
# oddzielone przecinkami. Przykład:
#   "https://www.otomoto.pl/osobowe/volkswagen/golf/od-2015?search...",
#   "https://www.olx.pl/motoryzacja/samochody/volkswagen/q-golf-gti/?search...",
#   "https://autoplac.pl/oferty/samochody-osobowe/volkswagen/golf?...",
#   "https://automarket.pl/...",
SEARCH_URLS = [
]

# Co ile sekund sprawdzać (nie schodź poniżej ok. 300, żeby nie dostać blokady).
INTERVAL_SECONDS = 600

# --- Powiadomienia (wypełnij te, których chcesz używać, resztę zostaw pustą) ---

# Telegram (powiadomienie na telefon)
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""

# E-mail (np. Gmail: wymaga "hasła aplikacji", nie zwykłego hasła)
EMAIL_FROM = ""        # np. twojmail@gmail.com
EMAIL_PASSWORD = ""    # hasło aplikacji z konta Google
EMAIL_TO = ""          # na jaki adres wysłać (może być ten sam)
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

# Okienko na ekranie komputera (tylko Windows)
WINDOWS_POPUP = True

# Ustawienia można też podać jako zmienne środowiskowe o tych samych nazwach
# (tak działa wersja w chmurze). SEARCH_URLS wtedy podajesz jako adresy
# w osobnych liniach.
SEARCH_URLS = [u for u in os.environ.get("SEARCH_URLS", "").splitlines() if u.strip()] or SEARCH_URLS
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)
EMAIL_FROM = os.environ.get("EMAIL_FROM", EMAIL_FROM)
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", EMAIL_PASSWORD)
EMAIL_TO = os.environ.get("EMAIL_TO", EMAIL_TO)

# ============================================================================

# Fragmenty adresu, po których rozpoznajemy link do POJEDYNCZEGO ogłoszenia.
# Działa na wielu portalach naraz. Jeśli któryś portal używa innego wzorca,
# dopisz tu jego fragment (np. "/auto/").
LISTING_PATH_HINTS = [
    "/oferta/",        # otomoto, autoplac i inne
    "/d/oferta/",      # olx
    "/ogloszenie/",    # automarket i inne
    "/ogloszenia/",
    "/samochod/",
]
# Gratka i podobne kończą adres na -ob12345678
LISTING_ID_REGEX = re.compile(r"-ob\d{5,}")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}

SEEN_FILE = Path("seen_listings.json")
URL_REGEX = re.compile(r'(?:https?:)?//[^\s"\'<>\\]+|/[A-Za-z0-9_\-./%]+')


def load_seen() -> dict:
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    return {}


def save_seen(seen: dict) -> None:
    SEEN_FILE.write_text(json.dumps(seen, indent=2), encoding="utf-8")


def looks_like_listing(path: str) -> bool:
    if len(path.strip("/").split("/")[-1]) < 12:  # za krótki "slug" = to nie ogłoszenie
        return False
    if LISTING_ID_REGEX.search(path):
        return True
    return any(hint in path for hint in LISTING_PATH_HINTS)


def extract_links(page_html: str, base_url: str) -> set:
    text = html.unescape(page_html).replace("\\/", "/").replace("\\u002F", "/")
    base_host = urlparse(base_url).netloc.replace("www.", "")
    links = set()
    for raw in URL_REGEX.findall(text):
        full = urljoin(base_url, raw).split("?")[0].split("#")[0]
        parsed = urlparse(full)
        if parsed.netloc.replace("www.", "") != base_host:
            continue
        if looks_like_listing(parsed.path):
            links.add(full)
    return links


def _popup(message: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, "Golf GTI watcher", 0x40 | 0x1000)
    except Exception:
        pass


def notify(message: str) -> None:
    print("\a" + message)  # \a = sygnał dźwiękowy w terminalu

    if WINDOWS_POPUP and platform.system() == "Windows":
        threading.Thread(target=_popup, args=(message,), daemon=True).start()

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": message},
                timeout=15,
            )
        except requests.RequestException as e:
            print(f"[!] Telegram nie zadziałał: {e}")

    if EMAIL_FROM and EMAIL_PASSWORD and EMAIL_TO:
        try:
            msg = EmailMessage()
            msg["Subject"] = "Nowe ogłoszenie: Golf GTI"
            msg["From"] = EMAIL_FROM
            msg["To"] = EMAIL_TO
            msg.set_content(message)
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
                smtp.login(EMAIL_FROM, EMAIL_PASSWORD)
                smtp.send_message(msg)
        except Exception as e:
            print(f"[!] E-mail nie zadziałał: {e}")


def check(url: str, seen: dict) -> None:
    site = urlparse(url).netloc
  try:
        scraper = cloudscraper.create_scraper()
        resp = scraper.get(url, headers=HEADERS, timeout=30)
    except Exception as e:
        print(f"[!] {site}: błąd połączenia: {e}")
        return

    if resp.status_code != 200:
        print(f"[!] {site}: kod {resp.status_code} (portal mógł zablokować skrypt)")
        return

    links = extract_links(resp.text, url)
    if not links:
        print(f"[!] {site}: nie znalazłem ogłoszeń (strona ładuje się skryptami albo ma inny układ)")
        return

    first_run = url not in seen
    known = set(seen.get(url, []))
    new = links - known

    if first_run:
        print(f"[ok] {site}: pierwsze uruchomienie, zapamiętano {len(links)} ogłoszeń.")
    elif new:
        for link in sorted(new):
            notify(f"NOWE OGŁOSZENIE ({site}): {link}")
    else:
        print(f"[ok] {time.strftime('%H:%M:%S')} {site}: bez nowych ({len(links)} na liście)")

    seen[url] = sorted(known | links)


def main() -> None:
    if not SEARCH_URLS:
        print("Najpierw wklej adresy wyszukiwań do SEARCH_URLS w pliku skryptu.")
        return

    once = "--once" in sys.argv  # jedno sprawdzenie i koniec (do uruchamiania w chmurze)
    seen = load_seen()
    if not once:
        print(f"Pilnuję {len(SEARCH_URLS)} wyszukiwań, sprawdzam co {INTERVAL_SECONDS} s. Ctrl+C kończy.")
    while True:
        for url in SEARCH_URLS:
            check(url, seen)
            time.sleep(5)
        save_seen(seen)
        if once:
            return
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
