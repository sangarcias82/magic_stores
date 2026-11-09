from sqlalchemy import inspect


FORBIDDEN_COLUMN_FRAGMENTS = ("tube", "pressure", "size", "solar", "gas_brand", "watts")


def test_no_domain_specific_sql_columns(engine):
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "stores" in tables
    assert "categories" in tables
    assert "property_definitions" in tables
    assert "products" in tables
    assert "property_values" in tables
    assert "solar_heaters" not in tables
    assert "clothing_items" not in tables

    for table in tables:
        for column in inspector.get_columns(table):
            name = column["name"].lower()
            for fragment in FORBIDDEN_COLUMN_FRAGMENTS:
                assert fragment not in name, f"Domain column leaked: {table}.{name}"


def test_clothing_vertical_without_migration(client, clothing_fixture):
    stores = client.get("/api/stores").json()
    assert any(s["slug"] == "clothing-demo" for s in stores)

    cats = client.get("/api/stores/clothing-demo/categories").json()
    shirts = next(c for c in cats if c["slug"] == "shirts")
    keys = {d["key"] for d in shirts["property_definitions"]}
    assert keys == {"size", "color"}

    products = client.get("/api/stores/clothing-demo/products").json()
    assert len(products) == 1
    detail = client.get("/api/stores/clothing-demo/products/linen-shirt-m").json()
    props = {p["key"]: p["value"].get("value") for p in detail["properties"]}
    assert props["size"] == "M"
    assert props["color"] == "Sand"

    html = client.get("/stores/clothing-demo/p/linen-shirt-m")
    assert html.status_code == 200
    assert "Linen shirt" in html.text
    assert "Size" in html.text
    assert "Sand" in html.text


def test_second_vertical_same_schema(client, electronics_fixture):
    cats = client.get("/api/stores/clothing-demo/categories").json()
    slugs = {c["slug"] for c in cats}
    assert "shirts" in slugs
    assert "chargers" in slugs

    charger_cat = next(c for c in cats if c["slug"] == "chargers")
    keys = {d["key"] for d in charger_cat["property_definitions"]}
    assert keys == {"voltage", "watts"}

    detail = client.get("/api/stores/clothing-demo/products/charger-65w").json()
    props = {p["key"]: p["value"].get("value") for p in detail["properties"]}
    assert props["voltage"] == 20
    assert props["watts"] == 65
