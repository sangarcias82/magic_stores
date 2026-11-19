# Store packs (contract v1)

> This page is checked against the code by `tests/test_pack_docs.py`. The example commands must print exactly the output shown, the quoted errors must be the real messages, the field tables must list exactly the fields the validator accepts (with the right ones marked required), and the limits must be the code's constants. If you change the contract, update this page or the suite fails.

A store pack is one JSON file that describes a store: its settings and theme, categories with their property definitions, and products with their values. The engine can **validate** a pack and **import** it into the database (see [Importing a pack](#importing-a-pack)), from the command line or by uploading it in admin at `/admin/packs` (the `.json`, or a `.zip` of the pack folder to include its storefront and images; the main README compares the two ways under "Importing a pack: command line or admin upload").

A versioned pack materializes:

1. Catalog data (store, categories, property_definitions, products, …)
2. Optionally a **dedicated storefront**: `storefront.copy_from_pack_dir` names a folder next to the pack file, which the importer copies to `app/storefronts/<slug>/` (see [Storefront folder](#storefront-folder)):

3. Optionally **images**: a `media/` folder next to the pack file, published to the content app (see [Images (`media/`)](#images-media)).

```
pack/                         # one pack per folder
  <slug>.store-pack.v1.json   # "storefront": {"copy_from_pack_dir": "storefront/"}
  storefront/                 # copied to app/storefronts/<slug>/ (served by Magic Stores)
    templates/                # overrides (e.g. store.html, product.html)
    static/style.css          # look and feel for that store (theme assets: CSS, icons)
    pages/about.html          # extra pages
  media/                      # images: published to CONTENT_ROOT/<slug>/ (served by the content app)
    logo.png                  # -> "logo_url": "/content/<slug>/logo.png"
    products/blue.jpg         # -> image value "/content/<slug>/products/blue.jpg"
```

A store without its own folder uses `_default`.

## Files in this folder

| File | What it is |
| --- | --- |
| `store-pack.v1.schema.example.json` | Skeleton showing the top-level layout (no categories / products) |
| `clothing-demo.store-pack.v1.json` | Generic English pack: store + theme, one category (`shirts`) with enum / text / number / boolean properties, one product with values |
| `demo-widgets.store-pack.v1.json` | Spanish-content pack with `en` translations (`i18n`), nested categories and every property type, including `category_ref` |

The folder `storefront-demo/` is a complete pack folder: `storefront-demo.store-pack.v1.json`, the `storefront/` folder it copies (a `templates/store.html` override and a `static/extra.css`) and a `media/` folder with two images (`logo.png`, the store's `logo_url`, and `tiny-gadget.png`, a product's image value). It is used by `tests/test_storefront_copy.py` and `tests/test_media_publish.py`.

The folder `solares/` is a business pack (solar water heaters, Spanish with English translations, prices in MXN); see [The solares pack](#the-solares-pack).

Pack files are named `<store-slug>.store-pack.v<version>.json`, and the version is also inside the file (`"version": 1`). Every file here must pass validation (`tests/test_store_pack_validation.py`).

## The solares pack

`solares/solares.store-pack.v1.json` (slug `solares`) is a complete pack folder. It lives in its own folder, next to the `media/` and `storefront/` folders it ships: a pack's folders are found next to its file, so a file directly in `fixtures/packs/` would have no images or storefront.

| Part | Content |
| --- | --- |
| `calentadores-solares` | Evacuated-tube solar water heaters. Properties (rows in `property_definitions`, no new columns): `tubos` (enum, required: `8`, `10`, `12`, `14`, `16`, `18`, `20`), `presion` (enum, required: `alta` / `baja`, English `high` / `low`), `capacidad_litros` and `personas` (number), `respaldo_gas` (boolean), `foto` (image). Products: 8 tubes high, 12 low, 16 high, 20 high |
| `calentadores-gas` | Optional gas category: `tipo_gas` (enum: `Gas LP` / `Gas natural`), `capacidad_litros`, `foto`. Product: a 40 L gas boiler |
| Theme | Sky blue `#0077b6`, sunlit yellow `#fff6d5`, sun gold `#ffb703`; logo `/content/solares/logo-solar.jpg` |
| `media/` | The 6 images (logo + one per product), published to `CONTENT_ROOT/solares/`; every image value points at one of them (`/content/solares/<file>`) |
| `storefront/` | `templates/store.html` (solar home with a tube-count guide), `templates/product.html` (photo first, tube and pressure badges), `static/style.css` (the store's whole stylesheet) and `static/logo-solar.jpg` |
| `ui_translations` | The footer and the "Catalog" link, in Spanish and English |
| SKU and stock | Every product has a `sku` (`SOL-8T-ALTA`, `SOL-12T-BAJA`, `SOL-16T-ALTA`, `SOL-20T-ALTA`, `GAS-BOIL-40L`) and an initial `stock` of 1, so the store starts with SKUs and everything in stock (`"in_stock": true` in the API). A database loaded before the pack had them gets them with one re-import (below). Every import of the pack sets each product's stock to 1 again (the pack wins); to push counted numbers, use the inventory import (main README) |
| `payment_terminals` | `bank_transfer` "Transferencia bancaria", in English "Bank transfer" with an English note (enabled; **example** deposit details: bank "Banco Ejemplo", a valid test CLABE; replace them in admin before taking real orders). `mock_card` and `mock_transfer` are listed with `"enabled": false`: unlinked from the store, kept for demos (enable them in admin to run **Try payment**). A database from before this pack had terminals gets them at the next start, once (see "Bank transfer" in the main README) |
| `shipping_methods` | `standard` (3 to 7 business days): MXN 450.00, **free from MXN 10,000.00**, Mexico only. `express` (1 to 2 business days): MXN 1,200.00, never free, Mexico only. Names in Spanish (the store's language) with English ones in `i18n`. Quotes work right after the first start |

**It is there without importing:** the app loads it at startup when the store doesn't exist yet (setting `SEED_PACKS`, default this pack), and its storefront and images are committed where an import puts them (`app/storefronts/solares/`, `../magic_stores_content/content/solares/`). To import it by hand anyway (e.g. after deleting the store, or into another database), validate it, then import it (from the `magic_stores` folder; the second command publishes the images and copies the storefront):

```bash
python -m app.services.store_pack_validation fixtures/packs/solares/solares.store-pack.v1.json
python -m app.services.store_pack_import fixtures/packs/solares/solares.store-pack.v1.json
```

Or upload a `.zip` of the folder at `/admin/packs` (`cd fixtures/packs && python -m zipfile -c /tmp/solares.zip solares/`). Then open http://127.0.0.1:8000/stores/solares (with the content app running on 8001 for the images). `tests/test_solares_pack.py` checks all of the above.

## Validating a pack

```bash
python -m app.services.store_pack_validation fixtures/packs/*.store-pack.v1.json
# fixtures/packs/clothing-demo.store-pack.v1.json: valid
# fixtures/packs/demo-widgets.store-pack.v1.json: valid
# Checked 2 file(s): 2 valid, 0 invalid or unreadable. Nothing was imported.
```

From code (`app/services/store_pack_validation.py`; it only checks, it never writes to the database):

```python
from app.services.store_pack_validation import read_store_pack, validate_store_pack, check_store_pack

issues = validate_store_pack(data)   # [] when valid; else [PackIssue(path, message), ...]
check_store_pack(data)               # returns data, or raises StorePackError(issues)
read_store_pack("x.store-pack.v1.json")  # strict JSON parse + check
```

It only checks: nothing is written to the database (to import, use `python -m app.services.store_pack_import`, below). Each file is reported as `valid` or `INVALID (n issues)` followed by the issues. Exit status: `0` every file is valid, `1` a file is invalid or unreadable, `2` usage error (`--help` describes the command).

Every problem is reported at once, with its location. For example, `clothing-demo` without `store.slug`, with `"currency": "usd"` and with `"size": "XXL"`:

```
store.slug: required
store.currency: must be a 3-letter upper-case ISO code like USD, got "usd"
products[0].values.size: must be one of the options (S, M, L, XL), got "XXL"
```

The one exception is the version: a missing or unsupported `version` is reported alone, because the rest of the file can't be read without it:

```
version: unsupported version 2; this engine reads store packs with "version": 1
```

## Importing a pack

```bash
python -m app.services.store_pack_import fixtures/packs/clothing-demo.store-pack.v1.json
# fixtures/packs/clothing-demo.store-pack.v1.json: imported (record #1)
#   store clothing-demo: created
#   categories: 1 created, 0 updated
#   property definitions: 5 created, 0 updated
#   products: 1 created, 0 updated
#   property values: 5 written, 0 removed
#   translations: 0 written
```

This writes to the database configured by `DATABASE_URL` (tables are created if missing). Each file is imported in its own transaction. Exit status: `0` every file was imported, `1` a file was not imported, `2` usage error (`--help` describes the command). In admin, `/admin/packs` does the same for an uploaded file; it refuses a pack whose store already exists unless "Update the store if it already exists" is ticked. Both record every attempt in the `pack_imports` table (status, issues, and the exception of an unexpected error; see "Import records" in the main README). From code:

```python
from app.services.store_pack_import import import_store_pack, import_store_pack_file
# Low-level: these import without writing a pack_imports record. For a recorded import
# (what the upload and the command line use): app.services.pack_import_log.import_pack_bytes

report = import_store_pack_file(db, "x.store-pack.v1.json")  # or import_store_pack(db, data)
print(report.summary())
```

- The pack is validated first. An invalid pack raises `StorePackError` and nothing is written.
- The import is all-or-nothing: any error rolls back the whole pack.
- Only the engine tables are used: `stores`, `categories`, `property_definitions`, `products`, `property_values`, `translations`, `shipping_methods` and `payment_terminals` (plus one `pack_imports` row per recorded attempt). No table or column is added for a vertical: property definitions are rows, and values are JSON in `property_values.value`, in the same format the admin writes (`{"value": …}`, `{"media_path": …}` for images, `{"category_id": …}` for category references, where the pack's category slug is turned into the category id).

### Re-import: upsert, not replace

Importing a pack again **updates** the store; it does not wipe it and recreate it.

| Rule | Effect |
| --- | --- |
| Rows are matched by natural key | store `slug`; category `slug` in the store; property `key` in its category; product `slug` in the store; shipping method `code` in the store; payment terminal `code` in the store. A match is updated in place, so ids, URLs and category references stay the same; anything else is created |
| The pack wins for what it names | every field of a matched row is overwritten, including admin edits. Optional fields the pack omits get their defaults (no `theme` = default theme, no `description` = none, no `sort_order` = 0), so importing the same pack twice gives the same result |
| A product's values are replaced as a whole | a value that is `null` or missing in the pack is removed from that product; a product moved to another category loses the values of its old category |
| Nothing outside the pack is touched | categories, properties, products and shipping methods the pack doesn't mention (e.g. created in admin) are kept as they are. To remove something, delete it in admin (a shipping method: untick **Active**) |
| Type changes are guarded | a property's `prop_type` can't change while products **not in the pack** have values for it (same rule as the admin form). The import stops with `StorePackImportError` before writing anything; change or delete those values in admin first |
| Product `sku`: set when given, kept when omitted | unlike the other optional fields, a product without `sku` in the pack keeps the SKU it has (e.g. entered in admin). A SKU the pack gives that another product of the store keeps (one outside the pack, or one the pack gives no SKU) stops the import as a conflict before anything is written; SKUs swapped between the pack's own products are fine |
| Terminal `config`: written once | a payment terminal's `config` (bank deposit details) is only written when the terminal has none; its `name`, `enabled` and `sort_order` follow the pack like other fields |
| Product `stock`: the pack wins | every import sets the stock of each product that has `stock` in the pack to that number, overwriting admin edits and adjustments like the other fields; a product without `stock` keeps its stock. Each change is logged as a `pack_import` stock movement (admin product page), in the same all-or-nothing transaction (a refused pack changes no stock), and the report says `stock: N set, M unchanged`. To set only stock numbers, use the inventory import (main README, "Inventory import (CSV)") |

Translations go to the `translations` table (level 2, see "Translation levels" in the main README):

- The `i18n` block of every store / category / property / product / shipping method the pack names **replaces** that row's translations. A row without `i18n` loses its translations, including ones added outside the pack.
- `ui_translations` **replaces** the store's UI overrides. Leaving it out removes them.
- Enum options are translated by position: `i18n.en.config.options[i]` translates `config.options[i]`.

### Uploading the same pack again

Re-uploading a pack never leaves a half-imported store. Every case ends in one of these known states:

| Situation | Result | Database afterwards |
| --- | --- | --- |
| Same file again, "Update the store if it already exists" **not** ticked | `409`: "This exact file was already imported … (record #N)", linking to the earlier successful import | Unchanged (the attempt is recorded as `refused`) |
| A different file for an existing store, box not ticked | `409`: "A store with the slug … already exists" | Unchanged (`refused`) |
| Same file again, box ticked (any number of times) | `303`, "updated from the pack" | Exactly the pack's state for everything the pack names. The store, categories, properties, products and property values keep their ids, and the storefront page and API output are identical to after the first upload. Translation rows are rewritten (same content, new ids). Admin edits to rows the pack names are undone; rows the pack doesn't name are kept |
| Two uploads of the same **new** store at the same moment | One succeeds; the other gets `409` (conflict: the store slug is already taken) | The successful upload only; nothing partial from the other |
| Command line (`python -m app.services.store_pack_import`) | Always updates, like the ticked box | As with the box ticked |

So re-applying a pack is idempotent: running it again with the box ticked changes nothing if nobody edited the store, and otherwise brings the named rows back to the pack's content.

### Storefront folder

`storefront.copy_from_pack_dir` is a folder **relative to the pack file** (e.g. `"storefront/"`). When the pack is imported with its folder (the command line, a `.zip` upload in admin, `import_store_pack_file(db, path)`, or `import_store_pack(db, data, pack_dir=…)`), that folder is copied to `app/storefronts/<store-slug>/`, and the import report says `storefront: N file(s) copied to …`.

| Case | Result |
| --- | --- |
| Folder set, pack imported with its folder (command line, `.zip` upload) | Copied. It **replaces** the store's folder (files of an earlier copy the pack no longer has are removed). Installed only after the database import succeeds; a failed import installs nothing and keeps the previous folder |
| Folder set, no pack folder (`.json` admin upload, `import_store_pack(db, data)`) | Not copied; the report lists it under `skipped` and the upload message says so |
| Installing the copy fails after the database import (disk full, …) | The catalog stays imported (`succeeded`); the report lists it under `problem:` (`file_problems` in the record, "some files could not be installed" in the upload message) |
| Folder missing, outside the pack (`..`, `/…`, `~`, a symlink leading outside), or the pack folder itself | The import stops (`StorePackImportError`, status `conflict`) before anything is written |
| `null` or no `storefront` | Nothing is copied and the store folder is left alone: the store uses `_default` unless `app/storefronts/<slug>/` was added by hand |

Inside the folder, symbolic links and special files are refused, and hidden files and folders (`.DS_Store`, `.git/`) are not copied. Templates are code that runs on the server (Jinja): only import packs you trust.

### Images (`media/`)

Product images and the store logo live in a **`media/` folder next to the pack file** (a convention: there is no JSON field for it). When the pack is imported from its file (command line, `import_store_pack_file`, or `import_store_pack(db, data, pack_dir=…)`), the image files in `media/` (subfolders kept) are published to the content app's folder, **`CONTENT_ROOT/<store-slug>/`**. The content app serves them at `/content/<store-slug>/<path>`, so that is how the pack refers to them:

| Pack file | Published to | Value in the pack | Shown on the storefront as |
| --- | --- | --- | --- |
| `media/logo.png` | `CONTENT_ROOT/<slug>/logo.png` | `"logo_url": "/content/<slug>/logo.png"` | `CONTENT_BASE_URL/content/<slug>/logo.png` (header logo) |
| `media/products/blue.jpg` | `CONTENT_ROOT/<slug>/products/blue.jpg` | image value `"/content/<slug>/products/blue.jpg"` | `CONTENT_BASE_URL/content/<slug>/products/blue.jpg` (product page) |

`/content/…` values pass the contract as they are (a path starting with `/`); absolute `https://…` URLs still work and are shown unchanged. `storefront/static/` is something else: theme assets (CSS, icons) copied with the storefront folder and served by Magic Stores at `/stores/<slug>/assets/…`.

| Case | Result |
| --- | --- |
| `media/` present, pack imported from its file | Published after the database import succeeds (a failed import publishes nothing). The report says `media: N image(s) published to …` |
| Re-import | **Merges**: the pack's files overwrite files with the same path; other files in `CONTENT_ROOT/<slug>/` are kept (like rows the pack doesn't name) |
| Admin upload of a `.zip` of the pack folder | Same as the command line: unpacked into a temporary folder and imported from it, so `media/` is published |
| No pack folder (`.json` admin upload, `import_store_pack(db, data)`) | Nothing is published. The `/content/…` values are imported as they are and use files already there, e.g. published earlier; missing ones are reported (below) |
| Setting `CONTENT_ROOT` empty | The pack is imported; `media/` is listed under `skipped` |
| `CONTENT_ROOT` missing, `media/` outside the pack (symlink), a symbolic link or a non-image file inside it, or `CONTENT_ROOT/<slug>/` (or a folder below it) being a symbolic link or in the way | The import stops (`StorePackImportError`, status `conflict`, issue path `media/`) before anything is written |

Only image files are published: `.avif`, `.gif`, `.jpeg`, `.jpg`, `.png`, `.svg`, `.webp` (any case). Hidden files (`.DS_Store`) are skipped. Nothing is ever written outside `CONTENT_ROOT/<slug>/`.

**Missing images are reported.** After every import, each `/content/<slug>/<file>` the pack uses (image values and `logo_url`) is looked up in `CONTENT_ROOT`. The ones that aren't there would show as broken images: the report lists them (`missing media: …` on the command line, `missing_media` in the import record, and the admin upload message). File names are case-sensitive: `Logo.png` is not `logo.png`, on every OS (also on macOS and Windows, whose file systems ignore case by default; a server on Linux would answer 404).

## Contract v1

General rules:

- `version` must be the number `1`. A missing or different version is the only error reported, because the rest of the file can't be interpreted without it.
- Objects are closed: unknown fields are errors, so typos are caught (`"categoires"`). The exception is `store.theme`, which may have extra keys for custom storefront templates.
- Text limits, slug / key formats, enum options and value types follow the same rules as the admin forms.
- The file must be UTF-8 JSON without duplicate keys, without `NaN` / `Infinity`, and nested at most 64 levels deep (a real pack needs about 7). A deeper file, such as `[[[[…]]]]`, is rejected as invalid rather than crashing the parser.
- `i18n` blocks (content translations) are keyed by a language code such as `en` or `pt-BR` (lower-case language part). Translations are stored per language, so one object can't have two blocks for the same language (`en` and `en-GB`). Translated texts have the limits of the fields they translate, e.g. 200 for a name, 100 for an enum option and 2000 for a text value.
- Validation does not look at the database. For example, a slug that already exists in the database is for the importer to handle.

**Required** fields are in bold; the rest are optional (default in brackets).

### Top level

| Field | Type | Rules |
| --- | --- | --- |
| **`version`** | number | Exactly `1` |
| **`store`** | object | See below |
| `categories` | array [`[]`] | Category objects |
| `products` | array [`[]`] | Product objects |
| `shipping_methods` | array [`[]`] | The store's flat-rate shipping methods (see below) |
| `payment_terminals` | array [`[]`] | The store's payment terminals: mocks to switch off, an offline bank transfer with its deposit details (see below) |
| `storefront` | object | `copy_from_pack_dir`: `null` or a relative folder inside the pack (no `/…`, no `..`), copied to `app/storefronts/<slug>/` on import (see [Storefront folder](#storefront-folder)); `notes`: text |
| `ui_translations` | object | This store's UI text overrides: `{"en": {"store.nav.catalog": "Shop"}}`. Languages must be UI languages (`es`, `en`); keys must exist in `app/locales/es.json`, with the same `{placeholders}` |

### `store`

| Field | Type | Rules |
| --- | --- | --- |
| **`slug`** | string | Lower-case letters, digits and single hyphens, max 64 (`demo-widgets`); `new` is reserved |
| **`name`** | string | Not empty, max 200 |
| **`currency`** | string | 3-letter upper-case ISO code (`USD`) |
| `locale_default` | string [`es`] | A UI language (`es`, `en`; `en-US` counts as `en`). The storefront's language when the visitor doesn't choose one |
| `theme` | object [default theme] | `primary`, `secondary`, `accent`: hex colors (`#1a5f4a`); `logo_url`: `null`, an `http(s)` URL or `/path`; other keys allowed |
| `i18n` | object | Translations: `{"en": {"name": "…"}}` |

### `categories[]`

| Field | Type | Rules |
| --- | --- | --- |
| **`slug`** | string | Slug format; unique in the pack |
| **`name`** | string | Not empty, max 200 |
| `description` | string or null | Max 2000 |
| `sort_order` | integer [`0`] | ±1,000,000 |
| `parent_slug` | string or null [`null`] | Another category of the pack; no cycles |
| `property_definitions` | array [`[]`] | See below |
| `i18n` | object | `{"en": {"name": "…", "description": "…"}}` |

### `categories[].property_definitions[]`

| Field | Type | Rules |
| --- | --- | --- |
| **`key`** | string | Starts with a letter; lower-case letters, digits, `_`; max 64; unique in the category |
| **`label`** | string | Not empty, max 200 |
| **`prop_type`** | string | `text`, `number`, `boolean`, `enum`, `image` or `category_ref` |
| `required` | boolean [`false`] | Products of the category must have a value |
| `sort_order` | integer [`0`] | ±1,000,000 |
| `config` | object [`{}`] | For `enum`, **`options`** is required: 1–200 unique non-empty strings, max 100 characters each |
| `ref_category_slug` | string or null | Only for `category_ref`: values must be this category or one of its subcategories |
| `i18n` | object | `{"en": {"label": "…", "config": {"options": [...]}}}`; translated options are listed in the same order as the originals |

### `products[]`

| Field | Type | Rules |
| --- | --- | --- |
| **`slug`** | string | Slug format; unique in the pack |
| **`name`** | string | Not empty, max 200 |
| **`category_slug`** | string | A category of the pack |
| **`price_cents`** | integer | Cents: `1999` = 19.99; 0 to 99,999,999,999 |
| `description` | string or null | Max 5000 |
| `is_active` | boolean [`true`] | `false` = hidden from the storefront |
| `sku` | string | The product's SKU (admin rules): letters, digits, `-` `_` `.`, starting with a letter or digit, max 64; stored upper-case; unique in the pack ignoring case. Omitted: the product keeps its SKU |
| `stock` | integer | Stock on hand, 0 to 2,147,483,647 units; set on every import (see "Re-import"). Omitted: a new product starts at 0, an existing one keeps its stock |
| `values` | object [`{}`] | `{"<property key>": value}` for the properties of the product's category; `null` or missing = no value (not allowed for required properties) |
| `i18n` | object | `{"en": {"name": "…", "description": "…", "values": {"<key>": "…"}}}`; only text / enum values can be translated |

Value types in `values`:

| `prop_type` | JSON value |
| --- | --- |
| `text` | string, max 2000 |
| `number` | number (`20`, `2.5`) |
| `boolean` | `true` / `false` |
| `enum` | one of `config.options` |
| `image` | `http(s)` URL or `/path`, max 1000 |
| `category_ref` | slug of a category of the pack |

### `shipping_methods[]`

The store's shipping methods, as in the admin **Shipping** tab (same rules) and used by the quote API
(`POST /api/stores/<slug>/shipping/quote`). Amounts are cents in the store's `currency`.

| Field | Type | Rules |
| --- | --- | --- |
| **`code`** | string | Slug format, max 64 (`standard`); unique in the pack. What the quote API takes |
| **`name`** | string | Not empty, max 200 |
| **`rate_cents`** | integer | Flat price: `45000` = 450.00; 0 to 2,147,483,647 |
| `free_shipping_threshold_cents` | integer or null [`null`] | The quote is 0 when the order subtotal is at least this; `null` = never free |
| `countries` | array [`[]`] | 2-letter upper-case country codes it ships to (`["MX"]`), no duplicates; empty = every country |
| `sort_order` | integer [`0`] | Listing order, ±1,000,000 |
| `is_active` | boolean [`true`] | `false` = not offered and refused by quotes |
| `i18n` | object | `{"en": {"name": "…"}}`: the name in other languages (admin pages in that language show it) |

### `payment_terminals[]`

The store's payment terminals, as in the admin **Payment terminals** tab. Every store gets the mock terminals
`mock_card` and `mock_transfer` anyway; a pack lists terminals to add (an offline bank transfer) or to set up
(e.g. `"enabled": false` to unlink the mocks from a real store while keeping them for demos).

| Field | Type | Rules |
| --- | --- | --- |
| **`code`** | string | Lower-case letters, digits and `_`, starting with a letter, max 64; unique in the pack. What checkout and the payments API take (`"terminal": "bank_transfer"`) |
| **`name`** | string | Not empty, max 200. What shoppers and the admin see |
| **`kind`** | string | `mock` (code must start with `mock_`: answered by the signed callback or the admin simulator) or `bank_transfer` (code must **not** start with `mock_`: an offline transfer that only an admin confirms or rejects) |
| `enabled` | boolean [`true`] | `false` = listed but refused for new payments |
| `sort_order` | integer [`0`] | Listing order, ±1,000,000 |
| `i18n` | object | `{"en": {"name": "Bank transfer", "note": "…"}}`: the name and, for a `bank_transfer`, the note in other languages (name max 200, note max 500). Shown to shoppers browsing in that language (checkout choices, deposit instructions) |
| `config` | object | **Required for `bank_transfer`**, not allowed for `mock`. The deposit instructions shown to shoppers: **`bank`** (max 100), **`beneficiary`** (max 200), **`clabe`** (the 18-digit Mexican CLABE, its control digit is checked), `account` (6 to 20 digits), `note` (max 500) |

Re-import: terminals are matched by `code`. `name`, `enabled` and `sort_order` follow the pack like any
other named row (the `i18n` names replace the terminal's name translations), but **`config` is only written to a
terminal that has none**: real bank details entered in admin are never replaced by a pack's example ones. The
`i18n` notes go with the config (written only when it is). The report says `payment terminals: N created, M
updated`.

### Changing the contract

Optional fields that older packs can simply omit may be added within version 1, as long as the validator and this page are updated together. Anything that changes the meaning of existing fields, or makes a new field required, needs `"version": 2`. The validator then has to be extended to read the new version, because version 1 only accepts `1`.
