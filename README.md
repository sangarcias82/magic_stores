# Magic Stores

A **configurable multi-store engine**. One database schema for every vertical. Categories, products, and property definitions (text, number, enum, image, category reference, …) are **data**, not per-vertical columns. Each store can ship its own templates and styles under `app/storefronts/<slug>/`, and its images are served by a separate static app (`../magic_stores_content`). A store arrives as a **store pack** (JSON + optional `storefront/` and `media/` folders), imported from the command line or uploaded in admin: see [Importing a pack](#importing-a-pack-command-line-or-admin-upload).

The engine has no business content of its own: stores are loaded from store packs. One business store ships with the repository and is there from the first start, without importing anything: **solares** (solar water heaters, `fixtures/packs/solares/`), see "Run".

## Stack

- Python 3.9+
- FastAPI + SQLAlchemy 2 + Jinja2
- SQLite by default

License: [MIT](LICENSE)

## Setup

From the project folder (the one containing `requirements.txt`):

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # optional; see "Before deploying" below
```

## Run

```bash
uvicorn app.main:app --reload
```

- Storefront home: http://127.0.0.1:8000/
- Sign in: http://127.0.0.1:8000/login
- Admin: http://127.0.0.1:8000/admin
- Health: http://127.0.0.1:8000/api/health

On first start the dummy admin account below is created, and the **solares** store is loaded
(http://127.0.0.1:8000/stores/solares): nothing to import by hand.

- Its storefront and images are in the repository, as an import installs them: `app/storefronts/solares/`
  (templates + stylesheet) and `../magic_stores_content/content/solares/` (the 6 JPEGs, served by the content app).
- Its catalog rows (store, categories, the `tubos` / `presion` property definitions, products, and its two shipping
  methods `standard` / `express`, see [Solares shipping methods](#solares-shipping-methods)) live in the database,
  which is not in the repository, so the app loads them at startup from `fixtures/packs/solares/` (setting
  `SEED_PACKS`). That happens only while the store doesn't exist: later starts never change it, so edits made in admin
  stay. It is recorded in `pack_imports` with source `startup` (**Imports** in admin).
- `SEED_PACKS=` (empty) in `.env` starts with an empty catalog; a missing or invalid pack is logged and never stops the
  app from starting.
- Its products come with a **SKU** and a **stock of 1** each (from the pack, logged as a "Pack import" stock movement),
  so they are in stock from the first start. A database that loaded solares before the pack had them: re-import the pack
  once (`python -m app.services.store_pack_import fixtures/packs/solares/solares.store-pack.v1.json`, prints
  `stock: N set, M unchanged`): SKUs are set and every product's stock is set to the pack's 1 (a pack import always
  overwrites the stock of the products it names; to push counted numbers instead, use the
  [inventory import](#inventory-import-csv)).

### Two processes: Magic Stores + the content app

Product files (images, …) are **not** stored in Magic Stores. The sibling app
[`../magic_stores_content`](../magic_stores_content/README.md) serves them. It only serves static files
from `magic_stores_content/content/<store-slug>/`: it has no database, no admin and no pack import.
Magic Stores writes into that folder when it imports a pack's images (`CONTENT_ROOT`, below).
Run both, each from its own folder and in its own terminal:

| Process | Folder | Command | URL |
| --- | --- | --- | --- |
| Magic Stores | `magic_stores/` | `uvicorn app.main:app --reload` | http://127.0.0.1:8000 |
| Content app | `magic_stores_content/` | `uvicorn content_app.main:app --host 127.0.0.1 --port 8001` | http://127.0.0.1:8001/content/<store-slug>/<file> |

How they connect:

- `CONTENT_BASE_URL` (setting / `.env`, default `http://127.0.0.1:8001`) is where the content app can be reached.
- `CONTENT_ROOT` (default: `../magic_stores_content/content`, as an absolute path computed from this project) is the
  folder the content app serves, i.e. its `CONTENT_DIR`; both must be the same folder. Importing a pack from its folder
  publishes its `media/` images to `CONTENT_ROOT/<store-slug>/` (see "Store packs" below). Empty: nothing is published.
- An image value stored as a **content path**, `/content/<store-slug>/<file>`, is shown on the product page as
  `CONTENT_BASE_URL` + that path, e.g. `/content/demo-widgets/widget-blue.svg` →
  `<img src="http://127.0.0.1:8001/content/demo-widgets/widget-blue.svg">`. The path is the content app's own
  URL path, so the file is `magic_stores_content/content/demo-widgets/widget-blue.svg`.
- Absolute `http(s)://…` URLs pass through unchanged. Other `/path` values (`/static/…`, `/stores/<slug>/assets/…`)
  are this site's own files and also stay unchanged.
- `CONTENT_BASE_URL` must be an `http(s)` URL (a trailing `/` is dropped) or empty. An invalid value stops the app at
  startup. Leave it empty when a reverse proxy serves `/content/` on the same host as Magic Stores: content paths
  then stay relative.
- Pages don't check that the file exists (imports do: the images a pack uses but that aren't in `CONTENT_ROOT` are
  reported, see "Importing a pack"). If the content app is down, product pages still load, but their images are broken.
- The same goes for the store logo, `theme.logo_url` (e.g. `/content/<store-slug>/logo.png`), shown in the header of
  every storefront page.
- Templates: `media_url(path)` is available in every storefront template (`app/services/media.py`). A store's own
  `product.html` / `base.html` override must call `{{ media_url(prop.value.media_path) }}` /
  `{{ media_url(theme.logo_url) }}` too.
- Only storefront pages convert content paths. The admin product form thumbnail and the JSON API
  (`/api/…`) still show the stored value (`/content/…`) as it is.

Try it: with both apps running, upload a `.zip` of `fixtures/packs/storefront-demo/` at `/admin/packs`
(`cd fixtures/packs && python -m zipfile -c /tmp/storefront-demo.zip storefront-demo/`), or import it on the command
line (`python -m app.services.store_pack_import fixtures/packs/storefront-demo/storefront-demo.store-pack.v1.json`).
Its images are published to `magic_stores_content/content/storefront-demo/`; open
http://127.0.0.1:8000/stores/storefront-demo: the logo and the product image load from port 8001. (Uploading only
the `.json` publishes no images: the page then shows broken images, and the upload message lists them.)

