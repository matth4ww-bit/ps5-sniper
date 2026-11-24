"""
PS5 sniper - pilnuje zestawu w Media Expert:
  PS5 Slim 1TB (napęd) + 2. pad DualSense + GTA VI: Pakiet Vintage Vice City - 3 799,99 zł

Co robi:
  * co kilka minut otwiera kartę produktu w prawdziwej przeglądarce (Chromium przez Playwright),
  * sprawdza, czy da się go zamówić online z dostawą,
  * otwiera okno "Odbierz w sklepie", wpisuje Twoje miasto i czyta listę sklepów, które mają go na stanie,
  * jeśli coś jest online albo w sklepie bliżej niż MAX_KM - wysyła powiadomienie na telefon (ntfy),
    piszczy, otwiera stronę produktu w Twojej przeglądarce i kończy działanie.

Koszyk i płatność robisz sam - program niczego nie kupuje.

Instalacja (raz):
    pip install playwright requests
    python -m playwright install chromium

Uruchomienie:
    python ps5_sniper.py
"""

import json
import os
import random
import sys
import time
import webbrowser
from datetime import datetime

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---------------- USTAWIENIA ----------------
PRODUCT_URL = (
    "https://www.mediaexpert.pl/gaming/playstation-5/konsole-ps5/"
    "konsola-sony-playstation-5-slim-e-chassis-1tb-kontroler-sony-dualsense-bialy-"
    "grand-theft-auto-vi-pakiet-vintage-vice-cit"
)
CITY = "Czeladź"          # miasto do wyszukiwania sklepów
MAX_KM = 40               # alarm tylko dla sklepów w tym promieniu
CHECK_EVERY_MIN = 5       # co ile minut sprawdzać (nie schodź poniżej 3)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC") or "mateusz-ps5-8472"  # temat w aplikacji ntfy
HEADLESS = True           # True = przeglądarka całkiem niewidoczna
OFFSCREEN_IF_BLOCKED = True  # gdy w trybie niewidocznym strona blokuje: okno poza ekranem, bez fokusa
OPEN_PAGE_ON_ALERT = False   # True = przy alarmie otwiera stronę w Twojej przeglądarce (wyskoczy nad grą)
BEEP_ON_ALERT = False        # True = pisk przy alarmie
STOP_AFTER_ALERT = True   # po znalezieniu kończy działanie
NOTIFY_EVERY_CHECK = True # cichy raport na telefon po każdym sprawdzeniu
# --------------------------------------------
LAST_STORES_OK = False


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def send_ntfy(title, body, priority=3, tags=("video_game",)):
    """Wysyła powiadomienie na telefon (JSON, więc polskie znaki działają).
    priority: 1 = ciche, 3 = normalne, 5 = pilne"""
    try:
        r = requests.post("https://ntfy.sh/", json={
            "topic": NTFY_TOPIC, "title": title, "message": body,
            "priority": priority, "tags": list(tags), "click": PRODUCT_URL,
        }, timeout=10)
        if r.status_code != 200:
            log(f"ntfy odpowiedział {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log(f"ntfy nie wysłane: {e}")


def notify(title, body):
    log(f"*** {title}: {body}")
    send_ntfy(title, body, priority=5, tags=("rotating_light", "video_game"))
    if BEEP_ON_ALERT:
        try:
            if sys.platform == "win32":
                import winsound
                for _ in range(5):
                    winsound.Beep(1500, 300)
            else:
                print("\a" * 5, flush=True)
        except Exception:
            pass
    if OPEN_PAGE_ON_ALERT:
        webbrowser.open(PRODUCT_URL)


def dismiss_cookies(page):
    """Baner cookies (OneTrust) zasłania przyciski. Zapisujemy ustawienia domyślne
    (tylko niezbędne), a jak się nie da - po prostu chowamy baner bez wyrażania zgody."""
    for sel in ("#onetrust-reject-all-handler", "button.save-preference-btn-handler",
                "button:has-text('Zapisz ustawienia')", "button:has-text('Odrzuć')"):
        try:
            loc = page.locator(sel).locator("visible=true").first
            loc.click(timeout=1200)
            page.wait_for_timeout(500)
            break
        except Exception:
            pass
    try:
        page.evaluate("""() => {
            for (const s of ['#onetrust-consent-sdk', '.onetrust-pc-dark-filter', '#onetrust-banner-sdk'])
                document.querySelectorAll(s).forEach(e => e.remove());
            document.body.style.overflow = 'auto';
        }""")
    except Exception:
        pass


def check_once(page):
    """Zwraca (online: bool, sklepy: list[dict])"""
    global LAST_STORES_OK
    LAST_STORES_OK = False
    page.goto(PRODUCT_URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2500)
    dismiss_cookies(page)

    text = page.inner_text("body")
    online = "Dodaj do koszyka" in text and "chwilowo niedostępny" not in text

    stores = []
    step = "otwieranie okienka 'Odbierz w sklepie'"
    try:
        # przycisk "Odbierz w sklepie" przy cenie (pierwszy widoczny)
        btn = page.locator("button.add-to-cart-button:has-text('Odbierz w sklepie'), "
                           "button:has-text('Odbierz w sklepie')").locator("visible=true").first
        btn.wait_for(timeout=10000)
        btn.scroll_into_view_if_needed(timeout=5000)
        try:
            btn.click(timeout=5000)
        except PWTimeout:
            dismiss_cookies(page)
            btn.dispatch_event("click")   # klik "na siłę", gdyby coś dalej zasłaniało
        page.wait_for_timeout(1200)

        step = "wpisywanie miasta"
        field = page.locator("input[placeholder*='Miejscowo'], input[name*='address'], "
                             "input[aria-label*='Miejscowo']").locator("visible=true").first
        try:
            field.wait_for(timeout=5000)
        except PWTimeout:
            field = page.get_by_label("Miejscowość lub kod pocztowy", exact=False).first
        field.click(timeout=5000)
        field.fill("")
        field.type(CITY, delay=80)
        page.wait_for_timeout(800)

        step = "czekanie na listę sklepów"
        # strona sama pyta swoje API o sklepy po kliknięciu "Sprawdź" - łapiemy tę odpowiedź
        with page.expect_response(lambda r: "/nearby-pos" in r.url and r.status == 200,
                                  timeout=20000) as resp:
            try:
                page.get_by_role("button", name="Sprawdź").first.click(timeout=4000)
            except Exception:
                field.press("Enter")
        data = resp.value.json()
        if isinstance(data, list):
            stores = data
            LAST_STORES_OK = True
    except Exception as e:
        shot = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            f"debug_{datetime.now():%H%M%S}.png")
        try:
            page.screenshot(path=shot)
        except Exception:
            shot = "(brak)"
        log(f"Nie udało się odczytać sklepów na kroku: {step}. Tytuł strony: '{page.title()}'. "
            f"Zrzut: {shot}. ({type(e).__name__})")
    return online, stores


