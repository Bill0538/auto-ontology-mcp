"""A small synthetic retail database for trying the server without your own data.

Some foreign keys are declared, others are deliberately left undeclared so
the relationship inference has something to find, and a few traps mirror real
warehouses: two roles pointing at the same table (delivery vs billing city),
self references (category parent, employee manager), a pure bridge table
(product_tags), and a discount stored as a 0–1 fraction rather than a percent.
There is no advertising data, so "return on ad spend" is unanswerable.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE regions (
    region_id INTEGER PRIMARY KEY,
    region_name TEXT NOT NULL
);
CREATE TABLE states (
    state_id INTEGER PRIMARY KEY,
    state_name TEXT NOT NULL,
    state_code TEXT NOT NULL,
    region_id INTEGER NOT NULL REFERENCES regions(region_id)
);
CREATE TABLE cities (
    city_id INTEGER PRIMARY KEY,
    city_name TEXT NOT NULL,
    state_id INTEGER NOT NULL,          -- undeclared FK → states
    population INTEGER
);
CREATE TABLE employees (
    employee_id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL,
    job_title TEXT NOT NULL,
    manager_id INTEGER,                 -- undeclared self reference
    hire_date TEXT NOT NULL
);
CREATE TABLE customers (
    customer_id INTEGER PRIMARY KEY,
    customer_name TEXT NOT NULL,
    email TEXT,
    segment TEXT NOT NULL,
    signup_date TEXT NOT NULL,
    delivery_city_id INTEGER NOT NULL,  -- undeclared FK → cities (role: delivery)
    billing_city_id INTEGER NOT NULL,   -- undeclared FK → cities (role: billing)
    is_active INTEGER NOT NULL
);
CREATE TABLE product_categories (
    category_id INTEGER PRIMARY KEY,
    category_name TEXT NOT NULL,
    parent_category_id INTEGER REFERENCES product_categories(category_id)
);
CREATE TABLE products (
    product_id INTEGER PRIMARY KEY,
    product_name TEXT NOT NULL,
    category_id INTEGER NOT NULL REFERENCES product_categories(category_id),
    unit_price REAL NOT NULL,
    unit_cost REAL NOT NULL,
    is_discontinued INTEGER NOT NULL
);
CREATE TABLE tags (
    tag_id INTEGER PRIMARY KEY,
    tag_name TEXT NOT NULL
);
CREATE TABLE product_tags (
    product_id INTEGER NOT NULL,        -- undeclared FK → products
    tag_id INTEGER NOT NULL,            -- undeclared FK → tags
    PRIMARY KEY (product_id, tag_id)
);
CREATE TABLE orders (
    order_id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL,       -- undeclared FK → customers
    sales_rep_id INTEGER,               -- undeclared FK → employees
    order_date TEXT NOT NULL,
    status TEXT NOT NULL,
    channel TEXT NOT NULL
);
CREATE TABLE order_lines (
    order_line_id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(order_id),
    product_id INTEGER NOT NULL,        -- undeclared FK → products
    quantity INTEGER NOT NULL,
    unit_price REAL NOT NULL,
    discount_pct REAL NOT NULL,         -- fraction 0–0.3, not a percent
    line_total REAL NOT NULL
);
"""

_REGIONS = ["Northeast", "Midwest", "South", "West"]
_STATES = [
    ("New York", "NY", 1), ("Pennsylvania", "PA", 1), ("Massachusetts", "MA", 1),
    ("Illinois", "IL", 2), ("Ohio", "OH", 2), ("Michigan", "MI", 2),
    ("Texas", "TX", 3), ("Florida", "FL", 3), ("Georgia", "GA", 3),
    ("California", "CA", 4), ("Washington", "WA", 4), ("Colorado", "CO", 4),
]
_CITIES = {
    "NY": ["New York City", "Buffalo", "Rochester"], "PA": ["Philadelphia", "Pittsburgh", "Allentown"],
    "MA": ["Boston", "Worcester"], "IL": ["Chicago", "Springfield"], "OH": ["Columbus", "Cleveland", "Cincinnati"],
    "MI": ["Detroit", "Grand Rapids"], "TX": ["Houston", "Austin", "Dallas", "San Antonio"],
    "FL": ["Miami", "Orlando", "Tampa"], "GA": ["Atlanta", "Savannah"], "CA": ["Los Angeles", "San Francisco", "San Diego"],
    "WA": ["Seattle", "Spokane"], "CO": ["Denver", "Boulder"],
}
_CATEGORIES = [
    ("Electronics", None), ("Home & Kitchen", None), ("Outdoors", None),
    ("Storage Devices", 1), ("Audio", 1), ("Cookware", 2), ("Furniture", 2), ("Camping", 3), ("Cycling", 3),
]
_PRODUCTS = [
    ("USB Flash Drive 64GB", 4, 18.0), ("USB Food Flash Drive - Sushi Roll", 4, 32.0), ("Portable SSD 1TB", 4, 119.0),
    ("Wireless Earbuds", 5, 79.0), ("Bluetooth Speaker", 5, 59.0), ("Noise Cancelling Headphones", 5, 249.0),
    ("Cast Iron Skillet", 6, 39.0), ("Nonstick Pan Set", 6, 89.0), ("Chef Knife", 6, 65.0),
    ("Standing Desk", 7, 399.0), ("Office Chair", 7, 229.0), ("Bookshelf", 7, 149.0),
    ("4-Person Tent", 8, 189.0), ("Sleeping Bag", 8, 79.0), ("Camping Stove", 8, 54.0),
    ("Road Bike Helmet", 9, 69.0), ("Bike Lock", 9, 35.0), ("Cycling Gloves", 9, 25.0),
]
_TAGS = ["bestseller", "eco-friendly", "gift idea", "new arrival", "clearance", "premium"]
_FIRST = ["Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Jamie", "Avery", "Quinn", "Drew", "Sam", "Robin", "Hana", "Min", "Joon", "Seo"]
_LAST = ["Smith", "Johnson", "Lee", "Kim", "Park", "Garcia", "Brown", "Davis", "Miller", "Wilson", "Moore", "Clark"]


