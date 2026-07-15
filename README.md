# pelositracker

A tool for tracking U.S. Congress member stock trading disclosures (e.g. periodic transaction reports filed under the STOCK Act).

## Status

Early scaffold — stubs only, no functionality implemented yet.

## Project layout

```
src/pelositracker/
    __init__.py
    scraper.py     # fetches/parses disclosure filings
    models.py      # data models for trades and filers
    storage.py     # persistence layer
    cli.py         # command-line entrypoint
tests/
    test_placeholder.py
```

## Setup

```bash
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
```

## Usage

```bash
python -m pelositracker.cli
```