def main():
    once = "--raz" in sys.argv   # tryb dla GitHub Actions: jedno sprawdzenie i koniec
    log(f"Start. {'Jedno sprawdzenie' if once else f'Sprawdzam co ~{CHECK_EVERY_MIN} min'}, "
        f"sklepy do {MAX_KM} km od: {CITY}")
    if not once:
        send_ntfy("Snajper PS5 wystartował",
                  f"Sprawdzam co ~{CHECK_EVERY_MIN} min, sklepy do {MAX_KM} km od: {CITY}.",
                  priority=2, tags=("rocket",))
    with sync_playwright() as p:
        def start(headless):
            visible = "--widoczny" in sys.argv
            args = [] if (headless or visible) else ["--window-position=-32000,-32000",
                                                     "--window-size=1280,900"]
            b = p.chromium.launch(headless=headless, args=args)
            c = b.new_context(
                locale="pl-PL", viewport={"width": 1280, "height": 900},
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"))
            return b, c.new_page()

        headless = HEADLESS
        browser, page = start(headless)
        while True:
            try:
                online, stores = check_once(page)
                if headless and not stores and not online and OFFSCREEN_IF_BLOCKED \
                        and "media expert" not in page.title().lower():
                    log("Tryb niewidoczny chyba zablokowany - przełączam na okno poza ekranem.")
                    browser.close()
                    headless = False
                    browser, page = start(headless)
                    online, stores = check_once(page)
                near = [s for s in stores if (s.get("distance") or 9999) <= MAX_KM]
                summary = ", ".join(f"{s.get('city')} {s.get('distance', 0):.0f} km" for s in stores) or "brak"
                log(f"online: {'TAK' if online else 'nie'} | sklepy ze stanem: {summary}")
                if NOTIFY_EVERY_CHECK and not (online or near):
                    status = (f"Online: nie. Sklepy ze stanem: {summary}." if LAST_STORES_OK
                              else "Nie udało się odczytać sklepów - zajrzyj do okna programu.")
                    send_ntfy(f"Sprawdzono {datetime.now():%H:%M} - brak w pobliżu", status,
                              priority=2, tags=("hourglass",))

                if online:
                    notify("PS5 + GTA VI dostępne ONLINE!", "Media Expert - 3799,99 zł. Wchodź i kupuj.")
                if near:
                    lines = "; ".join(
                        f"{s.get('name')} ({s.get('distance', 0):.0f} km, "
                        f"{s.get('availability_variants', [{}])[0].get('stock', '?')} szt.)"
                        for s in near)
                    notify("PS5 + GTA VI w sklepie blisko!", lines)
                if (online or near) and STOP_AFTER_ALERT:
                    break
            except Exception as e:
                log(f"Błąd sprawdzania: {e}")
                if once:
                    send_ntfy("Snajper PS5 - błąd", str(e)[:300], priority=2, tags=("warning",))
            if once:
                break
            wait = max(120, CHECK_EVERY_MIN * 60 + random.randint(-30, 60))
            nxt = datetime.fromtimestamp(time.time() + wait)
            log(f"Następne sprawdzenie ok. {nxt:%H:%M}. (Ctrl+C = koniec)")
            time.sleep(wait)
        browser.close()


if __name__ == "__main__":
    if "--widoczny" in sys.argv:
        HEADLESS = False
        OFFSCREEN_IF_BLOCKED = False
    if "--test" in sys.argv:
        # test: promień 300 km, więc znajdzie sklep w Pabianicach i wyśle powiadomienie
        MAX_KM = 300
        log("TRYB TESTOWY (300 km) - powinno przyjść powiadomienie na telefon.")
    main()