def build_demo_database(path: Path, *, seed: int = 7) -> Path:
    """Create (or replace) the demo SQLite database at *path*."""
    rng = random.Random(seed)
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)

    con.executemany("INSERT INTO regions VALUES (?, ?)", [(i + 1, n) for i, n in enumerate(_REGIONS)])
    con.executemany("INSERT INTO states VALUES (?, ?, ?, ?)", [(i + 1, n, c, r) for i, (n, c, r) in enumerate(_STATES)])
    state_id = {c: i + 1 for i, (_, c, _) in enumerate(_STATES)}
    cities = []
    for code, names in _CITIES.items():
        for name in names:
            cities.append((len(cities) + 1, name, state_id[code], rng.randint(50_000, 8_000_000)))
    con.executemany("INSERT INTO cities VALUES (?, ?, ?, ?)", cities)

    employees = [(1, "Dana Whitfield", "VP Sales", None, "2014-03-01")]
    for i in range(2, 5):
        employees.append((i, f"{rng.choice(_FIRST)} {rng.choice(_LAST)}", "Sales Manager", 1, f"201{rng.randint(5, 8)}-0{rng.randint(1, 9)}-15"))
    for i in range(5, 17):
        employees.append((i, f"{rng.choice(_FIRST)} {rng.choice(_LAST)}", "Sales Representative", rng.randint(2, 4), f"20{rng.randint(17, 23)}-{rng.randint(1, 12):02d}-01"))
    con.executemany("INSERT INTO employees VALUES (?, ?, ?, ?, ?)", employees)

    customers = []
    start = date(2021, 1, 1)
    for i in range(1, 241):
        name = f"{rng.choice(_FIRST)} {rng.choice(_LAST)}"
        delivery = rng.randint(1, len(cities))
        billing = delivery if rng.random() < 0.8 else rng.randint(1, len(cities))
        customers.append(
            (
                i, name, f"{name.lower().replace(' ', '.')}{i}@example.com" if rng.random() < 0.9 else None,
                rng.choices(["Consumer", "Corporate", "Home Office"], [6, 3, 2])[0],
                (start + timedelta(days=rng.randint(0, 900))).isoformat(), delivery, billing,
                1 if rng.random() < 0.85 else 0,
            )
        )
    con.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?, ?, ?)", customers)

    con.executemany(
        "INSERT INTO product_categories VALUES (?, ?, ?)", [(i + 1, n, p) for i, (n, p) in enumerate(_CATEGORIES)]
    )
    products = []
    for i, (name, cat, price) in enumerate(_PRODUCTS):
        products.append((i + 1, name, cat, price, round(price * rng.uniform(0.45, 0.7), 2), 1 if i in (2, 14) else 0))
    con.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?, ?)", products)
    con.executemany("INSERT INTO tags VALUES (?, ?)", [(i + 1, t) for i, t in enumerate(_TAGS)])
    pt = set()
    for pid in range(1, len(products) + 1):
        for tid in rng.sample(range(1, len(_TAGS) + 1), rng.randint(1, 3)):
            pt.add((pid, tid))
    con.executemany("INSERT INTO product_tags VALUES (?, ?)", sorted(pt))

    orders, lines = [], []
    order_start = date(2023, 1, 1)
    statuses = ["Delivered"] * 14 + ["Shipped"] * 3 + ["Placed"] * 2 + ["Cancelled", "Returned"]
    for oid in range(1, 3201):
        cust = rng.randint(1, len(customers))
        day = order_start + timedelta(days=rng.randint(0, 729))
        orders.append(
            (oid, cust, rng.randint(5, 16) if rng.random() < 0.7 else None, day.isoformat(), rng.choice(statuses), rng.choices(["Web", "Store", "Phone"], [6, 3, 1])[0])
        )
        for _ in range(rng.randint(1, 4)):
            pid = rng.randint(1, len(products))
            qty = rng.randint(1, 5)
            price = products[pid - 1][3]
            discount = rng.choice([0.0, 0.0, 0.0, 0.05, 0.1, 0.15, 0.2, 0.3])
            lines.append((len(lines) + 1, oid, pid, qty, price, discount, round(qty * price * (1 - discount), 2)))
    con.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", orders)
    con.executemany("INSERT INTO order_lines VALUES (?, ?, ?, ?, ?, ?, ?)", lines)
    con.commit()
    con.close()
    return path
