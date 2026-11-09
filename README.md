# Magic Stores

A **configurable multi-store engine**. One database schema for every vertical. Categories, products, and property definitions (text, number, enum, image, category reference, …) are **data**, not per-vertical columns. Each store can ship its own templates and styles under `app/storefronts/<slug>/`.

This repository is the base engine only. Business catalog content is loaded later via store packs.

## Stack

- Python 3.9+
- FastAPI + SQLAlchemy 2 + Jinja2
- SQLite by default

License: [MIT](LICENSE)

## Setup

```bash
cd magic_stores
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # optional
```

## Run

From `magic_stores/`:

```bash
uvicorn app.main:app --reload
```

- Home: http://127.0.0.1:8000/
- Health: http://127.0.0.1:8000/api/health

On first start the catalog is empty by design.

## Tests

```bash
pytest -q
```

Tests insert arbitrary verticals (clothing / chargers) through the ORM to prove domain changes do not require schema migrations.

## Schema (summary)

| Table | Role |
| --- | --- |
| `stores` | Store + theme + locale |
| `categories` | Tree per store |
| `property_definitions` | Field schema per category |
| `products` | Items |
| `property_values` | Typed values (JSON) |
| `media_assets` | Media stub |

## Storefronts (per-store front)

```
app/storefronts/
  _default/           # shared base: templates + static + pages/
  <store-slug>/       # optional override (from a store pack)
```

Resolution: use the store file when present; otherwise `_default`.  
Assets: `/stores/<slug>/assets/style.css`  
Extra pages: `/stores/<slug>/pages/<page>`

See `app/storefronts/README.md` and `fixtures/packs/`.

## Packaging

```bash
./scripts/pack_archive.sh
```

Exclude `.git`, `venv`, `__pycache__`, `.env`, and `*.db` from any archive you share.