From the workspace folder (the parent of `magic_stores/`), `./for_testing` (or `sh for_testing`) runs the test
suites (Magic Stores, the content app, then the cart's JavaScript with Node: `node --test tests/js/*.test.js`) and then
starts both apps (8000 + 8001). Press Ctrl+C to stop both. `./for_testing --cov` runs every suite with coverage (the
Node step enforces 90 % of lines, branches and functions of `app/static/cart/cart-core.js`). Without `node` the
JavaScript step is skipped with a message (the apps don't need Node). It uses `$PYTHON` if set, else the active venv, `magic_stores/venv` or `.venv`, or
`python3`, whichever has the packages; if none does, it creates `magic_stores/venv` and installs both
`requirements.txt` files into it (first run only; needs `python3-venv` and network access).

## Settings

From `.env` (copy `.env.example`) or the environment; environment variables win. Read once at startup.

| Setting | Default | What it is |
| --- | --- | --- |
| `APP_NAME` | `Magic Stores` | Name shown by `/api/health` and the API title |
| `DATABASE_URL` | `sqlite:///./store_engine.db` | SQLAlchemy URL (relative SQLite paths are relative to the current folder) |
| `SECRET_KEY` | placeholder (warns at startup) | Signs the session cookie; set a real one before deploying |
| `SESSION_HTTPS_ONLY` | `false` | `true` behind HTTPS: the session cookie gets the `Secure` flag |
| `SEED_ADMIN_EMAIL`, `SEED_ADMIN_PASSWORD` | dummy admin | Admin created at startup if missing (see "Admin access") |
| `CONTENT_BASE_URL` | `http://127.0.0.1:8001` | Where the content app is reached; `/content/…` image values and logos are shown with it. Empty: keep them relative |
| `CONTENT_ROOT` | `../magic_stores_content/content` (absolute) | The content app's folder (its `CONTENT_DIR`): pack imports publish `media/` images there. Empty: don't publish |
| `SEED_PACKS` | `fixtures/packs/solares/solares.store-pack.v1.json` | Pack files (comma-separated, relative to this project) loaded at startup when their store doesn't exist yet; existing stores are never changed. Empty: start with an empty catalog |
| `CART_REVALIDATE_MINUTES` | `30` | Minutes a cart line keeps the name, SKU and price it was added with before it is validated again (repriced, quantity checked against the stock); `0` = on every request, at most 10080 (a week). See [Cart (public)](#cart-public) |
| `PAYMENT_CALLBACK_SECRET` | empty | Secret the payment terminals sign their callbacks with (HMAC-SHA256; at least 32 characters, else the app doesn't start). Empty: payment callbacks are refused (`503`). See "Terminal callback" |

The content app has one setting of its own, `CONTENT_DIR` (default: its `content/` folder); keep it equal to `CONTENT_ROOT`.

## Admin access

The public storefront (`/`, `/stores/…`) and the JSON API (`/api/…`) need no login. Everything under `/admin` requires a signed-in **admin**.

### Dummy admin credentials (development only)

On every startup the app creates this admin **if no user with that email exists yet**:

| Email | Password |
| --- | --- |
| `admin@example.com` | `ChangeMeAdmin123!` |

These are **dummy credentials** for local development. Never keep them on a shared or public server.

- Choose your own with `SEED_ADMIN_EMAIL` / `SEED_ADMIN_PASSWORD` in `.env` **before the first start**.
- The seed never overwrites an existing user: changing `SEED_ADMIN_PASSWORD` after the admin exists has no effect. There is no password-change page yet; to change the password of an existing account, run from the project folder (with the same `.env` / `DATABASE_URL` as the app):

  ```bash
  python -c "from app.db import SessionLocal; from app.auth import get_user_by_email, hash_password; db = SessionLocal(); u = get_user_by_email(db, 'admin@example.com'); u.password_hash = hash_password('A-New-Strong-Passw0rd'); db.commit(); print('password updated')"
  ```

### `/login` (sign in)

- `GET /login` shows the sign-in form (email + password).
- `POST /login` with form fields `email`, `password` and optional `next`:
  - **Success:** `303` redirect to `next` if it is a path on this site, otherwise to `/admin` (admins) or `/` (other users). A session cookie is set.
  - **Failure** (wrong password, unknown email, empty field): `401` with the form and the message "Invalid email or password." (the same message in every case, so it doesn't reveal which emails exist). No session is created.
- Emails are case-insensitive (`Admin@Example.com` works).
- `next` only accepts local paths (`/admin/stores`), never other sites (`https://…`, `//…`).

### `/admin` (who can open it)

| Visitor | Any `/admin…` page or form |
| --- | --- |
| Not signed in | `303` redirect to `/login?next=<page>`; after signing in you land on that page |
| Signed in, role `customer` | `403 Forbidden` |
| Signed in, role `admin` | allowed |

The check runs before anything else, so a blocked visitor can't learn whether a store, category or product exists. Admin pages are sent with `Cache-Control: no-store`, so the browser's back button doesn't show them after signing out.

### Sign out

The **Sign out** button in the header (`POST /logout`) clears the session and redirects to `/`. `GET /logout` works too (e.g. from the address bar).

### Sessions and passwords

- The session is a cookie named `magic_stores_session` that only holds the user id, signed with `SECRET_KEY` (a cookie changed or forged without the key is ignored). It is `HttpOnly`, `SameSite=Lax`, lasts 14 days, and gets the `Secure` flag when `SESSION_HTTPS_ONLY=true`.
- Passwords are never stored in plain text: `users.password_hash` holds a salted PBKDF2-HMAC-SHA256 hash (600,000 iterations, Python standard library). Hashes created by earlier versions (scrypt) are still accepted where Python supports scrypt.
- Roles are `admin` and `customer`. There is no sign-up page; accounts other than the seeded admin are created in code (`app.auth.create_user`).

### Before deploying

Set these in `.env` (or the environment):

| Setting | Why |
| --- | --- |
| `SECRET_KEY` | Signs the session cookie. The placeholder value logs a warning at startup; anyone who knows the key can forge sessions. |
| `SEED_ADMIN_EMAIL`, `SEED_ADMIN_PASSWORD` | Replace the dummy admin **before the first start** (see above). |
| `SESSION_HTTPS_ONLY=true` | Behind HTTPS: the cookie is only sent over HTTPS. |
| `CONTENT_BASE_URL` | Public URL of the content app (e.g. `https://media.example.com`), or empty if `/content/` is proxied on this host. The default `http://127.0.0.1:8001` only works on your own machine. |
| `PAYMENT_CALLBACK_SECRET` | Only the terminals may know it: anyone with it can mark payments succeeded. Generate it (`python3 -c 'import secrets; print(secrets.token_hex(32))'`) and keep it out of the repository. |
| `CONTENT_ROOT` | The content app's folder (its `CONTENT_DIR`), where pack imports publish images. The default assumes both projects side by side; set it when the content app's folder lives elsewhere (e.g. a shared volume). |

### Known limitations

- No CSRF tokens on forms yet; the `SameSite=Lax` cookie blocks cross-site form posts in current browsers.
- No rate limiting or lockout after failed sign-in attempts.
- No sign-up, password-change or password-reset pages.

### Signing in with curl

The examples below use a cookie jar for the session:

```bash
curl -c jar.txt --data-urlencode "email=admin@example.com" \
     --data-urlencode "password=ChangeMeAdmin123!" http://127.0.0.1:8000/login   # 303 -> /admin
curl -b jar.txt http://127.0.0.1:8000/admin                                      # 200
```

Sign out when you are done: `curl -b jar.txt -c jar.txt -X POST http://127.0.0.1:8000/logout`.

## Admin pages

Each store in admin has tabs: **Categories · Products · Inventory · Theme · Shipping · Payment terminals · Try payment · Payments · Translations · View storefront**.

### Stores

| Page | Purpose |
| --- | --- |
| `/admin` | Dashboard |
| `/admin/stores` | List stores |
| `/admin/stores/new` | Create a store (slug, name, currency, default storefront language); it starts with the default theme |

Slugs are lower-case letters, digits and single hyphens (e.g. `demo-widgets`; `new` is reserved); currency is a 3-letter ISO code.

```bash
curl -b jar.txt -d "slug=demo-widgets" --data-urlencode "name=Demo Widgets" \
     -d "currency=USD" http://127.0.0.1:8000/admin/stores      # 303 -> /admin/stores
curl http://127.0.0.1:8000/api/stores                        # includes demo-widgets
```

### Categories and property definitions

| Page | Purpose |
| --- | --- |
| `/admin/stores/<slug>` | Category tree of the store |
| `/admin/stores/<slug>/categories/new` | Create a category (name, slug, parent, description, sort order) |
| `/admin/stores/<slug>/categories/<id>` | Edit the category and list its property definitions |
| `/admin/stores/<slug>/categories/<id>/properties/new` | Add a property definition |
| `/admin/stores/<slug>/categories/<id>/properties/<def-id>` | Edit a property definition |
| `…/delete` (GET confirm, POST delete) | Delete a category or property definition |

Property types: `text`, `number`, `boolean`, `enum`, `image`, `category_ref`. Enum options are entered one per line and stored as `config = {"options": [...]}`; `category_ref` can optionally point at a category of the same store (`ref_category_id`).

Rules:

- A category can't be deleted while it has products (delete them first) or subcategories (move or delete them first). Deleting a category also deletes its property definitions.
- Deleting a property definition also deletes the product values stored for it.
- A property's type is locked once products have values for it.
- Edit forms replace every field: when posting with curl, send all fields (an omitted `sort_order` becomes 0, an omitted `required` means not required).

```bash
S=http://127.0.0.1:8000/admin/stores/demo-widgets
curl -b jar.txt -d "slug=widgets" -d "name=Widgets" $S/categories   # 303 -> $S/categories/<id>
curl -b jar.txt -d "key=model" -d "label=Model" -d "prop_type=text" -d "required=1" \
     $S/categories/1/properties
curl -b jar.txt -d "key=color" -d "label=Color" -d "prop_type=enum" \
     --data-urlencode $'options=Red\nBlue\nGreen' $S/categories/1/properties
curl http://127.0.0.1:8000/api/stores/demo-widgets/categories   # types + config.options
```

### Products

| Page | Purpose |
| --- | --- |
| `/admin/stores/<slug>/products` | Product list (filter by category) |
| `/admin/stores/<slug>/products/new?category_id=<id>` | New product; without `category_id` you pick the category first |
| `/admin/stores/<slug>/products/<id>` | Edit product |
| `/admin/stores/<slug>/products/<id>/delete` | Delete (GET confirm, POST delete) |

A product belongs to one category (fixed after creation). Its form has one field per property definition of that category, posted as `prop_<key>`. Values are stored as typed JSON in `property_values`:

| Type | Form value | Stored JSON |
| --- | --- | --- |
| text | any text | `{"value": "…"}` |
| number | `20`, `2.5` | `{"value": 20}` / `{"value": 2.5}` |
| boolean | `true` / `false` (also yes/no, 1/0) | `{"value": true}` |
| enum | one of the options | `{"value": "Blue"}` |
| image | `https://…` URL or `/path`; product files: `/content/<store-slug>/<file>` (content app, see "Two processes") | `{"media_path": "…"}` |
| category_ref | category id (the referenced category or its subcategories) | `{"category_id": 2}` |

An empty field means "no value" (required properties must be filled). `price` is in currency units (`19.99`). `status` is `active` (default) or `hidden`; hidden products are not shown on the storefront or in the public API.

**SKU** (`sku`, column `products.sku`): the store's own code for the product, a generic retail field like the price
(not a property definition). Rules of the admin form, on create **and** edit:

| Case | Result |
| --- | --- |
| Empty, only spaces, or the field not sent | `400`, "SKU is required." next to the field; nothing saved |
| Not letters / digits / `-` `_` `.` (ASCII), not starting with a letter or digit, spaces inside, over 64 characters | `400`, the allowed format next to the field |
| Already used by another product **of this store** (case doesn't matter) | `400`, `This store already has a product with SKU WB-100-M: "Widget Blue M" (widget-blue-m).` |
| The same SKU in **another** store | Allowed: SKUs are per store |

- Stored trimmed and upper-case (`wb-100-m` → `WB-100-M`), so `wb-100-m` and `WB-100-M` are the same SKU. The
  database enforces it too (`UNIQUE (store_id, sku)`), so two admins saving at the same moment can't both get it.
- Shown in the admin product list (**SKU** column) and on the product's admin page (form + summary line). Admin only:
  not on the storefront nor in the JSON API (see [Product API (public)](#product-api-public)). A store pack may give
  products a SKU (optional `sku`, same rules; solares gives all of its products one, e.g. `SOL-8T-ALTA`). Products
  without one ("(not set)" in the list) need one at their next save in admin. A re-import of a pack that doesn't give
  a product a SKU keeps the one set in admin (see "Re-import" in `fixtures/packs/README.md`).
- Databases created before the column existed get it at the next start (`app/schema_upgrades.py`), with no SKU on
  existing products.

**Stock** (`stock`, column `products.stock_on_hand`): units on hand, a generic retail field like the SKU. Set in the
admin form on create and edit; shown in the admin product list (**Stock** column, "Out of stock" next to 0) and on the
product's admin page: the form, and a summary line above it with the **saved** SKU, stock and what the public API
reports (`In stock` / `Out of stock`), so a rejected edit still shown in the form can't be mistaken for the saved value.

| Case | Result |
| --- | --- |
| `0`, `12`, … up to 2,147,483,647 (spaces around are ignored) | Saved |
| Negative (`-1`) | `400`, "Stock cannot be negative." next to the field; nothing saved |
| Empty, or the field not sent | `400`, "Stock is required (0 or more)." (the new-product form starts at `0`). Edits replace every field, so a post without `stock` is refused instead of setting it to 0 |
| Decimals, separators or other text (`2.5`, `1,000`, `+5`, `ten`) | `400`, "Stock must be a whole number like 12 (no decimals or separators)." |
| Over 2,147,483,647 | `400`, "Stock is too large (at most 2,147,483,647)." |

- The database refuses negatives too (`CHECK (stock_on_hand >= 0)`).
- Under Stock, **Low-stock threshold (optional)**: the product's own threshold for the low-stock list (empty = the
  store's); see [Low stock (admin)](#low-stock-admin).
- Products created without it have `0`: older databases (upgraded at the next start) and packs without the optional
  `stock` field. **Solares** gives each product a stock of **1** (and a SKU) in its pack. A pack's `stock` wins like
  its other fields: **every import of the pack sets the stock** of the products it names to the pack's number (logged
  as **Pack import** movements), all or nothing with the rest of the pack (see "Re-import" in
  `fixtures/packs/README.md`). To set only stock numbers, use the [inventory import](#inventory-import-csv).
- Editing the number in the form is logged as a stock movement (see below). The form remembers the stock it showed
  (hidden `stock_seen`): saving with the field unchanged leaves the stock alone, so an adjustment made after the form
  was opened is never overwritten; a *changed* field on such a form is refused (`409`, "The stock changed to N after
  you opened this page…") with nothing saved, and saving that page again applies it. Clients that don't send
  `stock_seen` (curl) set the number as given.
- Public side: only the flag `in_stock` (stock > 0) in the JSON API, never the count; see
  [Product API (public)](#product-api-public). The storefront pages don't show it yet. Nothing reserves or decrements
  the stock (there are no orders).

```bash
curl -b jar.txt -d "category_id=1" -d "slug=widget-blue-m" -d "sku=WB-100-M" --data-urlencode "name=Widget Blue M" \
     -d "price=19.99" -d "stock=12" -d "prop_model=WB-100" -d "prop_color=Blue" $S/products
curl http://127.0.0.1:8000/api/stores/demo-widgets/products/widget-blue-m
```

The list also has a **SKU contains** search and an **Export inventory (CSV)** button: see [Inventory](#inventory).

#### Stock adjustments and the movement log

Besides setting the number, an admin **adjusts** the stock by a signed amount with a short reason ("+12, received from
supplier", "-1, broken in transport"), without opening or saving the rest of the product:

| Where | How |
| --- | --- |
| Product list (`/admin/stores/<slug>/products`) | Column **Adjust**: **Adjust stock** opens a small form in the product's row: **Change (+/−)** (a number with − / + buttons), **Reason**, **Save**. Back on the list (same category filter, scrolled to the row) with "Stock of "…": 10 → 15 (+5)." |
| Product page (`/admin/stores/<slug>/products/<id>`) | Section **Stock** (link "Adjust stock" in the summary line): the same form, and the **Stock movements** log |
| HTTP | `POST /admin/stores/<slug>/products/<id>/stock` with `delta`, `reason` (and `return=list` to come back to the list, optionally with its `category_id`) |

| Case | Result |
| --- | --- |
| `delta` a whole number other than 0 (`5`, `+5`, `-2`), `reason` 1 to 200 characters | The stock changes by `delta`; a movement is logged; `303` |
| The result would be **below zero** (stock 2, `delta=-3`) | **Refused**: `400`, "Refused: the stock would go below zero (2 -3 = -1). Nothing was changed." in the row / form, which stays open with what was typed |
| Over 2,147,483,647 | Refused the same way ("…would be too large…") |
| `delta` empty, `0`, not a whole number; `reason` empty or over 200 | `400` with the field's message; nothing changed |

- The refusal can't be raced: the change is one conditional `UPDATE … SET stock_on_hand = stock_on_hand + delta WHERE
  stock_on_hand + delta >= 0`, so two admins removing the last units at the same moment can't both succeed, whatever
  their pages showed. The database refuses a negative stock too.
- **Movement log** (table `stock_movements`): one row per change made by the app, written in the same transaction as the
  change: when, type, change (`+5` / `-2`), stock after, reason, and the admin. Types: **Initial stock** (product created
  with stock > 0), **Set in the product form**, **Adjustment** (with its reason), **Pack import** (a store pack set
  it), **Inventory import** (a row of an [inventory CSV](#inventory-import-csv); the reason is the file name), **Order** (a
  [checkout](#checkout)'s payment succeeded and took the units; the reason is the order's id). The product page shows the latest 50, newest first. Deleting a product deletes its log.
- Messages are in the page language (`admin.stock.*`). Code: `app/services/stock.py`.

```bash
S=http://127.0.0.1:8000/admin/stores/solares/products
curl -b jar.txt -d "delta=+5" --data-urlencode "reason=Received from supplier" $S/<id>/stock       # 303
curl -b jar.txt -d "delta=-99" --data-urlencode "reason=Broken" "$S/<id>/stock?lang=en"            # 400, refused
```

Public page: http://127.0.0.1:8000/stores/demo-widgets/p/widget-blue-m

### Shipping methods

| Page | Purpose |
| --- | --- |
| `/admin/stores/<slug>/shipping` | The store's shipping methods, active and inactive: name, code, rate, free-from amount, countries, order, status |
| `/admin/stores/<slug>/shipping/new` | Create a method |
| `/admin/stores/<slug>/shipping/<id>` | Edit a method |

Fields (what the [quote API](#shipping-quotes) uses):

| Field | Form value | Rule |
| --- | --- | --- |
| `name` | text | Required, at most 200 characters |
| `code` | `standard`, `express-24h` | Required; lower-case letters, digits and single hyphens; unique in the store. The quote API takes it: changing it breaks callers using the old one |
| `rate` | `4.95` | Required, ≥ 0, at most 2 decimals, in the store's currency. **Dot for decimals**: `4,95` is refused with a message rather than guessed (it means 4.95 in Spanish, 495 in English) |
| `free_from` | `50` or empty | Free-shipping threshold (`free_shipping_threshold_cents`); empty = never free |
| `countries` | `ES, PT` or empty | 2-letter codes separated by commas, semicolons or spaces, stored upper-case without duplicates; empty = every country |
| `sort_order` | `0` | Whole number, lower first (same range as categories) |
| `is_active` | checkbox | Unticked = inactive: not listed by the API and refused by quotes (`inactive_method`) |

There is no delete: untick **Active** instead, so callers that still send the code get a clear `inactive_method`.
Validation messages are in the page language (`admin.shipping.error.*` in `app/locales/`), like every admin form's.

```bash
S=http://127.0.0.1:8000/admin/stores/solares
curl -b jar.txt --data-urlencode "name=Warehouse pickup" -d "code=pickup" -d "rate=0" \
     -d "countries=MX" -d "sort_order=2" -d "is_active=1" $S/shipping          # 303 -> $S/shipping
```

### Payment terminals (admin)

| Page | Purpose |
| --- | --- |
| `/admin/stores/<slug>/terminals` | The store's terminals: display name, code, type (mock / bank transfer (offline)), enabled or disabled, with an **Enable** / **Disable** button per row |
| `/admin/stores/<slug>/terminals/<id>` | Edit the display name (required, at most 200 characters) and the **Enabled** checkbox; for a bank transfer terminal also its **deposit details** (bank, beneficiary, CLABE, account, note for the shopper; see [Bank transfer (offline)](#bank-transfer-offline)) |
| `…/terminals/<id>/enable`, `…/terminals/<id>/disable` (POST) | The list's buttons. Each sets the state (doing it twice is harmless), so a double submit can't flip it back |

- **Disabled** = no new payments through it: the payments API refuses it (`422`, `terminal_disabled`), and **Try payment**
  doesn't offer it and refuses it if an older page still sends it. The public terminal list shows it with
  `"enabled": false`. With every terminal disabled, the list warns that the store can't take payments.
- Payments already pending on a terminal can still get their answer after it is disabled (its signed callback, or the
  simulator for mock terminals): disabling stops new payments, it doesn't strand the ones in progress.
- The **code** can't be changed: the API and the signed callbacks use it, and its `mock_` prefix is what allows the
  admin simulator. Terminals are not created or deleted here (every store has the mock defaults; a store pack adds
  others, e.g. solares' `bank_transfer`).
- Bank details are checked when saved: bank and beneficiary required (at most 100 / 200 characters), the CLABE must be
  18 digits with a valid control digit (spaces and hyphens are dropped), the account number is optional (6 to 20
  digits), the note at most 500 characters. Each problem is shown under its field and nothing is saved. A bank transfer
  terminal with incomplete details refuses new payments (`terminal_not_configured`). Changing the details doesn't
  change what earlier shoppers were told: each payment keeps a copy of its instructions.
- Admin only (`/login` first, `403` for customers), Spanish by default like the other admin pages.

### Payments (admin list)

`/admin/stores/<slug>/payments` (tab **Payments**): the store's payments, most recent first, 50 per page (`?page=2`, …).
One row per payment with its id (the random `pay_…`, linking to its page in **Try payment**, or to the payment page
below for a bank transfer), amount, currency, terminal, status
(pending / approved / declined) and created / updated times (UTC); a bank transfer also shows its 7-digit transfer
reference. The list itself changes nothing: a mock payment's outcome comes from its terminal (see
[Payments (mock)](#payments-mock)); a bank transfer's from an admin, on its payment page:

| Page | Purpose |
| --- | --- |
| `/admin/stores/<slug>/payments/<id>` | The payment (`<id>` = its `pay_…` id): status, amount, terminal, reference, its order's lines and shipping, the deposit instructions the shopper got, and who decided it, when and with what note. A pending bank transfer has the **Confirm deposit** / **Reject** form; a mock payment links to **Try payment** |
| `/admin/stores/<slug>/payments/<id>/confirm` (POST, optional `note`) | The deposit arrived: the payment becomes `succeeded` (the order is paid and its stock taken); `303` back to the page |
| `/admin/stores/<slug>/payments/<id>/reject` (POST, optional `note`) | No (valid) deposit: the payment becomes `failed` (the order takes no stock); `303` back |

- Only for bank transfer payments: confirming a mock payment is refused (`409`, "answered by its terminal").
- The note (at most 200 characters, e.g. the SPEI tracking key) and the admin are stored on the payment
  (`decided_by_id`, `decision_note`). Confirming twice is harmless; the other answer afterwards is refused (`409`,
  the page shows the status it already has). Admin only (`/login` first,
`403` for customers), Spanish by default. A `?page=` that isn't a page number shows page 1; very large ones are capped
(`MAX_PAGE`, shared with the Imports list), so they show an empty page instead of failing.

### Try payment (operator demo)

`/admin/stores/<slug>/try-payment` (tab **Try payment**; admin is in Spanish unless `?lang=en`) connects the
[shipping quote](#shipping-quotes) and the [mock payments](#payments-mock) for a demo, in the store's currency:

1. **Quote**: order subtotal, an active shipping method, destination country (optional unless the method lists
   countries) → subtotal + shipping = **total** (shipping is 0 from the free-shipping amount).
2. **Pay**: choose an enabled **mock** terminal → a `pending` payment of the total. The server quotes again at this moment: the
   amount never comes from the page, so a rate changed in between is applied.
3. **Answer**: **Simulate approved payment** / **Simulate declined payment** (optional mock reference) → `succeeded` /
   `failed`. No card network is called. The payment page links to the same payment in the JSON API.

| Page | Purpose |
| --- | --- |
| `/admin/stores/<slug>/try-payment` | Steps 1 and 2: the quote form (`?subtotal=…&method=…&country=…`), then the quote and the pay form |
| `/admin/stores/<slug>/try-payment/pay` (POST) | Quote again and create the `pending` payment; `303` to its page |
| `/admin/stores/<slug>/try-payment/<id>` | Step 3: the payment (`<id>` = its random `pay_…` id) and, while pending, the answer buttons |
| `/admin/stores/<slug>/try-payment/<id>/succeed`, `/admin/stores/<slug>/try-payment/<id>/fail` (POST) | The simulated answer (optional `mock_reference`); `303` back to the payment page |

The demo works **only with mock terminals** (code `mock_…`): only enabled mock terminals are offered, and a
`bank_transfer` (or any other non-mock) terminal sent anyway is refused (`400`, "Try payment only creates
payments through mock terminals", nothing created): a demo never creates a real payment nor deposit instructions. With no enabled mock
terminal the page says so. The simulated answer is an admin-only stand-in for the terminal, allowed only for mock
terminals too: a payment through any other terminal shows "Real terminal" (a bank transfer is confirmed on its
[payment page](#payments-admin-list); a gateway answers with its [signed callback](#terminal-callback)). The JSON
API has no such route: there, only the callback sets the outcome.

Demo on solares (MXN), after signing in. Solares ships its shipping methods (`standard`, `express`), but its mock
terminals **disabled** (it takes payments by bank transfer only), so first enable one: **Payment terminals** tab →
**Enable** on "Mock card" (disable it again afterwards):

1. `/admin/stores/solares/try-payment`: subtotal `5199` (the gas boiler), the standard method, country `MX`,
   **Quote** → MXN 5,199.00 + MXN 450.00 = total MXN 5,649.00. (Subtotal `13499`: shipping is free; the express
   method: MXN 1,200.00, never free.)
2. **Create payment of MXN 5,649.00** → payment page (pending) → **Simulate approved payment**.
3. **View in the API (JSON)** on the payment page (admin session) → `"amount_cents": 564900, "currency": "MXN",
   "status": "succeeded"`.

Code: `app/services/checkout_demo.py` (quote + pay, reusing the shipping and payment services) and
`app/routers/admin_try_payment.py`. A payment keeps only the total (no orders yet: the subtotal / shipping split is
not stored).

### Theme

`/admin/stores/<slug>/theme` edits the store's primary, secondary and accent colors (hex, e.g. `#1a5f4a`). The storefront renders them as the CSS variables `--color-primary`, `--color-secondary`, `--color-accent`, which `style.css` uses.

```bash
curl -b jar.txt --data-urlencode "primary=#aa0000" --data-urlencode "secondary=#fff8e1" \
     --data-urlencode "accent=#0044cc" $S/theme
curl http://127.0.0.1:8000/api/stores/demo-widgets        # "theme": {"primary": "#aa0000", …}
```

### Import a store pack (upload)

The upload is one of the two ways to import a pack; [Importing a pack](#importing-a-pack-command-line-or-admin-upload) compares it with the command line.

`/admin/packs` (admin only; linked from the admin menu and the dashboard) uploads a store pack file (`.json`, at most 5 MB) **or a `.zip` of the pack folder** (at most 50 MB) and imports it. The whole file is checked before anything is written, and the import is all-or-nothing.

**Images need the `.zip`.** A `.json` is only the pack file: its `media/` images and `storefront/` folder are not in it, so they can't be published or copied. Upload a `.zip` of the pack folder instead (e.g. `cd fixtures/packs && python -m zipfile -c /tmp/storefront-demo.zip storefront-demo/`, or "Compress" the folder): it is unpacked safely into a temporary folder and imported from there, exactly like the command line, so `storefront/` is copied and `media/` is published to the content app. The zip must hold one `*.store-pack.v1.json` at its top or in its single top-level folder; `__MACOSX/` and hidden files are ignored; unsafe entries (`..`, absolute paths, symbolic links), more than 2,000 entries or more than 200 MB once unpacked are refused (`400`, nothing imported).

After any import, the images the pack uses (`/content/…` image values and `logo_url`) are checked against the content folder. If some are missing, they would show as broken images, so the success message lists them, e.g. after a `.json` upload whose images were never published.

| Result | Status | What you see |
| --- | --- | --- |
| Valid pack, new store | `303` to `/admin/stores/<slug>` | Message with the number of categories, properties and products imported |
| No file chosen | `400` | The form with an error |
| Invalid file (not JSON, bad `version`, missing `store.slug`, broken rules, …) or unusable `.zip` (see above) | `400` | The form with every problem found, each with its location (e.g. `store.slug: required`, `zip: unsafe path in the zip: '../x.png'`) |
| File larger than 5 MB (`.json`) or 50 MB (`.zip`) | `413` | The form with an error |
| The pack's store already exists and **Update the store if it already exists** is not ticked | `409` | The form with an error (nothing imported) |
| The pack conflicts with current data (e.g. a property type change while other products use it) | `409` | The form with the conflicts |
| Unexpected error during the import (a bug, the database going away, …) | `500` | The form with the number of the import record that holds the details |

A `.json` upload has no folder next to it, so a pack's storefront folder (`storefront.copy_from_pack_dir`) can't be copied and its `media/` images can't be published: the success message says so (and lists the images that are missing from the content folder), and the store keeps its current storefront. Upload a `.zip` of the pack folder, or import the pack file from its folder on the command line (see "Storefronts" and "Store packs").

If the catalog was imported but its files could not be installed afterwards (e.g. the disk is full), the upload still reports the import (`303`), and the message adds "some files could not be installed: …" with the reason; fix the cause and upload again with the update box ticked.

In every error case no catalog data is written (only the attempt's `pack_imports` record, see below), and existing stores are left exactly as they were. With the update box ticked, the pack updates the existing store following the re-import rules in `fixtures/packs/README.md` (upsert: what the pack names is overwritten, everything else is kept). Re-uploading the same file is safe: without the box it is refused with "this exact file was already imported (record #N)"; with it, the result is identical to the first upload. See "Uploading the same pack again" in `fixtures/packs/README.md`. The page is in Spanish by default (`?lang=en` for English). The per-problem details come from the validator and are in English, like the admin forms' field errors.

#### Import records (`pack_imports`)

Every import attempt (an upload with a file, or the command line) is stored as one row in `pack_imports`, with its result:

| Column | Content |
| --- | --- |
| `status` | `succeeded`, `invalid`, `too_large`, `refused` (store exists, update not ticked), `conflict` (clashes with current data, or the pack's storefront / media folder is missing or unsafe), `failed` (unexpected error), or `running` |
| `started_at`, `finished_at` | UTC times; `finished_at` is empty while `running` |
| `source`, `filename`, `user_id` | `upload` (with the admin who uploaded) or `cli` (the file path) |
| `file_bytes`, `file_sha256` | Size and hash of the file (empty for `too_large`: it is not read) |
| `update_existing` | Whether updating an existing store was allowed |
| `store_slug`, `store_id`, `store_created` | The pack's store; `store_id` / `store_created` only after a successful import |
| `report` | Counts of a successful import: categories / properties / products created and updated, values, translations, `storefront_files` copied, `media_files` published, `missing_media` (images the pack uses that aren't in the content folder), `file_problems` (files not installed after the commit), `skipped` parts |
| `issues` | Expected failures (`invalid`, `conflict`): a list of `{"path", "message"}` |
| `exception` | **Unexpected disruption** (`failed`): `Type: message` and the full traceback |

The row is committed as `running` *before* the import starts, and finished in a separate transaction afterwards. So it survives the rollback of a failed import: the data stays untouched, and the record says what happened. A row left as `running` means the process stopped in the middle of an import (e.g. it was killed); since the import is one transaction, none of its data was written. If the record itself can't be saved, the import result still stands and the problem is written to the application log. Rows are never deleted automatically.

Admin screens (admin only; **Imports** in the admin menu and on the dashboard; Spanish by default):

| Page | Shows |
| --- | --- |
| `/admin/imports` | Every attempt, latest first: number, start time (UTC), status, store, file, source, user. Filter with `?status=<status>`; 50 per page (`?page=2`, …) |
| `/admin/imports/<id>` | One attempt: times and duration, source, user, file name / bytes / SHA-256, store (with links to its admin page and storefront), the counts of a successful import, the list of issues, and for an unexpected error the exception with its traceback (escaped, admin only). Unknown id: `404` |

When an upload fails after it was recorded (invalid, too large, refused, conflict, unexpected error), the error on `/admin/packs` links to its record. From code: `list_imports(db, status=…)`, `get_import(db, id)`, `recent_imports(db)` in `app/services/pack_import_log.py`.

```bash
curl -b jar.txt -F "pack=@fixtures/packs/clothing-demo.store-pack.v1.json" http://127.0.0.1:8000/admin/packs
# 303 -> /admin/stores/clothing-demo ; add  -F update_existing=1  to update an existing store
```

## Language (i18n)

The UI is **Spanish first**: admin, sign-in, the platform home and the public storefront chrome (navigation, buttons, labels, flash messages) are in Spanish unless another language is chosen.

How the language of a page is chosen (first match wins; codes without a catalog are skipped):

1. `?lang=<code>` in the URL, e.g. `/admin?lang=en`. On a page view (GET) a valid choice is remembered in the `magic_stores_lang` cookie, so following links keeps it; `?lang=es` switches back. A form POST with `?lang=` is answered in that language but sets no cookie.
2. The remembered `magic_stores_lang` cookie.
3. On a store's public pages (`/stores/<slug>/…`) only: that store's `locale_default`, chosen when the store is created (default `es`). Admin pages ignore it.
4. `es`.

So an unknown code (`?lang=fr`) falls back to the store's language on its storefront, and to Spanish everywhere else. Codes are matched on the base language: `en-US`, `EN` and `en_GB` all mean `en`. The browser's `Accept-Language` header is deliberately not used, so the default stays Spanish. The header has a language switcher listing each available language under its own name.

Admin form validation messages follow the page language too: the services of every admin form (store, theme, category, property definition, product, stock adjustment, shipping method, payment terminal, Try payment) return error **codes**, and the page shows them in its language (`admin.form.error.*` for store / theme / category / property, `admin.product.error.*`, `admin.stock.error.*`, `admin.shipping.error.*`, …). E.g. a negative stock is "Las existencias no pueden ser negativas." on a Spanish page and "Stock cannot be negative." with `?lang=en`; a duplicate category slug is "Esta tienda ya tiene una categoría con este slug." / "This store already has a category with this slug.". Still English: the store-pack validator's per-problem details (shown on `/admin/packs`) and command-line output.

### The two translation levels

Every translated text comes from one of two levels. Level 1 is the base; level 2 refines it, and where both have a text, **the more specific one wins**.

| | Level 1: JSON files | Level 2: `translations` table |
| --- | --- | --- |
| Where | `app/locales/es.json`, `app/locales/en.json` | Database table `translations` (one generic table, no per-vertical columns) |
| Holds | The UI chrome: navigation, buttons, labels, messages | **UI overrides** (replace one JSON key in one language, platform-wide or for one store) and **content translations** (one field of one store / category / property / product row, in one language) |
| Managed by | Developers: edit the file, restart | Store packs (`i18n`, `ui_translations`) and `app/services/translations.py` |
| Decides | Which languages exist: a language is available only if it has a JSON file | Nothing about languages; it only refines level 1 |

#### Level 1: JSON catalogs (UI chrome)

Flat files `{"message.key": "text"}`. `es.json` is the reference and must have every key; `en.json` (or any other catalog) may be partial, and a missing key falls back to Spanish. Templates use `{{ t("key", name=…) }}` and routers `app.i18n.translator(request)`. Adding a language means adding `app/locales/<code>.json` and restarting. With no level 2 rows, every page shows exactly the JSON text.

#### Level 2: UI overrides

An override replaces the text of one JSON key, in one language, either for the whole platform or only on one store's public pages. First match wins, for the page language and then for Spanish:

```
store override  ->  platform override  ->  JSON catalog        (page language)
store override  ->  platform override  ->  JSON catalog        (es)
```

- Store overrides apply only on that store's public pages (`/stores/<slug>/…`). Admin and platform pages (`/`, `/login`, `/admin…`) use platform overrides and the JSON files.
- An override in one language never shows in another (an `en` override is not used on `es` pages).
- Overrides apply to flash messages too.
- Rules: the language must have a JSON catalog; the key must exist in `es.json`; the text must use exactly the same `{placeholders}` as the catalog; at most 5,000 characters.

#### Level 2: content translations

Catalog content is what the admin enters, in the store's own language. On the storefront, each text is shown in the page language if it has a translation, **otherwise as entered**, field by field: a translated name next to an untranslated description is normal.

| Row | Translatable fields | Limit (same as the field) |
| --- | --- | --- |
| store | `name` | 200 |
| category | `name`, `description` | 200, 2,000 |
| property definition | `label`, `option:<enum option>` | 200, 100 |
| product | `name`, `description`, `value:<property key>` | 200, 5,000, 2,000 |
| shipping method | `name` | 200 |
| payment terminal | `name`, `note` (a bank transfer's note for the shopper) | 200, 500 |

- Enum values: the product's own `value:<key>` wins, then the property's `option:<option>`, then the value as entered.
- Text values use `value:<key>`, otherwise they're shown as entered. Numbers, yes/no values, images and category references are never translated. A category reference shows the referenced category's translated name.
- Translations are stored per **language**: `en-US` and `en-GB` are stored as `en`, because pages are always in a base language. A language without a JSON catalog (e.g. `pt`) can be stored but is shown only once `pt.json` exists.

#### Where translations are shown

| Place | Level 1 + UI overrides | Content translations |
| --- | --- | --- |
| Public storefront (`/`, `/stores/…`) | yes | **yes** |
| Admin (`/admin…`) | yes (platform overrides) | **no**: admin shows and edits the content as entered. Exception: shipping method names on the **Shipping** and **Try payment** pages are shown in the page language, with the original next to them (the form still edits the original) |
| JSON API (`/api/…`) | no UI text | **no**: always the content as entered, whatever `?lang=` or cookie. The shopper's answers also carry the translations, for pages in other languages: each cart / order line's `name_translations` and a bank transfer's `instructions.note_translations` (`{"en": "…"}`, copied with the name / note) |

At checkout, a shopper browsing in English (`?lang=en`, or the remembered cookie) sees everything in English
where a translation exists: the shipping methods and payment terminals offered on the cart page (the page gives
their names in its language; the JSON API lists them as entered), the product names of the cart lines, and after
the order the deposit instructions' note and, on the order page (`/stores/<slug>/orders/<id>`), the shipping
method and line names. Solares ships English translations for all of them (pack `i18n`), so its English checkout
reads "Standard shipping (3 to 7 business days)" / "Bank transfer" and the English note.

#### Store packs → level 2

| Pack field | Becomes |
| --- | --- |
| `i18n` block of the store, a category, a property, a product, a shipping method or a payment terminal | Content translations of that row: one row per translated text. Enum options are translated by position (`i18n.en.config.options[i]` ↔ `config.options[i]`) |
| `ui_translations` (`{"en": {"store.nav.catalog": "Shop"}}`) | UI overrides for that store only |

On re-import, the pack replaces the translations of every row it names, and replaces the store's UI overrides (see `fixtures/packs/README.md`). The validator rejects unknown UI keys, wrong placeholders, two `i18n` blocks for the same language (`en` and `en-GB`), untranslated option counts, and texts over the field limits.

#### Safety rules

- All translated text is HTML-escaped like any other text (an override or a name containing `<script>` is shown as text).
- One row per (store, language, row / key, field): setting a text again updates it.
- Deleting a store, category, property, product or shipping method deletes its translations (a store's terminals go with the store). Deleting a property also deletes the products' translated values for it.
- From code (`app/services/translations.py`): `set_ui_translation(db, "en", "store.nav.catalog", "Shop", store=store)`, `delete_ui_translation(...)`, `set_content_translation(db, product, "en", "name", "Blue widget")`, `delete_content_translations(db, product)`. These validate their input and raise `TranslationError`.

#### Where to edit translations in admin

Open a store in admin and choose the **Translations** tab (**Traducciones** in Spanish): `/admin/stores/<slug>/translations`. Admin only; Spanish by default.

- **Language to edit**: pick it at the top (`?locale=en`; `?lang=` still switches the admin's own language). By default it is the first UI language that isn't the store's own, e.g. English for a Spanish store. A link opens the storefront in that language.
- **Content translations**: every translatable text of the store is listed with its original: the store name; category names and descriptions; property labels and enum options; product names, descriptions and text / enum values; shipping method names; payment terminal names and a bank transfer's note. Type a translation and press **Save translations**. An empty field removes the translation, so the original shows again. The save is all or nothing: if any field is invalid (e.g. too long), nothing is saved and the errors are shown next to the fields. The form accepts up to 20,000 fields, enough for large catalogs.
- **Interface texts of this store**: set the text of one UI key (from `app/locales/es.json`, e.g. `store.nav.catalog`) for this store's public pages in the chosen language. The key must exist and keep the catalog's `{placeholders}`. The table lists the store's current overrides next to the default text, each with a **Delete** button that restores the default.

Platform-wide overrides (all stores and admin) are not edited here; set them from code (`set_ui_translation(..., store=None)`).

#### Tests for the levels

| What | Where |
| --- | --- |
| Level 1 alone (no rows), page language choice, Spanish fallback, catalog completeness | `tests/test_i18n.py`, `tests/test_translation_levels.py` |
| UI override order (store → platform → JSON, page language → es), scoping, flash messages, validation, escaping | `tests/test_translations.py`, `tests/test_translation_levels.py` |
| Content fallback field by field, enum chain, untranslated types, other languages, regional codes, limits, escaping | `tests/test_translation_levels.py` |
| Admin and JSON API keep the source language | `tests/test_translation_levels.py`, `tests/test_translations.py` |
| Pack `i18n` / `ui_translations` import, re-import, translation rules in the validator | `tests/test_translations.py`, `tests/test_translation_levels.py`, `tests/test_pack_path.py` |

UI text quoted elsewhere in this README (buttons, messages) is the English version, i.e. what you see with `?lang=en`.

The test suites use the `client` fixture, which calls every endpoint with `?lang=en` (`tests/conftest.py`); the Spanish default and the fallbacks are covered with the `default_lang_client` fixture.

## Product API (public)

Read-only JSON, no login (like the storefront). Content is always as entered in admin (no translations, see
"Language (i18n)"). Hidden products (`status` = `hidden`) are never listed and their detail is `404`.

| Call | Returns |
| --- | --- |
| `GET /api/stores/<slug>/products` | The store's active products, by name. `?category=<category slug>` keeps one category (unknown: `404` `{"detail": "Category not found"}`) |
| `GET /api/stores/<slug>/products/<product slug>` | One active product with its property values. Hidden or unknown: `404` `{"detail": "Product not found"}` |

Unknown store: `404` `{"detail": "Store not found"}`.

### Product fields

| Field | List | Detail | Meaning |
| --- | --- | --- | --- |
| `id` | yes | yes | Product id |
| `slug` | yes | yes | Used in URLs (`/stores/<slug>/p/<product slug>`) |
| `name` | yes | yes | Name as entered |
| `description` | no | yes | Text or `null` |
| `price_cents` | yes | yes | Price in the store's `currency`, in cents |
| `is_active` | yes | yes | Always `true` here (hidden products are not public) |
| `in_stock` | yes | yes | `true` when the stock on hand is at least 1, `false` at 0 (see below) |
| `category_id` | yes | yes | The product's category |
| `properties` | no | yes | Every property definition of the category, in order: `{"key", "label", "prop_type", "value"}` (`value` is `{}` when not set) |

### Stock availability: `in_stock`

- Comes from the stock the admin sets on the product ([Products](#products), field **Stock**): `stock >= 1` → `true`,
  `0` → `false`. It changes as soon as the admin saves a new stock or an adjustment.
- **Only the flag is public.** The count (`stock_on_hand`) and the SKU (`sku`) are admin data: they are in no public API
  response (`tests/test_product_api.py` checks both). With an admin session, [Lookup by SKU](#lookup-by-sku) returns them.
- Products that never had a stock set (created before stock existed, or from a pack without `stock`) have stock 0,
  so they are `"in_stock": false` until an admin enters or adjusts their stock. **Solares** products start with 1, so
  they are `true`.
- Nothing reserves or decrements stock yet (no orders or checkout), and the storefront pages don't show the flag.

```bash
curl http://127.0.0.1:8000/api/stores/demo-widgets/products
# [{"id": 6, "slug": "widget-blue-m", "name": "Widget Blue M", "price_cents": 1999, "category_id": 3,
#   "is_active": true, "in_stock": true}, …]
curl http://127.0.0.1:8000/api/stores/demo-widgets/products/widget-blue-m      # …, "in_stock": true, …
```

## Inventory

Tools around SKUs and stock numbers, for admins. Stock changes themselves: see
[Stock adjustments and the movement log](#stock-adjustments-and-the-movement-log).

### Lookup by SKU

`GET /api/stores/<slug>/skus/<sku>` (JSON): which product of the store has that SKU, and its stock on hand.

- **Admin session required**, like the payments API: SKUs and stock counts are admin data (the public product API
  only says `in_stock`). No session: `401`; signed in but not an admin: `403`. Both come before any lookup, so
  they don't reveal whether a store or SKU exists.
- The SKU is matched exactly, ignoring case and surrounding spaces (`sol-8t-alta` finds `SOL-8T-ALTA`), only in that
  store (two stores may use the same SKU). Hidden products are found too (`"is_active": false`): they still have stock.
- Unknown SKU (or a value that can't be a SKU): `404` `{"detail": "No product with this SKU in this store"}`. Unknown
  store: `404` `{"detail": "Store not found"}`.

| Field | Content |
| --- | --- |
| `store` | The store's slug |
| `currency` | The store's currency (for `price_cents`) |
| `sku` | The SKU as stored (upper-case) |
| `stock_on_hand` | Units on hand now |
| `in_stock` | `stock_on_hand >= 1` (what the public API shows) |
| `product.id` | Product id |
| `product.slug` | Product slug (`/stores/<slug>/p/<product slug>`) |
| `product.name` | Name as entered |
| `product.price_cents` | Price in cents |
| `product.is_active` | `false` = hidden from the storefront |
| `product.category_id` | Its category |

```bash
curl -b jar.txt http://127.0.0.1:8000/api/stores/solares/skus/SOL-8T-ALTA
# {"store": "solares", "currency": "MXN", "sku": "SOL-8T-ALTA", "stock_on_hand": 1, "in_stock": true,
#  "product": {"id": 1, "slug": "solar-8-tubos-alta", "name": "Calentador solar 8 tubos, alta presión", …}}
curl -b jar.txt -i http://127.0.0.1:8000/api/stores/solares/skus/NOPE-1     # 404
curl -i http://127.0.0.1:8000/api/stores/solares/skus/SOL-8T-ALTA           # 401 (no session)
```

### Search by SKU (admin)

The product list (`/admin/stores/<slug>/products`) has a **SKU contains** box next to the category filter
(`?sku=…`): products whose SKU contains the text, ignoring case (`alta` finds `SOL-8T-ALTA`, `SOL-16T-ALTA`, …);
`_` and `%` are plain characters there, not wildcards. It combines with the category (`?category_id=…&sku=…`),
**Clear filters** removes both, and a stock adjustment made from a filtered list comes back to the same filtered
list. No match: "No product's SKU contains "…"".

### Stock log (admin)

`/admin/stores/<slug>/inventory/log` (tab **Inventory** → "Stock log (every movement)", or "See this product in the
stock log" on a product's page): every stock movement of the store's products, newest first, 50 per page (`?page=`).
Columns: when (UTC), product (link to its page), SKU, type (initial stock, product form, adjustment, pack import,
inventory import), change, stock after, reason, admin. Filters: **SKU contains** (`?sku=`, ignoring case),
**Reason contains** (`?reason=`, i.e. `%reason%`: ignoring case of ASCII letters, spaces collapsed, a typed `%` or `_`
is a plain character; an inventory import's reason is its file name), **type**
(`?kind=adjustment`, …) and one product (`?product=<id>`); they combine, and the page links keep them. Odd values
(`?kind=bogus`, `?page=x`) show the unfiltered first page instead of failing. Admin only, Spanish by default.
Each product's page keeps its own latest 50 too ([Stock adjustments](#stock-adjustments-and-the-movement-log)).

### Low stock (admin)

A simple threshold, at two levels (`app/services/low_stock.py`):

| Level | Where | Value |
| --- | --- | --- |
| Store | `stores.low_stock_threshold`, set on the low-stock page (below) | Units, 0 to 1,000,000; a new store (and one from an older database) starts at **2** (`DEFAULT_LOW_STOCK_THRESHOLD`) |
| Product | `products.low_stock_threshold`, the product form's **Low-stock threshold (optional)** | Units, 0 to 1,000,000; **empty = the store's**. A form post without the field (an older client) keeps it |

A product is **low** when its stock on hand is **at or below** its threshold (its own, else the store's); at 0 it is
out of stock, which is always listed (a threshold of 0 lists only those). Hidden products are listed too (marked
"Hidden"): their stock is still the store's. It is a list to restock from: nothing is blocked or sent.

`/admin/stores/<slug>/inventory/low-stock` (tab **Inventory** → **Low stock (N)**; Spanish by default: "Existencias
bajas"): the store's low products, out of stock first, then the fewest units, then by name. Columns: product (link to
its form), SKU, stock, threshold (with "own" / "store's"), state ("Out of stock" / "Low"). Under it, **Store
threshold**: `POST /admin/stores/<slug>/inventory/low-stock/threshold` with `threshold=<units>` (required; a negative,
decimal, separated or too large number is refused under the field, `400`, nothing saved), then `303` back to the list
with "Low-stock threshold saved: N units.". The product list marks low products ("· Low") next to their stock, like
"· Out of stock". Admin only (`/login` first, `403` for customers).

Solares ships every product with 1 unit, so with the default threshold (2) its whole catalog is listed. Try it:

```bash
curl -s -b jar.txt http://127.0.0.1:8000/admin/stores/solares/inventory/low-stock | grep -o 'data-sku="[^"]*" data-state="[a-z]*"'
curl -s -b jar.txt -o /dev/null -w '%{http_code}\n' -d threshold=0 http://127.0.0.1:8000/admin/stores/solares/inventory/low-stock/threshold   # 303
curl -s -b jar.txt -d threshold=-1 http://127.0.0.1:8000/admin/stores/solares/inventory/low-stock/threshold | grep field-error             # 400
```

### Inventory export (CSV)

The store's products with their SKU, price and stock on hand, to edit those numbers offline (a spreadsheet) and push
them back with the [inventory import](#inventory-import-csv). Only prices and stock: it is not a store export (the
catalog's format is the store pack).

| Where | How |
| --- | --- |
| Admin | Product list or the store's **Inventory** page (`/admin/stores/<slug>/inventory`): **Export inventory (CSV)** (shown when the store has products) downloads `/admin/stores/<slug>/products/export.csv` as `inventory-<slug>-<UTC time>.csv`, with the headers in the page's language |
| Command line | `python -m app.services.inventory_export <store slug> [-o FILE] [--lang es\|en]` (from `magic_stores/`, uses `DATABASE_URL`); headers in the store's language unless `--lang`; without `-o` the CSV goes to standard output |

One row per product (active and hidden): products with a SKU first (by SKU), then those without (by slug). The
header row is in the export's language (`admin.inventory.csv.*` in `app/locales/`); the import reads the headers of
every language, and the column ids:

| Column id | Header (`es` / `en`) | Content | On the way back (import) |
| --- | --- | --- | --- |
| `store` | Tienda / Store | The store's slug | A row of another store is refused |
| `sku` | SKU | The SKU; empty for products without one | **The key**: finds the row's product in the store (a row without SKU is refused: give the product a SKU in admin) |
| `slug` | Slug | The product's slug | Ignored (for people) |
| `name` | Nombre / Name | The name, for people (a leading `=` `+` `-` `@` gets a `'`, so a spreadsheet never runs it as a formula) | Ignored |
| `price_at_export` | Precio al exportar (no editar) / Price at export (do not edit) | The price when the file was made (`6499.00`, store currency). **Don't edit** | Lets the import tell your edit from a repricing made since the export (see below) |
| `stock_at_export` | Existencias al exportar (no editar) / Stock at export (do not edit) | The stock when the file was made. **Don't edit** | Lets the import tell your edit from a sale or adjustment made since the export (see below) |
| `price` | Precio / Price | **A price to edit**: store currency, dot for decimals, at most 2 (`6499.00`, `6499.5`, `6499`) | The new price |
| `stock_on_hand` | Existencias / Stock on hand | **A number to edit** | The new stock |

- UTF-8 with a BOM (spreadsheets show accents right), comma-separated, CRLF line ends, header row first. The headers
  have no accents, so they survive a spreadsheet that saves in another encoding. Standard output (no `-o`) has no BOM.
- The numbers are one consistent read of the database at export time.
- Nothing to export fails cleanly: a store without products answers `409` with the product list and "This store has
  no products, so there is no inventory to export." (admin), or `error: store 'x' has no products to export` and exit
  status 1 (command line). Unknown store: `404` (admin), `error: no store with slug 'x'` and exit status 1 (command
  line); no file is written.

```bash
python -m app.services.inventory_export solares -o solares-inventory.csv
#   5 product(s) of solares written to solares-inventory.csv
curl -b jar.txt -OJ http://127.0.0.1:8000/admin/stores/solares/products/export.csv   # saves inventory-solares-….csv
```

```
Tienda,SKU,Slug,Nombre,Precio al exportar (no editar),Existencias al exportar (no editar),Precio,Existencias
solares,GAS-BOIL-40L,boiler-gas-40-litros,Boiler de gas de depósito 40 L,5199.00,1,5199.00,1
solares,SOL-12T-BAJA,solar-12-tubos-baja,"Calentador solar 12 tubos, baja presión",7299.00,1,7299.00,1
…
```

### Inventory import (CSV)

Push stock numbers and prices back: the export above, edited offline, or any CSV with a SKU column and a stock and / or
price column (`sku,qty`, `sku,price`, `sku,price,qty`). It **only sets stock on hand and price**: names, descriptions,
properties and every other field are never touched (unlike a store pack import, which rewrites everything it names).

| Where | How |
| --- | --- |
| Admin | The store's **Inventory** page, `/admin/stores/<slug>/inventory` (button **Import inventory (CSV)** on the product list): choose the file, optionally tick **Apply even if the stock or price changed since the export**, **Import**. The page answers with the result: changed rows (line, SKU, stock before → after, price before → after), the number unchanged, and every refused row with its line and the reason, in the page's language |
| Command line | `python -m app.services.inventory_import <store slug> <file.csv> [--force]` (from `magic_stores/`, uses `DATABASE_URL`). Prints `solares: 1 changed, 4 unchanged, 0 refused` and the changed rows (`line 2: SOL-8T-ALTA: stock 1 -> 5, price 6499.00 -> 6999.00`); refused rows go to standard error (`line 3: refused: No product with SKU NOPE-1 in solares.`) |

Rules (one store per import):

| Row | Result |
| --- | --- |
| SKU of a product of this store (ignoring case and spaces), a stock (whole number ≥ 0) and / or a price | **Applied**: the stock and / or price are set; a stock change is logged as an **Inventory import** stock movement (reason: the file name; the admin for uploads), a price change is listed in the result (prices have no log yet, like price edits in the product form). An empty stock or price cell leaves that value as it is |
| The values are already the product's, or weren't edited (equal to `stock_at_export` / `price_at_export`) | **Unchanged**: nothing written (a sale or repricing made since the export is kept) |
| Neither a stock nor a price | **Refused** |
| Unknown SKU (also a SKU of another store) | **Refused** |
| The same SKU more than once in the file | **Refused**, every occurrence (the message lists their lines) |
| Negative stock (`-4`) | **Refused** |
| Stock not a whole number (`2.5`, `1,000`, `+5`, `ten`), over 2,147,483,647 | **Refused** |
| Price with a comma (`6499,00`, `6,499.00`: ambiguous between languages, so refused rather than guessed), negative, more than 2 decimals, not a number, over 999,999,999.99 | **Refused** |
| No SKU (a product without one) | **Refused**: give the product a SKU in admin first |
| `store` column with another store | **Refused** |
| The file has `stock_at_export` / `price_at_export` (the export does), the row's stock / price was edited, and the product's stock / price changed since the export (a sale, an adjustment, a repricing) | **Refused**, with both values, so the import never silently undoes that change. `--force` / the box applies it anyway (e.g. a physical count is the truth) |

- **Partial apply**: the rows that can be applied are applied (one transaction); refused rows change nothing (neither
  their stock nor their price: a row is applied whole) and are listed. Importing the same file twice changes nothing the second time. (A store pack import stays all or nothing.)
- The whole file is refused, nothing applied, when: no file / empty, not a CSV (e.g. an `.xlsx`), over 5 MB (`413`) or
  50,000 rows, a header without the SKU column or without both a stock and a price column, the same column twice, or no rows; unknown store (`404` in
  admin, exit status 1 on the command line).
- Headers: any UI language's (`Existencias`, `Stock on hand`, `Precio`, `Price`), the column ids (`sku`,
  `stock_on_hand`, `price`, `price_at_export`, `stock_at_export`), or `qty` / `quantity` / `cantidad` for the stock; compared ignoring case and spaces. Other columns are ignored.
- Separator `,` or `;` (a spreadsheet in Spanish saves CSV with `;`, which is why prices use a dot); UTF-8 with or
  without BOM, else read as Windows-1252 (only the SKU and number columns, plain ASCII, are used). Blank lines are skipped.
- Command-line exit status: `0` every row applied or unchanged, `3` some rows refused (the others applied), `1` nothing
  applied (unknown store, unreadable file), `2` wrong arguments.
- Code: `app/services/inventory_import.py` (the service and the command), `app/routers/admin_inventory.py` (the page).

```bash
python -m app.services.inventory_export solares -o solares-inventory.csv      # headers in Spanish (the store's language)
#   … edit the "Existencias" column in a spreadsheet, save as CSV …
python -m app.services.inventory_import solares solares-inventory.csv
#   solares: 1 changed, 4 unchanged, 0 refused
#     line 6: SOL-8T-ALTA: 1 -> 4
printf 'sku;price;qty\r\nGAS-BOIL-40L;4999.00;6\r\n' > prices.csv
python -m app.services.inventory_import solares prices.csv                     # line 2: GAS-BOIL-40L: stock 1 -> 6, price 5199.00 -> 4999.00
printf 'sku,qty\r\nSOL-8T-ALTA,3\r\nNOPE-1,2\r\n' > counts.csv
python -m app.services.inventory_import solares counts.csv; echo $?             # 3: NOPE-1 refused, SOL-8T-ALTA applied
curl -b jar.txt -F "file=@solares-inventory.csv" http://127.0.0.1:8000/admin/stores/solares/inventory/import
```

## Cart (public)

A visitor's cart, per store, with no sign-in. The shipping method and the payment are **not** part of the cart: they
are chosen at [Checkout](#checkout), which turns the cart into an order. A cart doesn't reserve stock (an order takes it).

### In the storefront

| Where | What |
| --- | --- |
| Every storefront page | Header link **Cart** with the number of items (`data-cart-count`, filled by the script) |
| Product pages (`/stores/<slug>/p/<product>`) | Below the product: quantity + **Add to cart** (or "Out of stock"), then "Added to the cart. View cart" or the reason it was refused. Shown under the store's own `product.html` too (it comes from the shared `base.html`) |
| `/stores/<slug>/cart` | The cart: one row per product with its **mini picture** (56 px), name (link), SKU, price, quantity with − / + buttons, line total and **Remove**, and under the name "Price updated: it was MXN …" (repriced, below) or what's wrong with it ("Not enough stock for this quantity now…", "Out of stock now…", "No longer sold…"); the item count and subtotal; **Empty the cart** (asks "Remove every product from the cart?" → **Yes, empty it** / **Cancel**); the **Checkout** box (see [Checkout](#checkout)); and a form **Add by SKU or product id** (choose SKU or Product id, type it, quantity, **Add to cart**). Empty cart: "Your cart is empty." with the same form |

Texts are in the page's language (`cart.*` in `app/locales/`, store UI overrides apply); product names, shipping
methods and payment terminals too, when they have a translation (else as entered; see
[Language](#language-i18n)).

**On a phone** (up to 640 px wide) the storefront and the cart are one narrow column, with CSS only (no other
markup): 17 px text, 10 px side margins, the header scrolls away instead of staying on top, photos as wide as the
screen, and every control of the cart at least 44 px tall (quantity − / +, **Add to cart**, **Remove**,
**Place order**, the checkout's fields, which are 16 px so the phone doesn't zoom into them). Each cart line becomes a
card: picture and name, price, the quantity stepper next to the line total, **Remove**. The rules are in the shared
stylesheets, `app/static/cart/cart.css` (cart, checkout, order page) and `app/storefronts/_default/static/style.css`,
and in solares' own `app/storefronts/solares/static/style.css` (its pack copy,
`fixtures/packs/solares/storefront/static/style.css`, is identical; so is `templates/product.html`: change one, copy
it to the other). Desktop pages are unchanged, and so is admin. Checked by `tests/test_storefront_mobile.py`; to see
it, open the pages with the browser's device toolbar at 375 × 812.

**How it's built.** React 18 (`app/static/vendor/react-18.3.1/`, the official UMD builds, see
`app/static/vendor/README.md`) loaded with `<script>` tags: **no Node build step** and no CDN. `app/static/cart/cart.js`
is the UI (`React.createElement`, no JSX); `app/static/cart/cart-core.js` is its logic without React (tested with
Node: `tests/js/cart-core.test.js`). The page holds no cart data: the script reads and changes the cart through the API
below. `base.html` (in `app/storefronts/_default/`) adds the header link, the scripts and the texts
(`<script type="application/json" id="cart-config">`); a store that overrides `base.html` must keep those to have a cart.

### Rage clicks

| Control | What happens on repeated clicks |
| --- | --- |
| **Add to cart** (product page, cart page form) | Disabled ("Adding…") while its request runs; extra clicks are ignored, not queued. Each click sends an `Idempotency-Key`; copies of the same request get the first answer instead of adding again (below) |
| Quantity − / + | The number changes on screen at once; the request is sent 400 ms after the last click, with the final number (five quick "+" = one request). A quantity is absolute, so repeating it is harmless. If refused (stock), the line goes back to the cart's number with the reason |
| **Remove** | Disabled ("Removing…") after the first click; removing a removed line changes nothing |
| **Empty the cart** | Needs a second click on **Yes, empty it** (a rage click can't empty the cart by accident); that button is disabled ("Emptying…") while it runs; emptying an empty cart changes nothing |
| Server | The add request's key is recorded before the work: a copy arriving meanwhile gets `409 request_in_progress`, later copies the first answer (`Idempotent-Replayed: true`). Adding to a line is one conditional `UPDATE` checked against the stock at that moment, so parallel adds can't go past it |

### The cart cookie: one store

- `magic_stores_cart`: a random token (256 bits), `HttpOnly`, `SameSite=Lax`, `Secure` with `SESSION_HTTPS_ONLY=true`,
  30 days, **`Path=/api/stores/<slug>/cart`**: the browser only sends it to that store's cart API, so every store has
  its own cart and a store never sees another's. Only the token's SHA-256 is stored (`carts.token_hash`), and a cart
  is always looked up together with its store: a token sent by hand to another store finds nothing.
- Created by the first successful add (reading an empty cart creates nothing). It is not the sign-in session
  (`magic_stores_session`), which is unrelated.
- JSON bodies only: the form or `text/plain` bodies another site could send are refused (`422`), like the payments API.

### API

`/api/stores/<slug>/cart`, public; every answer is the whole cart (below) and is sent with `Cache-Control: no-store`.

| Call | Body | Answer |
| --- | --- | --- |
| `GET /api/stores/<slug>/cart` | | The cart; empty (`"lines": []`) without one |
| `POST /api/stores/<slug>/cart/lines` | `{"sku": "SOL-8T-ALTA", "quantity": 2}` or `{"product_id": 1, "quantity": 2}` (exactly one of them; `quantity` 1 to 99, default 1); optional header `Idempotency-Key` (8 to 64 letters, digits, `-`, `_`) | Adds to the product's line (creates it, and the cart + its cookie, if needed) |
| `PATCH /api/stores/<slug>/cart/lines/<line id>` | `{"quantity": 3}` (1 to 99) | Sets the line's quantity. **Refused** (`409`) if more than the stock on hand now, even the quantity it already has after a sale; lowering it to what's in stock clears the line's issue |
| `DELETE /api/stores/<slug>/cart/lines/<line id>` | | Removes the line (an unknown line: nothing to do, `200`) |
| `DELETE /api/stores/<slug>/cart` | | **Clears** the cart: removes every line (the cart and its cookie stay; no cart or already empty: nothing to do, `200`) |

| Cart field | Content |
| --- | --- |
| `store`, `currency` | The store's slug and currency |
| `lines[]` | `id`, `product_id`, `sku`, `slug`, `name`, `name_translations` (`{"en": "Solar heater, 8 tubes, high pressure"}`: the name in other languages, copied with it), `url` (product page), **`image_url`** (the mini picture: the product's first image property, through `CONTENT_BASE_URL`; `null` without one, the page shows "No picture"), `quantity`, `unit_price_cents`, `line_total_cents`, `unit_price_display`, `line_total_display` (e.g. `MXN 6,499.00`): `name`, `sku` and the prices are the line's **snapshot** (below); `price_changed`, `previous_unit_price_cents`, `previous_unit_price_display` (the price before the last repricing, else `null`); `issue` (`out_of_stock`, `insufficient_stock`, `unavailable` or `null`) and `available` (`issue` is `null`), as of `validated_at` (UTC, the snapshot's last check) |
| `line_count`, `item_count` | Lines, and units over all lines |
| `subtotal_cents`, `subtotal_display` | Sum of the line totals |

### Snapshots, repricing and the 30-minute check

- **Snapshot**: a line keeps the **name, SKU and price** the product had when the line was added. Editing the catalog
  (renaming, a new SKU, a new price) doesn't rewrite open carts: the line and the subtotal stay as they were. The
  picture and the link follow the product.
- **Validation**: a line is checked against its product again when its snapshot is older than
  **`CART_REVALIDATE_MINUTES`** (setting, default **30**; `0` = on every request), by the next request that reads or
  changes the cart, and whenever the visitor changes that line (adding more, a new quantity):
  - **Repricing**: name, SKU and price are refreshed. A different price becomes the line's price, and the old one is
    kept in `previous_unit_price_cents` ("Price updated: it was MXN 6,499.00.") until the line's next validation.
  - **Quantity**: checked against the stock on hand now. More than the stock → `issue` `insufficient_stock`, 0 in stock
    → `out_of_stock`, product hidden → `unavailable`; `available` is then `false`. The quantity is **not lowered by
    itself** (that would tell the stock count): the visitor lowers it (a change within the stock clears the issue) or
    removes the line. Stock is checked, **not reserved** (no checkout yet).
- Adding or changing a line always checks the stock at that moment (refused if it would be exceeded), whatever the
  snapshot's age.
- Databases with carts from before snapshots get the columns at the next start (`app/schema_upgrades.py`), filled from
  the products; the first read validates them.

Refusals use the shape of the shipping quote errors, `{"detail": [{"loc", "msg", "type"}]}`:

| Case | Status | `type` |
| --- | --- | --- |
| The line would hold more than the product's stock on hand (**oversell**) | `409` | `insufficient_stock` (the stock count is not said: it stays admin data) |
| Stock on hand is 0 | `409` | `out_of_stock` |
| Unknown product, hidden product, another store's product or SKU | `422` | `unknown_product` |
| The line would hold more than 99 / the cart more than 100 products | `422` | `quantity_limit` / `cart_full` |
| Malformed body (both or neither of `sku` / `product_id`, quantity out of 1..99, unknown fields, not JSON) | `422` | FastAPI's |
| `Idempotency-Key` malformed / reused for another request or another cart / same request still running | `422` / `422` / `409` | `invalid_idempotency_key` / `idempotency_key_reused` / `request_in_progress` |
| Unknown line (`PATCH`) | `404` | `line_not_found` |
| Unknown store | `404` | `{"detail": "Store not found"}` |

A refused add writes nothing (no cart is created for it, and its key is forgotten so the same click can be retried).
Keys are kept 24 hours. Code: `app/services/cart.py`, `app/routers/cart.py`; tables `carts`, `cart_lines`,
`cart_requests`.

```bash
B=http://127.0.0.1:8000/api/stores/solares/cart
curl -s $B                                                                             # {"lines": [], "item_count": 0, …}
curl -s -c cart.txt -b cart.txt -H "Content-Type: application/json" -H "Idempotency-Key: click-0001" \
     -d '{"sku": "SOL-8T-ALTA", "quantity": 1}' $B/lines                               # 200, sets magic_stores_cart
curl -s -c cart.txt -b cart.txt -H "Content-Type: application/json" -H "Idempotency-Key: click-0001" \
     -d '{"sku": "SOL-8T-ALTA", "quantity": 1}' $B/lines                               # same answer, not added twice
curl -s -b cart.txt $B                                                                 # lines with sku + image_url
curl -s -b cart.txt -H "Content-Type: application/json" -d '{"sku": "SOL-8T-ALTA"}' $B/lines   # 409 insufficient_stock
```

```bash
curl -s -b cart.txt -X DELETE $B                                                       # clear: {"lines": [], …}
```

Not done yet: stock reservation while shopping, merging a cart into an account, removing old carts (they stay in the
database), and a rate limit. Repeated adds can still be used to probe whether a quantity is in
stock (not the exact count).

## Checkout

Turns the visitor's cart into an **order** with a **payment** of its total. The shopper (or an API client) picks a
**shipping method** and a **payment terminal** separately, at checkout: the cart holds neither, and no answer has a
combined "shipping + payment" field. It reuses the existing services: the total is the quote of admin **Try payment**
(`app/services/checkout_demo.py`: the [shipping quote](#shipping-quotes) on the subtotal, plus its total checks) and the
payment is `create_payment` of [Payments (mock)](#payments-mock).

### On the cart page

Under the cart, **Checkout**: **Shipping** (the store's active methods, `GET /api/stores/<slug>/shipping/methods`),
**Country** (2 letters; filled in when the method ships to one country only), **Payment** (the enabled terminals,
`GET /api/stores/<slug>/payment/terminals`), then subtotal, shipping (the quote API, asked again whenever the method,
the country or the cart change; "Free" from the free-shipping amount) and total, and **Place order and pay MXN …**
(disabled while it runs: one order per click, sent with an `Idempotency-Key`). Afterwards: "Order placed: Order ord_… for
MXN …. Payment pay_…: pending (the payment terminal confirms it)." and the cart, now empty. With **Transferencia
bancaria** (`bank_transfer`, solares' only enabled terminal) the button reads **Place order (MXN …)** and a hint says the
account details come next; afterwards the page shows the **deposit instructions** (amount, bank, beneficiary, CLABE,
account, 7-digit reference, concept, the store's note) and a link to the [order page](#bank-transfer-offline), which
shows them again. A refusal is shown under the
button, and the cart is reloaded so the lines at fault show their note (new price, not enough stock, …).

The path stays small: the **Add to cart** controls are where they were (product pages, and the cart's own form under
the checkout), and the checkout is one box under the cart lines: pick the method, the country and the terminal
separately, read subtotal + quoted shipping + total, place the order. Method and terminal names are in the page's
language ("Envío estándar (3 a 7 días hábiles)" / "Transferencia bancaria" in Spanish, "Standard shipping (3 to 7
business days)" / "Bank transfer" with `?lang=en`), and so is the transfer note after the order.

### Steps

`POST /api/stores/<slug>/cart/checkout` (under the cart's path: the cart cookie is scoped to it), public, JSON:

1. **The lines, right now.** Every line is validated against its product at this moment (not only every 30 minutes):
   still sold, stock on hand covers the quantity, same price as the cart shows. Otherwise **refused** (`409`), each line
   at fault listed; a changed price is applied to the cart (shown as "Price updated…"), so confirming again charges it.
2. **The choices.** The shipping quote for the method and country on the subtotal of the lines, and the total
   (`subtotal + shipping`, more than 0 and within a payment's limit); the terminal must exist and be enabled.
3. **The payment**: `create_payment` of the total through that terminal (`pending`).
4. **The order**, in one transaction: the order and copies of its lines are written, and **the cart is emptied**.
   **No stock is taken** here: step 1 only checked it (see [Stock follows the payment](#stock-follows-the-payment)).

Nothing is created when a check of steps 1 and 2 fails.

### Stock follows the payment

| Payment | Stock |
| --- | --- |
| Order created, payment `pending` | **Not taken** (checked at checkout, not held) |
| Payment `succeeded` (the terminal's signed callback, **Try payment → Simulate approved payment** for mock terminals, or an admin's **Confirm deposit** for a bank transfer) | **Taken**: each line's quantity comes off the product's stock, logged as an **Order** stock movement (reason: the order id). The order gets `stock.taken: true` and its time |
| Payment `failed` (the terminal declined it, or it was cancelled there: a mock payment has no separate "cancelled" state; a bank transfer an admin **rejected**) | **Never taken**, so nothing stays taken and nothing has to be given back; no movement |

- It happens in `payments.mark_payment`, the one function both the callback and the simulator use, **in the same
  transaction as the status change**: the payment is `succeeded` if and only if its stock was taken, once (a terminal
  repeating its answer changes nothing; if taking the stock fails, the payment stays `pending` and the terminal can
  retry). Payments without an order (made in admin) take no stock.
- Stock isn't held while the payment is pending, so a product can sell out before the payment succeeds. The success is
  final (the terminal has been paid), so the line takes what is left (never below 0) and records the rest as
  `short_quantity` (`stock.short: true` on the order): restock or refund it. A product deleted meanwhile: its whole
  line is short.

### After checkout: the cart empties

The cart **empties** (its lines are now the order's) and is **not locked**: the same cookie keeps working, so the
visitor can go on shopping into the same, now empty, cart. A second "Place order" finds it empty (`409 cart_empty`),
which also stops a double click without an `Idempotency-Key`; with the key, copies get the first order back
(`Idempotent-Replayed: true`).

The order keeps **copies**: each line's product id, SKU, name, unit price, quantity and total; the shipping method's
code and name, the country and the quoted amount; subtotal, shipping and total. Later catalog or shipping changes never
alter it (a deleted product leaves `product_id: null`). Its **status comes from its payment**: `awaiting_payment`
(pending), `paid` (succeeded), `payment_failed` (failed). The payment is answered as any other: its terminal's
[signed callback](#terminal-callback), **Try payment → Simulate** in admin for mock terminals, or, for a bank
transfer, an admin on its [payment page](#payments-admin-list) (the callback is refused for it).

### API

| Call | Body | Answer |
| --- | --- | --- |
| `POST /api/stores/<slug>/cart/checkout` | `{"shipping_method": "standard", "country": "MX", "terminal": "mock_card"}` (`country` optional: needed when the method lists countries); optional header `Idempotency-Key` | `201` + `{"order": {…}, "payment": {…}}` |
| `GET /api/stores/<slug>/orders/<order id>` | | `200` + the same shape, with the payment's current status; unknown or another store's: `404`. The id (`ord_` + 144 random bits) is the only key: whoever has it can read the order |

| `order` field | Content |
| --- | --- |
| `id`, `status`, `currency`, `created_at` | `ord_…`; `awaiting_payment` / `paid` / `payment_failed`; the store's currency; UTC |
| `lines[]` | `product_id` (`null` once deleted), `sku`, `name`, `name_translations`, `quantity`, `unit_price_cents`, `line_total_cents`, `unit_price_display`, `line_total_display`: copies from the cart at checkout; `short_quantity`: units that weren't there when the payment succeeded (0 = all taken) |
| `stock` | `taken` (the payment succeeded and the stock was taken), `taken_at` (UTC), `short` (some line has a `short_quantity`) |
| `item_count`, `subtotal_cents`, `total_cents` | Units; the sum of the lines; subtotal + shipping (= the payment's amount) |
| `shipping` | `method` (code), `name`, `country`, `amount_cents`, `free_shipping`: the quote at checkout |
| `subtotal_display`, `shipping_display`, `total_display` | e.g. `MXN 11,698.00` |

| `payment` field | Content |
| --- | --- |
| `id`, `status`, `terminal`, `amount_cents`, `currency` | `pay_…` (the payments API's id), `pending` / `succeeded` / `failed`, the terminal's code, the total, the currency |
| `kind` | The terminal's kind: `mock` or `bank_transfer` |
| `transfer_reference`, `instructions` | Bank transfer only (else `null`): the 7-digit reference, and the deposit instructions: `bank`, `beneficiary`, `clabe`, `account`, `note` (as entered), `note_translations` (`{"en": "…"}`), `reference`, `concept` (the payment id), `amount_cents`, `currency`, `amount_display` |

Refusals (`{"detail": [{"loc", "msg", "type"}]}`, like the cart API):

| Case | Status | `loc` | `type` |
| --- | --- | --- | --- |
| No cart, or an empty one | `409` | `["cart"]` | `cart_empty` |
| A line's product is out of stock / has less stock than the line's quantity / is no longer sold (one item per line) | `409` | `["cart", "lines", <line id>]` | `out_of_stock` / `insufficient_stock` / `unavailable` |
| A line's price changed (the cart now shows it) | `409` | `["cart", "lines", <line id>]` | `price_changed` |
| Unknown / inactive method; country missing / not served | `422` | `["body", "shipping_method"]` / `["body", "country"]` | `unknown_method`, `inactive_method`, `country_required`, `country_not_served` |
| Unknown / disabled terminal; a bank transfer terminal without complete bank details | `422` | `["body", "terminal"]` | `unknown_terminal`, `terminal_disabled`, `terminal_not_configured` |
| Total 0 / over a payment's limit | `422` | `["cart"]` | `nothing_to_pay` / `total_too_large` |
| Malformed body (missing method or terminal, a 3-letter country, unknown fields such as a total: it is never taken from the client), not JSON | `422` | FastAPI's | |
| `Idempotency-Key` malformed / reused for another request / still running | `422` / `422` / `409` | `["header", "Idempotency-Key"]` | as for the cart |

```bash
B=http://127.0.0.1:8000/api/stores/solares
curl -s -c cart.txt -b cart.txt -H "Content-Type: application/json" -d '{"sku": "SOL-8T-ALTA"}' $B/cart/lines > /dev/null
curl -s -b cart.txt -H "Content-Type: application/json" -H "Idempotency-Key: checkout-0001" \
     -d '{"shipping_method": "standard", "country": "MX", "terminal": "bank_transfer"}' $B/cart/checkout
#   201 {"order": {"id": "ord_…", "status": "awaiting_payment", "total_cents": 694900, …},
#        "payment": {"id": "pay_…", "status": "pending", "kind": "bank_transfer", "amount_cents": 694900,
#                    "transfer_reference": "4821937", "instructions": {"bank": "Banco Ejemplo", "clabe": "0021…", …}}}
curl -s -b cart.txt $B/cart                                     # "lines": [] (emptied into the order)
curl -s $B/orders/ord_…                                         # the order, with its payment's status
```

Not done yet: holding stock while a payment is pending, refunds, an admin orders page (the orders are in the `orders`
table; their payments are on the **Payments** tab, their stock movements in the stock log), customer accounts, emails,
taxes.

## Shipping quotes

Each store has its own **shipping methods** (table `shipping_methods`), each a simple flat rate:

| Column | Meaning |
| --- | --- |
| `code` | Slug, unique per store (e.g. `standard`); what the API takes |
| `name` | Display name |
| `is_active` | Inactive methods are not listed and can't be quoted |
| `sort_order` | Listing order (then name) |
| `rate_cents` | Flat price, in the store's `currency`, in cents (like `price_cents`) |
| `free_shipping_threshold_cents` | Optional: the quote is 0 when the subtotal is **at least** this |
| `countries` | Optional list of ISO 3166-1 alpha-2 codes it ships to (`["ES", "PT"]`); empty = anywhere |

They come from a store pack (`shipping_methods`, see below), the admin **Shipping** tab (see
[Shipping methods](#shipping-methods)), or code (`db.add(ShippingMethod(store_id=…, code="standard", name="Standard",
rate_cents=495))`). Rules: `app/services/shipping.py`; HTTP: `app/routers/shipping.py`.

### Solares shipping methods

Solares defines its methods in its own pack, `fixtures/packs/solares/solares.store-pack.v1.json` (`shipping_methods`,
contract in `fixtures/packs/README.md`):

| `code` | Method | Rate | Free shipping | Ships to |
| --- | --- | --- | --- | --- |
| `standard` | Standard shipping, 3 to 7 business days | MXN 450.00 | subtotal ≥ MXN 10,000.00 | MX |
| `express` | Express shipping, 1 to 2 business days | MXN 1,200.00 | never | MX |

Customers see the names in the store's language (Spanish); the pack's `i18n` gives their English names, used on
English pages. Change or add translations in the admin **Translations** tab.

How they load (the same import as the rest of the pack; nothing else seeds them):

| Database | What happens |
| --- | --- |
| **Fresh** (solares doesn't exist) | The first start imports the pack (`SEED_PACKS`, default solares): the store comes with both methods. Recorded in `pack_imports` (`"shipping_methods": {"created": 2, …}`) |
| **Existing**, solares loaded before the pack had methods | Restarting adds nothing: a seeded store is never changed by later starts. Re-import the pack once: `python -m app.services.store_pack_import fixtures/packs/solares/solares.store-pack.v1.json` (prints `shipping methods: 2 created, 0 updated`), or upload its `.zip` at `/admin/packs` with "Update the store if it already exists" |
| **Any re-import** | Upsert by `code`: `standard` / `express` are reset to the pack's values (and their translations to the pack's `i18n`) (admin edits to them are overwritten, like products); methods the pack doesn't name (created in admin) are kept |

A re-import also resets the solares categories and products the pack names (see "Re-import" in
`fixtures/packs/README.md`). To change solares shipping for every new database, edit the pack file; for one running
database, the **Shipping** tab is enough.

```bash
curl http://127.0.0.1:8000/api/stores/solares/shipping/methods       # standard, express
curl -X POST -H "Content-Type: application/json" \
     -d '{"method": "standard", "subtotal_cents": 519900, "country": "MX"}' \
     http://127.0.0.1:8000/api/stores/solares/shipping/quote           # "amount_cents": 45000, "currency": "MXN"
```

### `GET /api/stores/<slug>/shipping/methods`

The store's active methods in order: `[{"code", "name", "sort_order", "rate_cents", "free_shipping_threshold_cents", "countries"}]`. Unknown store: `404`.

### `POST /api/stores/<slug>/shipping/quote`

Request (JSON):

| Field | Type | Required | Rule |
| --- | --- | --- | --- |
| `method` | string | yes | A method code of this store |
| `subtotal_cents` | integer ≥ 0 | no | Only used for the free-shipping threshold; without it the full rate applies |
| `country` | string | no (yes if the method has `countries`) | 2 letters, case-insensitive (`es` = `ES`) |

With a method `standard` of USD 4.95 created in admin for `demo-widgets` (the store of the "Admin pages" examples):

```bash
curl -X POST -H "Content-Type: application/json" \
     -d '{"method": "standard", "subtotal_cents": 2000, "country": "ES"}' \
     http://127.0.0.1:8000/api/stores/demo-widgets/shipping/quote
```

`200` response:

```json
{
  "store": "demo-widgets",
  "currency": "USD",
  "method": {"code": "standard", "name": "Standard"},
  "country": "ES",
  "subtotal_cents": 2000,
  "amount_cents": 495,
  "free_shipping": false
}
```

`amount_cents` is what shipping costs; `free_shipping` is `true` when it is 0 because of the threshold. `country` is
echoed upper-cased; `country` / `subtotal_cents` are `null` when not sent.

Errors:

| Case | Status | Body |
| --- | --- | --- |
| Unknown store | `404` | `{"detail": "Store not found"}` |
| Malformed body (no `method`, negative subtotal, 3-letter country, …) | `422` | FastAPI's validation list |
| Code not among the store's methods (including another store's) | `422` | `type` `unknown_method` |
| Method exists but `is_active` is false | `422` | `type` `inactive_method` |
| Method has `countries` and no `country` was sent | `422` | `type` `country_required` |
| Method has `countries` and `country` isn't one of them | `422` | `type` `country_not_served` |

The quote errors use the same shape as FastAPI's validation errors, so a client parses one format:
`{"detail": [{"loc": ["body", "method"], "msg": "Unknown shipping method 'teleport' for this store.", "type": "unknown_method"}]}`.

Not handled yet: postal codes / zones, weight-based or per-item rates, taxes on shipping, multiple currencies.

## Payment terminals

The terminals a store can take payments with (table `payment_terminals`). No card network or bank API is called.
Columns: `code` (unique per store), `name` (display name), `kind`, `is_enabled`, `sort_order`, `config` (JSON: a bank
transfer's deposit details). Two kinds (`TerminalKind` in `app/models.py`):

| Kind (`kind`) | Code | Outcome comes from |
| --- | --- | --- |
| Mock (`mock`) | `mock_…` | The terminal's [signed callback](#terminal-callback), or the admin simulator (**Try payment**) |
| Bank transfer, offline (`bank_transfer`) | not `mock_…` (solares: `bank_transfer`) | **An admin only**: Confirm deposit / Reject on the payment page (see [Bank transfer (offline)](#bank-transfer-offline)); the callback and the simulator are refused |

Every store has these two mocks:

| `code` | `name` | Order |
| --- | --- | --- |
| `mock_card` | Mock card | 0 |
| `mock_transfer` | Mock bank transfer | 1 |

- A new store gets both, enabled, when it is inserted, however it is created: admin form, pack import (command line,
  upload) or startup seed (ORM hook on `Store` in `app/models.py`). A pack may then add terminals and set them up
  (`payment_terminals`, see `fixtures/packs/README.md`).
- **Solares** takes payments by bank transfer only: its pack adds `bank_transfer` ("Transferencia bancaria", enabled,
  with example deposit details) and lists `mock_card` / `mock_transfer` with `"enabled": false`: unlinked from the
  store but kept, so a demo can enable them (Payment terminals tab) and disable them again.
- Stores created before terminals existed get the missing ones at the next app start
  (`ensure_default_terminals` in `app/services/payments.py`). It only adds codes a store lacks and never changes
  existing rows, so a terminal you turned off stays off. Deleting a store deletes its terminals.
- A solares database from before `bank_transfer` existed gets it at the next start as well (`catch_up_terminals` in
  `app/services/pack_seed.py`): when a seeded store (`SEED_PACKS`) lacks a terminal its pack names, the pack's
  `payment_terminals` are applied once (`bank_transfer` created, the mocks disabled). After that it does nothing, so
  a mock you enable again and the bank details you enter stay as you left them.
- Turn one off or rename it in admin: the store's **Payment terminals** tab (see
  [Payment terminals (admin)](#payment-terminals-admin)).

### `GET /api/stores/<slug>/payment/terminals`

Every terminal of the store, **disabled ones included**, in order (`sort_order`, then `code`). `enabled` says whether it
can be used:

```bash
curl http://127.0.0.1:8000/api/stores/solares/payment/terminals
```

```json
[
  {"code": "bank_transfer", "name": "Transferencia bancaria", "kind": "bank_transfer", "enabled": true, "sort_order": 0},
  {"code": "mock_card", "name": "Mock card", "kind": "mock", "enabled": false, "sort_order": 10},
  {"code": "mock_transfer", "name": "Mock bank transfer", "kind": "mock", "enabled": false, "sort_order": 11}
]
```

Unknown store: `404` `{"detail": "Store not found"}`. A terminal with `"enabled": false` takes no new payments: creating
one through it is refused (`422`, `terminal_disabled`), and **Try payment** doesn't offer it. The bank details are not
in this list: a shopper gets them with the payment (checkout answer, order page).

## Payments (mock)

A payment (table `payments`) is an amount charged through one of the store's [payment terminals](#payment-terminals).
There is **no card network**: the terminals are mocks. A payment's outcome comes from its terminal through a
**signed callback** (server to server), never from a browser.

| Column | Content |
| --- | --- |
| `public_id` | The payment's id everywhere outside the database: `pay_` + 24 random characters (144 bits). The sequential `id` is internal and never shown |
| `store_id`, `terminal_id` | The store, and the terminal used (must be enabled when the payment is created) |
| `amount_cents`, `currency` | Amount in cents (1 to 2,147,483,647); currency = the store's, copied at creation |
| `status` | `pending`, `succeeded` or `failed` (below) |
| `mock_reference` | Optional, up to 64 characters: what the terminal answered (e.g. `AUTH-123`) |
| `transfer_reference`, `instructions` | Bank transfer only: the 7-digit reference (unique in the store) and a copy of the deposit instructions given to the shopper |
| `decided_by_id`, `decision_note` | Bank transfer only: the admin who confirmed / rejected it, and their note (up to 200 characters) |
| `created_at`, `updated_at` | UTC; `updated_at` changes when the status does |

### Statuses

Defined in code as `PaymentStatus` and `PAYMENT_TRANSITIONS` (`app/models.py`):

```
pending ──succeeded──▶ succeeded   (final)
   └──────failed─────▶ failed      (final)
```

- Every payment is created `pending`.
- It is answered **once**: a final payment never changes (`409`, type `invalid_transition`).
- The same answer again returns it unchanged (`200`; the first `mock_reference` is kept), so a terminal may retry.
- Two answers arriving at the same time can't both win: the change is a single `UPDATE … WHERE status = 'pending'`.

### Who may call what

| Call | Access | Success |
| --- | --- | --- |
| `GET /api/stores/<slug>/payment/terminals` | Public (configuration only: codes, names, enabled flags) | `200` |
| `POST /api/stores/<slug>/payments` with `{"terminal": "mock_card", "amount_cents": 1999}` | **Admin session** | `201` + payment (`pending`) |
| `GET /api/stores/<slug>/payments/<payment id>` | **Admin session** | `200` + payment |
| `POST /api/stores/<slug>/payments/callback` | **The terminal only**: signed with `PAYMENT_CALLBACK_SECRET` (below); refused for bank transfers (`409`, `operator_confirmation_required`) | `200` + payment (`succeeded` / `failed`) |
| Admin **Try payment** page | Admin session, **mock terminals only** (simulates the terminal for demos) | see [Try payment](#try-payment-operator-demo) |
| Admin payment page, **Confirm deposit** / **Reject** | Admin session, **bank transfers only** | see [Payments (admin list)](#payments-admin-list) |

- No session: `401` (JSON, not a redirect); signed in but not an admin: `403`. Both are answered before any lookup,
  so they don't reveal whether a store or payment exists.
- Payment ids are random: there is no `1, 2, 3…` to walk through. `GET …/payments/1` is `404`.
- Shoppers get payments only through [Checkout](#checkout): it creates the payment on the server, with
  `create_payment`, for the order's total (subtotal of the cart's lines + the shipping quote), never from an amount sent
  by the browser. The payments API itself stays admin only.
- A page on another site can't create payments through a signed-in admin's browser: the session cookie is
  `SameSite=Lax`, and JSON bodies sent as form data or `text/plain` (the only kinds a cross-site form can send) are
  refused (`422`).

### Terminal callback

The terminal tells the outcome with `POST /api/stores/<slug>/payments/callback`:

```
X-Terminal-Timestamp: <unix time in seconds>
X-Terminal-Signature: sha256=<hex HMAC-SHA256 of "<timestamp>.<raw body>" with PAYMENT_CALLBACK_SECRET>

{"payment": "pay_…", "status": "succeeded", "mock_reference": "AUTH-123"}
```

| Rule | Effect |
| --- | --- |
| `PAYMENT_CALLBACK_SECRET` empty (default) | Every callback is refused (`503`): callbacks are off until a secret is set |
| Missing / wrong signature, or a body changed after signing | `401`, nothing changes. The payment id is inside the signed body, so a signed answer can't be pointed at another payment |
| Timestamp more than 300 seconds from now | `401`: a captured callback can't be replayed later (within the window, replaying the same answer changes nothing) |
| Body over 8 KB | `413`, before anything else |
| Signed but malformed (`status` not `succeeded` / `failed`, unknown fields, reference over 64) | `422` |
| Payment not of this store | `404` |
| Payment through a bank transfer terminal (even correctly signed) | `409` (`operator_confirmation_required`), nothing changes: only an admin decides it |
| Already answered the other way | `409` (`invalid_transition`) |

The signature check is done first, with a constant-time compare (`app/services/payment_callbacks.py`). Try it with
the secret set (e.g. in `.env`) and a payment created in admin (**Try payment**, or the API with an admin session):

```bash
SECRET=…                      # the PAYMENT_CALLBACK_SECRET of the running app
PAYMENT=pay_…                 # a pending payment of solares
BODY="{\"payment\": \"$PAYMENT\", \"status\": \"succeeded\", \"mock_reference\": \"AUTH-123\"}"
TS=$(date +%s)
SIG=$(printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/^.* //')
curl -X POST -H "Content-Type: application/json" -H "X-Terminal-Timestamp: $TS" \
     -H "X-Terminal-Signature: sha256=$SIG" -d "$BODY" \
     http://127.0.0.1:8000/api/stores/solares/payments/callback      # 200, "status": "succeeded"
```

Payment (every response):

```json
{
  "id": "pay_3q0VJ7c2hWm9xKQy4ZlTr1bN", "store": "solares", "terminal": "mock_card",
  "amount_cents": 1999, "currency": "MXN",
  "status": "succeeded", "mock_reference": "AUTH-123", "transfer_reference": null,
  "created_at": "2025-11-15T17:35:02.855670Z", "updated_at": "2025-11-15T17:35:02.895403Z"
}
```

Errors of the create / callback bodies use the same shape as FastAPI's validation errors
(`{"detail": [{"loc", "msg", "type"}]}`):

| Case | Status | `type` |
| --- | --- | --- |
| Unknown store, or payment id not of this store | `404` | (`{"detail": "Store not found"}` / `"Payment not found"`) |
| Malformed body (no terminal, amount ≤ 0 / not an integer / too large) | `422` | FastAPI's |
| Terminal code not among the store's terminals | `422` | `unknown_terminal` |
| Terminal exists but is disabled | `422` | `terminal_disabled` |
| A bank transfer terminal whose bank details are incomplete | `422` | `terminal_not_configured` |
| Callback for a bank transfer payment (an admin decides it) | `409` | `operator_confirmation_required` |
| Payment already `succeeded` / `failed` and answered the other way | `409` | `invalid_transition` |

Databases created before `public_id` existed are upgraded at the next start (`app/schema_upgrades.py`): the column is
added and every existing payment gets its own random id.

Still to do before real money: per-terminal secrets (one shared secret today), rate limiting, and a customer checkout
that creates payments from server-side orders. Admin forms have no CSRF tokens (project-wide; `SameSite=Lax` is the
protection today).

## Bank transfer (offline)

A `bank_transfer` terminal (solares: "Transferencia bancaria") takes payments **offline**: the shopper deposits the
amount into the store's account (SPEI in Mexico) and an admin, after seeing the deposit, confirms or rejects the
payment. No bank API is called (`app/services/bank_transfer.py`).

1. **Checkout** (cart page or `POST /api/stores/<slug>/cart/checkout` with `"terminal": "bank_transfer"`): the order
   and a `pending` payment are created as for any terminal. The payment also gets a **reference** (7 digits, unique in
   the store: SPEI's "referencia numérica") and a **copy of the deposit instructions**: bank, beneficiary, CLABE,
   account, the store's note, the reference and the concept (the payment's `pay_…` id). A terminal whose details are
   incomplete refuses the checkout (`422`, `terminal_not_configured`, shown on the cart page).
2. **The shopper sees the instructions**: on the cart page right after placing the order, in the checkout answer
   (`payment.instructions`), and on the **order page** `/stores/<slug>/orders/<order id>` (linked from the cart
   page; the random `ord_…` id is the key, like the order API; `noindex`). The page shows the status (waiting for the
   payment / paid / payment declined), the lines and totals, and the instructions while the payment is pending.
   The store's note is shown in the page's language: its translation (Translations tab, or the pack's `i18n`;
   solares ships an English one) is copied with the instructions (`note_translations`), else as entered.
3. **An admin decides**: **Payments** tab → the payment (its reference is in the list) → **Confirm deposit** (the
   payment `succeeded`, the order `paid`, its stock taken: [Stock follows the payment](#stock-follows-the-payment)) or
   **Reject** (`failed`, no stock taken), each with an optional note (e.g. the SPEI tracking key). The admin and note
   are kept on the payment.

Nothing else can decide a bank transfer: the signed terminal callback is refused (`409`,
`operator_confirmation_required`, even with a valid signature), **Try payment** neither creates nor simulates it, and
in code `payments.mark_payment` refuses it without an `operator` (the admin user). The instructions are a copy:
editing the terminal's details later (Payment terminals tab) changes what new shoppers see, not what earlier ones were
told.

**Solares ships example details** (bank "Banco Ejemplo", beneficiary "Solares (cuenta de ejemplo)", a CLABE with a
valid control digit that is not a real account): enter the real ones in **Payment terminals → Transferencia bancaria**
before taking real orders. Re-importing the pack never overwrites them (a terminal's `config` is only written when it
has none).

Try it (app on 8000, solares seeded, admin cookie jar `jar.txt` as in [Signing in with curl](#signing-in-with-curl)):

```bash
B=http://127.0.0.1:8000/api/stores/solares
curl -s $B/payment/terminals                        # bank_transfer enabled ("kind": "bank_transfer"); mocks "enabled": false
curl -s -c cart.txt -b cart.txt -H "Content-Type: application/json" -d '{"sku": "SOL-8T-ALTA"}' $B/cart/lines > /dev/null
curl -s -b cart.txt -H "Content-Type: application/json" \
     -d '{"shipping_method": "standard", "country": "MX", "terminal": "bank_transfer"}' $B/cart/checkout
#   201 … "payment": {"id": "pay_…", "status": "pending", "kind": "bank_transfer", "transfer_reference": "4821937",
#         "instructions": {"bank": "Banco Ejemplo", "beneficiary": "Solares (cuenta de ejemplo)", "clabe": "002180001234567896", …}}
# Shopper page: http://127.0.0.1:8000/stores/solares/orders/ord_…
curl -s -b jar.txt -o /dev/null -w '%{http_code}\n' -d 'note=SPEI 0123' \
     http://127.0.0.1:8000/admin/stores/solares/payments/pay_…/confirm   # 303; the payment succeeded, stock taken
curl -s $B/orders/ord_…                                # "status": "paid", "stock": {"taken": true, …}
```

## Tests

```bash
pytest -q          # whole suite
pytest -q --cov    # + coverage report (line + branch); fails if coverage of app/ drops below 90 % (.coveragerc)
```

`../for_testing --cov` also runs the content app's tests with coverage (`content_app`, at least 90 %) and the cart's
JavaScript tests with Node's coverage (`app/static/cart/cart-core.js`, at least 90 % of lines, branches and functions):

```bash
node --test tests/js/*.test.js                       # what ../for_testing runs (Node 18.15+)
node --test --experimental-test-coverage --test-coverage-include='app/static/cart/cart-core.js' \
     --test-coverage-lines=90 --test-coverage-branches=90 --test-coverage-functions=90 tests/js/*.test.js   # --cov (Node 22+)
```

`./for_testing` (from the folder above this one; note the underscore) must be executable: git records it as such. If a copy
lost the bit ("Permission denied", e.g. after unpacking with a tool that drops file modes), run `chmod 755 for_testing`,
or start it as `bash for_testing`.

Run `--cov` **from this folder**: coverage reads `.coveragerc` and writes `.coverage` in the current folder. From
anywhere else use `cd /path/to/magic_stores && python -m pytest -q --cov`, or `../for_testing --cov`. Don't write
`pytest --cov /path/to/magic_stores`: `--cov` takes the path as its own value (the coverage source), so pytest gets
no test path and collects tests from the current folder (from `/`, the whole disk).

What is covered, among others:

| Area | Tests |
| --- | --- |
| Sign in: success (cookie flags, redirect), failure (401, no session, no user enumeration), `next`, open-redirect protection, sign out | `tests/test_auth_admin.py` |
| Every `/admin` route (discovered from the app, GET and POST) redirects to `/login` without a session and is `403` for customers; forged / stale sessions rejected | `tests/test_auth_admin.py` |
| Password hashing (PBKDF2 format, no plaintext, malformed hashes, Python without scrypt), admin seed | `tests/test_auth.py` |
| End to end: sign in → store → category → property definitions → product → sign out → visible on the storefront (HTML and API) | `tests/test_e2e_admin_storefront.py` |
| Admin CRUD for stores, categories, property definitions, products (SKU: created trimmed / upper-case and shown in the list and form; empty, malformed or duplicate-in-store refused on create and edit with nothing saved, case-insensitive; the same SKU allowed in another store; products without SKU coexist and need one on their next admin save; a concurrent duplicate names the SKU; the `UNIQUE (store_id, sku)` constraint; older databases upgraded at startup; stock: set on create and edit, shown in the form and list, 0 allowed, negative refused on create and edit with nothing saved, empty / missing / decimal / separators / non-ASCII digits / too large refused, a post without it never zeroes it, the `CHECK (stock_on_hand >= 0)` constraint, pack products start at 0 and a re-import keeps the stock, older databases upgraded at startup; validation messages in the page language: Spanish by default (e.g. a negative stock on a Spanish store, create and edit), English with `?lang=en`, every error code the service can return has a translated message, names in messages escaped; admin summary line and "Out of stock" in the list), theme, shipping methods, payment terminals (validation, scoping, deletes; shipping: login / customer `403` on every page, create, edit, Spanish default, translated field errors; terminals (`tests/test_admin_terminals.py`): login / customer `403` on every page with nothing changed, list with states, disable / enable (idempotent) seen in the API, rename (code kept), validation, other stores' terminals `404`, a disabled terminal refused by the payments API and by Try payment (not offered, refused if sent), all-disabled warnings, pending payments still answerable, Spanish default; payments list (`tests/test_admin_payments.py`): shows a payment created through the API with id, amount, currency, terminal, status and timestamps, status / updated time after the answer, newest first with 50-per-page paging, odd `?page=` values never fail, only this store's payments, empty state, read-only (GET only, no forms), login / customer `403`, Spanish default) | `tests/test_admin_*.py` |
| Import records admin UI: list (latest first, status filter, pages; a huge `?page=` is capped instead of a 500), detail (report, issues, escaped exception, unfinished imports), Spanish default, admin only, link from upload errors | `tests/test_admin_imports.py` |
| Import records: one per attempt with its status, file hash, user and counts; `issues` for invalid / conflicting packs; `exception` (type, message, traceback) for an unexpected error, which returns 500 and writes nothing; a stopped import stays `running`; a failure to save the record doesn't undo the import; command-line imports | `tests/test_pack_import_log.py` |
| Pack path end to end: each fixture's product pages (store language, `es`, `en`) show every imported property with the right label and value, API detail equals the fixture, store / category listings, hidden products stay private; re-upload of the same file (refused with "already imported", or with update: idempotent ids, pages and API), admin edits undone by a re-upload, concurrent duplicate upload, over-nested JSON | `tests/test_pack_path.py` |
| Admin pack upload: valid fixture creates the store (API + message), invalid files rejected on the page (400 / 413) with no store created, existing store only updated when asked (409), invalid or conflicting packs leave existing data unchanged, Spanish default | `tests/test_admin_packs.py` |
| Public product pages, hidden products, theme CSS variables | `tests/test_storefront_products.py`, `tests/test_admin_theme.py` |
| Inventory: SKU lookup API hit (product + stock, every fixture SKU, ignoring case and spaces, follows the stock, hidden products too) and miss (unknown / partial / wildcard / over-long SKU, another store's SKU, unknown store: `404`), admin session before any lookup (`401` / `403`), documented fields = the response models; admin SKU search (contains, ignoring case, exact, no match, `_` / `%` literal, with the category, adjustments come back to the search); inventory CSV export: every fixture SKU + stock (header, BOM, CRLF, order, accents, file name), headers in the page's language (admin) / the store's or `--lang` (command line), no header means two columns in two languages, current numbers, products without SKU last, formula-looking names neutralised, export button only with products, empty store `409` and missing store `404` (admin), command line to a file / standard output, empty and missing store exit 1 with nothing written, the documented command as a real process | `tests/test_inventory.py` |
| Cart API: empty cart (no cookie or cart created, `no-store`), clear the cart (every line, twice harmless, the same cart kept, no cart, only one's own), happy path (add by SKU in any case and by product id, cookie attributes and `Path`, merge into a line, read back lines with SKU, mini picture URL, quantities, totals and displays, change quantity, remove, remove twice), oversell refused (more than the stock, past it on an existing line, raising a line, out of stock, merge checked against the stock at that moment; nothing written, no count revealed), lines become unavailable when the stock drops or the product is hidden, unknown / hidden / other store's products, malformed and non-JSON bodies, limits, unknown lines; one store per cookie (path, forged or foreign tokens, only the hash stored), deletes; rage clicks (same key adds once, a copy before the cookie, reused / invalid keys, refused requests free their key, in-progress `409`, simultaneous claims, old keys forgotten, parallel adds merge); snapshots (catalog renames, new SKUs and prices don't rewrite an open cart; after `CART_REVALIDATE_MINUTES` the line is repriced with the previous price shown until its next validation, 29 minutes is not enough, the setting is configurable (0 = every request) and range-checked, only stale lines are checked, the quantity is checked against the stock (insufficient, out of stock, hidden) without lowering it, a change validates the line at once, an update past the stock is refused even at the same quantity after a sale, fixing it clears the issue), older cart tables upgraded; pages (cart page in the store's language with React and its texts, add-to-cart under product pages including solares' own template, header link, texts safe in the page), vendored React checksums | `tests/test_cart.py` |
| Checkout: happy path returns the order and payment ids (total = subtotal + the shipping service's quote, line copies, the payment pending for the total and seen by the admin payments API), the cart empties and stays usable, shipping charged below the free amount, the cart never holds shipping or payment, the order's status follows its payment; refused: unavailable / out of stock / oversell right now (every line listed, nothing created, cart kept), a changed price until seen, an empty cart, shipping / terminal choices (unknown, inactive, disabled, country), total 0, malformed or non-JSON bodies; rage clicks (same key = one order, a second click finds the cart empty, refused checkouts free the key); orders keep their copies after catalog changes and deletes, unknown / foreign orders `404`, store delete ; stock follows the payment: checkout and pending take none, the terminal's signed success takes each line's stock once (movements, order `stock.taken`, repeated answers), a failed / cancelled payment takes nothing (and can't succeed later), the admin simulator takes it too, sold out or deleted before the payment = what is left and `short_quantity`, the compare-and-set when the stock changes meanwhile, payments without an order, status and stock commit together, older order tables upgraded | `tests/test_checkout.py` |
| Cart JavaScript (also run by `../for_testing`, with a 90 % coverage threshold under `--cov`): money as the server shows it, the checkout body, the lines a refusal names, checkout error messages; the rage-click guard (one action at a time, failures free it), quantity batching (one request with the last number, per line, refusals reach every click, real timers), error messages, the SKU / id form, quantities, keys the server accepts; the scripts need no build | `tests/test_cart_js.py` (runs it with the TAP reporter chosen explicitly, since Node's default reporter differs between versions; runs `tests/js/cart-core.test.js` with Node; skipped without Node; checks `../for_testing` runs it) |
| Stock log view: every product's movements newest first with product, SKU, type, change, stock after, reason, admin; filters (SKU contains, reason contains: case, file names, `%` / `_` literal, spaces, combined, kept in the form and page links; type, one product from its page), odd parameters, 50 per page with links keeping the filters, typed filters escaped, other stores not listed, empty log in Spanish, Inventory tab and links, admin only | `tests/test_stock_log.py` |
| Inventory import: prices (an edited price alone, stock and price in one row, an empty cell kept, bad prices refuse the whole row: comma, negative, decimals, shape, too large; a price edited after a repricing refused, unedited prices keep the new one, force; a refused stock doesn't write the row's price; edited "price at export"; a file needs a stock or a price column; report and command line show price changes); round trip through the real export (Spanish headers) sets only the edited stock, logs it (file name, admin), touches nothing else (names, prices, descriptions, properties), same file twice changes nothing; headers of any language / ids / `qty`, SKU case ignored, Excel-style `;` + Windows-1252, extra columns and blank lines; refused rows reported with line and reason while the others apply (unknown SKU, duplicate SKU (every occurrence, lines listed), negative, empty, not whole, too large, no SKU, another store, edited `stock_at_export`, another store's SKU); a row edited after the stock changed since the export is refused (unedited rows keep the sale), `force` applies; whole file refused with nothing applied (empty, binary, no rows, missing / duplicate columns, unreadable, too large, too many rows, unknown store); messages in both languages; admin page (links, report, Spanish round trip, force box, `400` / `413`, login, `404`, empty store), command line (exit 0 / 3 / 1, force, unreadable file) and the documented export → edit → import as real processes | `tests/test_inventory_import.py` |
| Stock adjustments: add / remove by a delta with a reason, logged (type, change, stock after, reason, admin); down to exactly 0 allowed, **below zero refused** with nothing written (service, list row, product page), also against a stale copy (the conditional update decides), over the maximum refused; form validation (empty, 0, decimals, separators, non-ASCII digits, too large, reason required / max 200, `+5` accepted); list: a control per row (stepper input, reason, Save, script), returns to the list with its filter and the row, refusal shown open in that row only with the input kept, Spanish by default; product page: adjust + log newest first (kinds, escaped reason, admin email), 50 latest with a "showing" note; the public `in_stock` follows; product form edits are logged and never overwrite an adjustment made after the form was opened (`409` when the number was changed), older clients without `stock_seen`; admin only, other stores `404`; deleting a product deletes its log; database checks; every message translated. Solares defaults: the pack gives each product a SKU and stock 1 (logged as pack import, `in_stock` true), every re-import overwrites the stock with the pack's number (logged; unchanged products log nothing), a pack without `stock` keeps it, a failed (conflicting) pack changes no stock, an older database gets SKUs and stock with one re-import, setting a number that is already there is not a conflict, packs without SKUs keep admin SKUs, a SKU held by another product is a conflict with nothing written, SKUs can swap between the pack's products, pack `sku` / `stock` validated (format, duplicates ignoring case, range, types), the import record counts them | `tests/test_stock.py` |
| Admin form messages in the page language: store, theme, category and property-definition forms (Spanish by default, `?lang=en` English, each message under its field), category delete blockers, every error code of their services has a Spanish and an English message | `tests/test_admin_form_errors.py` |
| Public product API `in_stock`: true / false from the stock on list and detail (a real boolean, no session needed), follows admin edits, pack products without stock are `false`, the count and the SKU are never in the responses, hidden products stay `404` whatever their stock, the fields are exactly the response models and the README's field table | `tests/test_product_api.py` |
| Content app link: `media_url` (content paths get `CONTENT_BASE_URL`, absolute URLs and site paths unchanged), setting validation, product page `<img src>` with the setting set | `tests/test_content_media.py` |
| The solares pack: validation command passes, file name and slug, tube count covers 8–20 and pressure is high / low (enums, required), gas category, theme different from every other fixture, every image value points at a shipped file; import adds rows only (schema unchanged), report (6 images, 4 storefront files, nothing missing), store and product pages use the pack templates and theme in Spanish and English, every page renders and every image loads, documented CLI command | `tests/test_solares_pack.py` |
| The solares pack end to end with real processes and HTTP: the documented validation and import commands run from a copy of the project (pack folder → storefront copied, 6 images published, clothing-demo without a folder), the content app and Magic Stores run as servers with `CONTENT_BASE_URL` = the content app; the API shows the store, categories and every product's tube / pressure values; `/stores/solares` and a product page use the copied storefront and `clothing-demo` stays on `_default`; every product page's `<img src>` is on the content app and returns the JPEG (200), and Magic Stores doesn't serve `/content/` itself. Takes ~15 s (two servers start) | `tests/test_solares_e2e.py` |
| The solares pack in-process: API (store, categories, tube / pressure values of every product), template / static resolution (solares' `store.html`, `product.html`, `style.css` from its folder, `category.html` from `_default`; clothing-demo all `_default`) | `tests/test_solares_pack.py` |
| No warnings from our own code (inserts used the deprecated `datetime.utcnow`) | `tests/test_warnings.py` |
| Stores without a storefront folder stay on `_default` (templates, `style.css`, pages) in every case: created in admin, pack without `storefront`, `null`, `.json` upload of a pack that declares a folder, `.zip` without `storefront/`, a slug that starts like another store's, with solares' override installed next to them | `tests/test_default_storefront.py` |
| Files failing *after* the commit (images or storefront): the catalog is imported, the attempt is `succeeded`, the report / record / upload message list the problem and the images become "missing", staging is cleaned up | `tests/test_import_file_problems.py` |
| Admin upload end to end, as a user does it: a `.zip` of the pack folder (with or without a top folder, macOS junk ignored) publishes `media/` and copies `storefront/`, and every `<img src>` of the store pages loads (200) from the real content app; the record keeps the zip; a `.json` upload lists the missing images in the message and the record (none once they exist); unsafe zips refused with nothing written (`..`, absolute, drive, backslash, symlinks, no / several pack files, not a zip, too many entries, zip bomb, over 50 MB = 413) | `tests/test_upload_images.py` |
| Pack images: `media/` lands in `CONTENT_ROOT/<slug>/` (same bytes, subfolders, hidden files skipped), product image and header logo `src` = `CONTENT_BASE_URL/content/<slug>/…` and the real content app serves those paths, absolute logo URL unchanged, re-import merges, command line + record, upload publishes nothing, empty / missing `CONTENT_ROOT`; bad paths refused with nothing written (media symlinked outside the pack, symlinks or non-images inside, store folder or subfolder symlinked elsewhere, also when it changes after staging, bad slugs); failed import publishes nothing; missing images found by exact name on every OS (a wrong-case name is missing on a case-insensitive file system, as on macOS) | `tests/test_media_publish.py` |
| Pack storefront folder: `storefront-demo` lands in `app/storefronts/<slug>/` and its pages use it (template + static file), stores without one use `_default`, picked up without restart, re-import replaces, `null` leaves the folder alone, command line + record, upload reported as skipped; unsafe paths (`..`, absolute, `~`, symlinks out of / inside the folder, bad slugs) refused with nothing written; hidden files not copied; failed import installs nothing | `tests/test_storefront_copy.py` |
| Store pack import: generic fixture visible in the API / storefront / admin, typed values, category refs, no schema change, re-import upsert (idempotent, updates, removed values, untouched extra rows, category move), type-change conflict + rollback | `tests/test_store_pack_import.py` |
| Feature docs vs code (shipping, payment terminals, payments, their admin pages): every route documented and every documented `/api` / `/admin` path real (no removed routes left), error `type` codes, statuses, request fields and JSON examples = the code's models, quoted limits / windows / page sizes / headers / default terminals = the constants, the solares shipping table and demo amounts = the pack, store tabs = the template, schema table = every table, file paths exist, the callback signing recipe (bash + openssl) gives the accepted signature | `tests/test_feature_docs.py` |
| Store pack docs vs code: `fixtures/packs/README.md` example commands print exactly the documented output, quoted errors are real, field tables = accepted fields and required fields, limits = constants, file and status lists complete | `tests/test_pack_docs.py` |
| Store pack robustness: every single-field change to both fixtures (deleted / wrong type) gives issues, never a crash; every variant that validates imports cleanly | `tests/test_store_pack_validation.py`, `tests/test_store_pack_import.py` |
| Store pack v1 contract: fixtures valid; bad version, missing `store.slug` and other rule violations rejected with located errors; strict JSON files | `tests/test_store_pack_validation.py` |
| Admin Translations tab: Spanish default, lists every translatable text with its original, save reaches the storefront (admin / API keep the source), empty removes, unchanged = 0 changes, one invalid field saves nothing (translated error), other stores' fields ignored, >1,000 fields, store UI override set / update / delete, invalid overrides rejected in Spanish, admin only | `tests/test_admin_translations.py` |
| Both translation levels case by case: level 1 alone, override order and scoping, content fallback field by field, enum chain, source language kept in admin / API, pack i18n row counts and rules, service limits, cleanup | `tests/test_translation_levels.py` |
| Unknown stores, categories, products, pages, assets: 404 on the storefront and API; admin ids too large for a database integer (e.g. `/admin/imports/99999999999999999999`, either sign) are unknown ids (404, or the usual "choose …" form error), never a 500 | `tests/test_not_found.py` |
| Shipping quotes: flat rate for an active method (full response), unknown and inactive methods rejected (`422` with their `type`), free-shipping threshold, country restriction, another store's method is unknown, unknown store `404`, malformed bodies, active-only sorted method list, store delete removes its methods | `tests/test_shipping.py` |
| Payment terminals: a fixture store has `mock_card` and `mock_transfer` (names, order, enabled); a disabled one is still listed with `"enabled": false`; stores created in admin or from a pack get both, a re-import leaves them as they are, app start adds missing ones to older stores (idempotent, disabled stays disabled), unknown store `404`, store delete removes them | `tests/test_payment_terminals.py` |
| Solares shipping from its pack: a fresh database gets `standard` / `express` at the first start (`SEED_PACKS`) and any import brings them (report + record counts); GET methods returns them and quotes follow the pack's rates, free-shipping amount and countries (expected values read from the pack); the try-payment demo uses them with no admin setup; an older database gets them with the documented re-import (admin-made methods kept); re-import overwrites the pack's methods in place; packs without the section leave methods alone; contract errors located (code, amounts, countries, types, unknown fields, `i18n`) with nothing written; defaults; English names: pack `i18n` becomes translations, admin Shipping / Try payment pages in English show them (original as a note; Spanish pages show the original), the form edits the original, the Translations tab lists and saves them, the JSON API keeps the original, deleting a method deletes them | `tests/test_solares_shipping.py` |
| Operator demo **Try payment** on solares (MXN): shipping method created in admin → quote (1,000 + 150 = 1,150 MXN, same as the quote API) → mock payment of the total (random `pay_…` id in the URLs) → succeed with a reference / fail, checked in the payments API; the simulator refuses non-mock terminals (`409`, "Real terminal" shown); a final payment can't be answered again (`409`); free shipping pays the subtotal; the amount is quoted again at pay time (a rate changed meanwhile is used); currency follows the store (MXN, EUR); refusals explained and nothing paid (country, unknown / inactive method, comma, empty, total 0, disabled terminal); other stores' payments `404`; admin only; Spanish by default | `tests/test_admin_try_payment.py` |
| Payments locked down: create (`pending`, store's currency, random `pay_…` id) and read need an admin session (anonymous `401` as JSON before any lookup, customers `403`); succeed / fail only through the terminal's signed callback (admin session alone `401`, old `/succeed` `/fail` routes gone); wrong / missing / forged signatures, bodies changed after signing, timestamps outside the replay window, no secret configured (`503`), oversized (`413`) and malformed bodies refused with nothing changed; a short secret stops the app; database ids never work as payment ids; cross-site form / `text/plain` bodies refused; replayed answer is a no-op, the other answer `409`; concurrent answers: one wins; unknown terminal `422`; older databases get random ids at startup | `tests/test_payments.py` |
| Bank transfer (offline): CLABE control digit (published example, typos, full-width digits), deposit details cleaned and checked (every error code), 7-digit references unique per store (skipped when taken; database constraint, other stores free); solares as shipped (bank_transfer enabled, mocks disabled and refused, no bank details in the terminal list); checkout gives a pending payment with reference and the instructions copy (order API the same, no stock taken), a reference per payment, mock payments without them, later edits don't change earlier instructions, incomplete details refuse checkout and the payments API (`terminal_not_configured`, nothing created, cart kept); the signed callback refused (`409 operator_confirmation_required`, nothing changes), `mark_payment` needs an operator, Try payment never answers it; admin payment page (reference, instructions, order lines), confirm (note, admin recorded, order paid, stock taken once), reject (no stock), twice harmless, the other answer `409`, note limit, mock payments not decided there, unknown / foreign `404`, login / customer `403`, list shows the reference, Spanish default; order page in Spanish with every instruction (`noindex`), after the decision, `404`s; cart page texts; admin terminal form (show, save cleaned, each invalid field refused with nothing saved, mocks have no details, Spanish); pack `payment_terminals` (every validation error, import counts and summary, packs without it, re-import keeps the bank details); startup catch-up for an older solares (once; a re-enabled mock stays), schema upgrade | `tests/test_bank_transfer.py` |
| Low stock: the default store threshold, the product's own wins (0 = only when out), threshold parsing (empty, negative, decimals, separators, signs, non-ASCII digits, too large); the list (out of stock first, fewest units, own / store's threshold, hidden products marked, not-low products absent, empty state, count link on the inventory page, Spanish default, admin only, unknown store `404`); setting the store threshold (saved, flash, list follows, each bad value refused with nothing saved and what was typed kept, missing field); the product form (shows, sets, clears to the store's, an older form without the field keeps it, bad values refused with nothing saved, on create); "Low" in the product list; older databases upgraded | `tests/test_low_stock.py` |
| Shopper's language at checkout: the cart page gives the shipping method and terminal names in its language (English translations with `?lang=en`, Spanish by default, none on other pages); cart and order lines carry `name_translations`, the transfer instructions `note_translations` (a copy: later edits only reach new payments; none without a note); the order page in English and in Spanish (note, shipping method, line names; a deleted method shows the kept name); the Translations tab lists terminal names and notes (note limit); pack terminal `i18n` (import, re-import replaces names and keeps the note with the details, note written with new details, validation errors); startup fills missing terminal translations of an older database (logged; never replaces, skips renamed texts) | `tests/test_shopper_language.py` |
| Phones: the cart's tap targets (≥ 44 px) and 16 px fields, cart lines as cards and a one-column checkout in the phone block only (desktop rules unchanged, the cells and labels exist in `cart.js`), solares and `_default` fit a phone (images, 10 px margins, 17 px text, non-sticky wrapping header, 44 px links, one-column lists), the viewport meta, solares' files identical to the pack's | `tests/test_storefront_mobile.py` |
| `../for_testing` and `scripts/pack_archive.sh` are executable on disk and recorded as `100755` in git | `tests/test_scripts.py` |
| Health endpoint and the empty platform home | `tests/test_health.py` |
| Schema invariant: two verticals (clothing, chargers) in one database without migrations | `tests/test_schema_invariant.py` |
| Storefront resolution: store file first, then `_default` (static files, templates, extra pages) | `tests/test_storefront_resolution.py` |
| The solares store without importing: its storefront and images are committed, identical to the pack; the app loads it at the first start (`SEED_PACKS`, default solares; API, pack storefront, photos from the content app, record with source `startup`), later starts never change it (admin edits kept, no new attempt), the imports screen shows the source, empty setting = empty catalog, missing / invalid packs are logged and don't stop the app, paths independent of the current folder | `tests/test_seed_packs.py` |
| README completeness: every setting documented (README + `.env.example`), every test file in this table, every fixture path mentioned exists, the import comparison is present | `tests/test_readme.py` |
| Translation levels: JSON alone, platform / store UI overrides and their order, Spanish fallback, flash messages, validation, content translations on the storefront (not in admin / API), cleanup on delete, pack `i18n` / `ui_translations` import and re-import | `tests/test_translations.py` |
| Language: Spanish by default, `?lang=en`, cookie, fallback to Spanish (unknown codes, missing keys), `Store.locale_default`, catalog completeness | `tests/test_i18n.py` |

The suite is self-contained: `tests/conftest.py` makes `app` resolve to this project whatever folder pytest is started from (or if `pytest.ini` is not picked up, e.g. by an IDE), and pins test settings, so a local `.env` (real database, changed admin password, `SESSION_HTTPS_ONLY=true`) doesn't affect it and no database file is created. Every test file also passes on its own (`pytest -q tests/test_auth.py`). For speed, tests hash passwords with a low PBKDF2 iteration count (hashes store their count, so behaviour is identical); `tests/test_auth.py` pins the production value of 600,000 and hashes with it for real.

Tests also insert arbitrary verticals (clothing / chargers) through the ORM to prove domain changes do not require schema migrations.

Documentation is tested too: `tests/test_readme.py` (settings, test files, fixtures), `tests/test_pack_docs.py`
(the pack contract) and `tests/test_feature_docs.py` (shipping, terminals, payments) compare what the READMEs say
with the code, so changing one without the other fails the suite (`tests/markdown_doc.py` reads the markdown).

Interface text in assertions comes from the JSON catalogs, not copied into the test. `tests/ui_text.py` is the only
place for it (test modules don't define their own helpers):

| Helper | Returns | Compare with |
| --- | --- | --- |
| `en(key, **params)`, `es(…)`, `ui(locale, …)` | The catalog text, placeholders filled | `page_text(response)` (the page with `&#34;` etc. decoded) |
| `en_html(…)`, `es_html(…)`, `ui_html(…)` | The same, HTML-escaped like the templates | `response.text` (raw HTML; needed by tests about escaping itself) |
| `distinct(key)` | (Spanish, English) escaped; fails if both are equal | pages whose language is being checked |
| `pattern(locale, key)` | A regex of the message with any placeholder values | "this message is not shown" checks |

Every lookup requires the key to exist in that catalog, so a typo fails instead of silently matching nothing; rewording
a message in `app/locales/*.json` doesn't break tests, while showing the wrong message still does. Likewise, expected
pack data (names, rates) is read from the pack file (e.g. `tests/test_solares_shipping.py`). Texts that are not in the
catalogs stay literal on purpose: command-line and validator output, and the solares pack's own content in
`tests/test_solares_pack.py` / `tests/test_solares_e2e.py`, which exist to pin what that pack contains. Tests of the
translation functions themselves (`tests/test_i18n.py`, `tests/test_translation_levels.py`) call `app.i18n` directly.

## Schema (summary)

| Table | Role |
| --- | --- |
| `stores` | Store + theme + locale + low-stock threshold (units, default 2) |
| `categories` | Tree per store |
| `property_definitions` | Field schema per category |
| `products` | Items (slug, name, price, status, the store-scoped `sku`, `stock_on_hand` and an optional own `low_stock_threshold`; everything else is property values) |
| `property_values` | Typed values (JSON) |
| `media_assets` | Media stub |
| `translations` | Level 2 translations: UI overrides (platform / store) and per-field content translations |
| `pack_imports` | One record per store pack import attempt: status, file, store, counts, issues, and the exception of an unexpected error |
| `users` | Login accounts (email, password hash, role) |
| `shipping_methods` | Flat-rate shipping methods per store (code, name, active, order, rate, free-shipping threshold, countries); see "Shipping quotes" |
| `payment_terminals` | Payment terminals per store (`mock_card`, `mock_transfer`; solares also `bank_transfer`), each with a display name, kind (mock / bank transfer), enabled flag and config (bank deposit details); see "Payment terminals" |
| `stock_movements` | Stock movement log: product, type (initial / form edit / adjustment / pack import / inventory import), change, stock after, reason, admin, time; see "Stock adjustments and the movement log" |
| `carts` | Public carts: store, hash of the cookie token, times; see "Cart (public)" |
| `cart_lines` | A cart's products and quantities (one line per product), with snapshots of name (and its translations), SKU and price |
| `cart_requests` | `Idempotency-Key`s of "add to cart" and checkout requests (24 hours): rage clicks and retries act once |
| `orders` | Checkout results: public `ord_…` id, store, cart, copies of the shipping choice and quote, subtotal / shipping / total, the payment, when its stock was taken (payment succeeded); status read from the payment; see "Checkout" |
| `order_lines` | An order's products as bought: product (nullable), SKU, name (and its translations), unit price, quantity, line total, units short when paid |
| `payments` | Payments: store, terminal, amount + currency, status (`pending` → `succeeded` / `failed`), optional mock reference; for a bank transfer its reference, instructions copy, deciding admin and note; timestamps; see "Payments (mock)" |

No table has a vertical-specific column. A business pack adds **rows**, not columns: e.g. the solares pack's tube count and pressure are `property_definitions` rows (`tubos`, `presion`, enums) with `property_values` rows per product. `tests/test_solares_pack.py` checks that the schema after importing it is exactly the engine's.

## Storefronts (per-store front)

```
app/storefronts/
  _default/           # shared base: templates + static + pages/
  <store-slug>/       # optional override: copied from a store pack, or added by hand
```

Resolution: use the store file when present; otherwise `_default`. `_default/templates/base.html` also carries the
public cart (header link, "Add to cart" under product pages, the scripts; see [Cart (public)](#cart-public)): a store
that overrides `base.html` must keep those parts.  
Assets: `/stores/<slug>/assets/style.css`  
Extra pages: `/stores/<slug>/pages/<page>`

**From a store pack.** When a pack sets `storefront.copy_from_pack_dir` (e.g. `"storefront/"`, relative to the pack file)
and is imported **with its folder** (command line, `import_store_pack_file`, or a `.zip` upload), that folder is copied to
`app/storefronts/<store-slug>/`:

```bash
python -m app.services.store_pack_import fixtures/packs/storefront-demo/storefront-demo.store-pack.v1.json
#   ... storefront: 2 file(s) copied to /…/app/storefronts/storefront-demo
```

- The copy **replaces** the store's folder (files an earlier copy had and the pack no longer has are removed, so a
  stale override can't linger). It is installed only after the database import succeeds; a failed import leaves the
  previous folder as it was. Running apps pick up the new templates on the next request, no restart needed.
- `null` (or no `storefront`): nothing is copied and the store folder is left alone, so the store uses `_default`
  unless someone added `app/storefronts/<slug>/` by hand. To drop an override, delete that folder.
- Paths are checked on disk (not only by the validator): the folder must be inside the pack folder (no `/…`, `~`,
  `..`, and no symlink leading outside), symbolic links inside it and special files are refused, hidden files
  (`.DS_Store`, `.git/`) are not copied, and the destination is always `app/storefronts/<valid-slug>/`.
  A missing or unsafe folder stops the import (`conflict`) before anything is written.
- A `.json` upload in admin has no folder, so nothing is copied (reported as skipped); a `.zip` upload copies it.
- A store without its own folder always uses `_default`, however it was created (admin, a pack without `storefront`,
  `null`, a `.json` upload): `tests/test_default_storefront.py`.
- Templates in a pack are code that runs on the server (Jinja): only import packs you trust.

See `app/storefronts/README.md` and `fixtures/packs/`.

## Store packs (contract v1)

A store pack is a JSON file (`<store-slug>.store-pack.v1.json`) with a store, its theme, categories with property definitions, and products with their values. Packs can be **validated** and **imported** into the database, from the command line or by uploading them in admin (`/admin/packs`, see "Import a store pack (upload)").

- Required: `version` (exactly `1`), `store.slug`, `store.name`, `store.currency`; each category needs `slug` and `name`; each property definition needs `key`, `label` and `prop_type` (enums also need `config.options`); each product needs `slug`, `name`, `category_slug` and `price_cents`.
- Fixtures in `fixtures/packs/`: `store-pack.v1.schema.example.json` (skeleton), `clothing-demo.store-pack.v1.json` (generic pack), `demo-widgets.store-pack.v1.json` (every property type + translations), the folder `storefront-demo/` (a tiny pack with a storefront override that is copied on import, see "Storefronts"), and the folder `solares/` (a business pack: solar water heaters with tube count and pressure as property definitions, a gas category, its own theme, storefront and images; import it with `python -m app.services.store_pack_import fixtures/packs/solares/solares.store-pack.v1.json`).
- Validate: `python -m app.services.store_pack_validation fixtures/packs/*.store-pack.v1.json`, or `validate_store_pack(data)` / `check_store_pack(data)` / `read_store_pack(path)` from `app/services/store_pack_validation.py`. Errors name the field, e.g. `store.slug: required`, `version: unsupported version 2; this engine reads store packs with "version": 1`.
- Import: `python -m app.services.store_pack_import fixtures/packs/clothing-demo.store-pack.v1.json` (uses `DATABASE_URL`), or `import_store_pack(db, data)` / `import_store_pack_file(db, path)` from `app/services/store_pack_import.py`. It uses only the engine tables (no migrations), and each pack is imported all-or-nothing.
- Optional `shipping_methods`: the store's flat-rate shipping methods (code, name, rate, free-shipping amount, countries, order, active), same rules as the admin form.
- Re-import **upserts**: rows are matched by slug / key / shipping code and updated in place, and a product's values are replaced with the pack's. Nothing the pack doesn't mention is deleted. A property type can't change while products outside the pack use it.
- Storefront folder: `storefront.copy_from_pack_dir` is copied to `app/storefronts/<slug>/` when the pack is imported from its file (see "Storefronts").
- Images: a `media/` folder next to the pack file is published to the content app, `CONTENT_ROOT/<slug>/`, when the pack is imported from its file; the pack refers to the images as `/content/<slug>/<file>` (image values, `theme.logo_url`), shown through `CONTENT_BASE_URL`. Re-import merges (other files there are kept). Rules and path safety: "Images (`media/`)" in `fixtures/packs/README.md`.

The full contract (every field, type, default and rule) and the re-import rules are in `fixtures/packs/README.md`.

### Importing a pack: command line or admin upload

A pack is a folder; only the JSON file is required:

```
solares/                          # fixtures/packs/solares/, one pack per folder
  solares.store-pack.v1.json      # store, theme, categories + property definitions, products, translations
  storefront/                     # optional, "copy_from_pack_dir": "storefront/" -> app/storefronts/solares/
    templates/store.html, templates/product.html, static/style.css, static/logo-solar.jpg
  media/                          # optional, images -> CONTENT_ROOT/solares/ (served by the content app)
    logo-solar.jpg, tubo-8-alta.jpg, …   # referenced in the JSON as "/content/solares/<file>"
```

There are two ways to import it. Both run the same pipeline (`app/services/pack_import_log.py`): validate → import in
one transaction → install the files → check the images → record the attempt in `pack_imports`.

| | **Command line** | **Admin upload**, `.json` | **Admin upload**, `.zip` of the folder |
| --- | --- | --- | --- |
| How | `python -m app.services.store_pack_import <pack file>` (from `magic_stores/`; several files allowed) | `/admin/packs`, choose the `.json` | `/admin/packs`, choose the `.zip` |
| Catalog (store, categories, properties, products, translations) | Imported | Imported | Imported |
| `storefront/` → `app/storefronts/<slug>/` | Copied | **Not copied** (no folder; reported) | Copied |
| `media/` → `CONTENT_ROOT/<slug>/` | Published | **Not published** (no folder) | Published |
| Images the pack uses but that aren't in `CONTENT_ROOT` | `missing media: …` line | Listed in the success message | Listed in the success message |
| Existing store | Always updated (upsert) | Refused unless "Update the store if it already exists" is ticked | Same as `.json` |
| Limits | None (local files) | 5 MB | 50 MB zip; 2,000 entries, 200 MB unpacked; unsafe entries refused |
| Record | `pack_imports`, source `cli` | `pack_imports`, source `upload`, with the admin | Same, with the zip's size and hash |

```bash
# Command line (from magic_stores/; DATABASE_URL / CONTENT_ROOT from .env or the environment)
python -m app.services.store_pack_validation fixtures/packs/solares/solares.store-pack.v1.json
python -m app.services.store_pack_import fixtures/packs/solares/solares.store-pack.v1.json
#   storefront: 4 file(s) copied to /…/app/storefronts/solares
#   media: 6 image(s) published to /…/magic_stores_content/content/solares

# Admin upload of the folder (browser: /admin/packs; or curl with a signed-in cookie jar)
cd fixtures/packs && python -m zipfile -c /tmp/solares.zip solares/ && cd -
curl -b jar.txt -F "pack=@/tmp/solares.zip" -F "update_existing=1" http://127.0.0.1:8000/admin/packs
```

Both are all-or-nothing for the catalog: a bad pack, a conflict, or a missing / unsafe `storefront/` or `media/`
folder stops the import before anything is written. The files are checked and staged before the commit and installed
right after it; if installing fails then (disk full, a folder changed meanwhile), the catalog stays imported, the
attempt is `succeeded`, and the report lists the files under `problem:` / "some files could not be installed". Then
the storefront is served at `/stores/<slug>` (with the pack's templates, if any; `_default` otherwise) and its images
come from the content app through `CONTENT_BASE_URL`. Rules, path safety and re-import behaviour:
"Storefront folder" and "Images (`media/`)" in `fixtures/packs/README.md`.

### Known limitations (packs, storefronts, images)

- Templates in a pack are Jinja code run by the server, and `.svg` images are served as they are: only import packs you trust.
- The command line has no size limit for a pack folder (it reads local files); the upload has the limits above.
- The JSON API (`/api/…`) and the admin product form show image values as stored (`/content/…`), not as full URLs.
- Stores can't be deleted in admin. If a store is removed from the database by hand, its `app/storefronts/<slug>/`
  and `CONTENT_ROOT/<slug>/` folders stay; delete them too, or a new store with that slug would use them.
- Imported storefront folders and images are written inside the two projects (`app/storefronts/`,
  `magic_stores_content/content/`), so `git status` shows them as new files. The solares ones are committed (identical
  to the pack, checked by `tests/test_seed_packs.py`): loading solares changes no file.

## Packaging

```bash
./scripts/pack_archive.sh
```

Exclude `.git`, `venv`, `__pycache__`, `.env`, and `*.db` from any archive you share. The archive includes `app/`, so storefront folders copied from packs into `app/storefronts/<slug>/` are part of it.
