# rcm-data

Daily price reader for Rental Community Miami.

Once a day at 7 AM Miami time, GitHub Actions runs `scrape.py`, which:

1. asks the Google Sheet which communities to read
2. reads each community's public page once
3. writes prices, floor plans and facts back into the sheet
4. saves `docs/data.json`, served by GitHub Pages

Every run is a commit, so the history of this repository is a free daily
record of rents.

## What is here and what is not

Here: the scraper code and the published prices, which are public facts.

Not here: the list of source pages, the sheet's address, any token, any lead.
Those live in the private Google Sheet and in this repository's encrypted
Actions secrets (`SHEET_API_URL`, `SHEET_TOKEN`).

## Running it by hand

Actions tab > Actualizar precios > Run workflow. Or, from the sheet,
Rental Concierge > Actualizar precios ahora.
