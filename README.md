# Fénix API Dashboard

Webový dashboard pro analýzu výkonu e-shopu na Zboží.cz prostřednictvím Fénix API (api.sklik.cz).

## Co dashboard umí

- **Přehled položek** – seznam všech nabídek s cenou, doporučenou CPC (`maxCpcSearch`), konkurenčním přehledem a EAN
- **Konkurenční analýza** – počet konkurentů na produktové kartě, nejnižší cena konkurence, porovnání vlastní ceny vůči minimu
- **Statistiky (30 dní)** – zobrazení, kliknutí, CTR, náklady, průměrné CPC, konverze, hodnota konverzí a **PNO**
- **Přehled kategorií** – výkon kategorií (kliknutí, zobrazení, náklady, konverze), průměrný počet eshopů na kartě
- **Analýza parametrů** – porovnání parametrů ve feedu se specifikací Zboží.cz, upozornění na chybějící kritické/důležité parametry dle kategorie
- **Feed info** – stav importu feedu, počet položek, datum poslední úspěšné aktualizace
- **Recenze** – přehled hodnocení zákazníků
- **Doporučení** – automaticky generované tipy pro zlepšení výkonu kampaní

## Technologie

- Python 3.11 + Flask
- Fénix API v1 (api.sklik.cz/v1)
- Bootstrap 5 + Chart.js
- Nasazení: Render.com

## Nasazení na Render

Projekt obsahuje `render.yaml`. Stačí propojit GitHub repo a Render vše nakonfiguruje automaticky.

Start command:
```
gunicorn app:app --bind 0.0.0.0:$PORT --timeout 600 --workers 2 --threads 4
```

## Přihlášení

Pro přístup k API potřebuješ:
- **Fénix token** – refresh token z účtu Sklik/Zboží.cz
- **User ID** – ID uživatele
- **Premise ID** – ID provozovny (e-shopu)

Všechny hodnoty si dashboard ukládá do localStorage pro příští použití.
