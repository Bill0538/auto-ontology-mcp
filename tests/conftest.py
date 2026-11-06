from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from auto_ontology_mcp.config import Settings
from auto_ontology_mcp.demo import build_demo_database
from auto_ontology_mcp.service import OntologyService

# What a host model would propose for the demo tables (one Term per table).
DEMO_TERMS: dict[str, list[dict]] = {
    "regions": [{"name": "Region", "description": "A sales region grouping US states.", "columns": [
        {"column": "region_id", "display_name": "Region ID"}, {"column": "region_name", "display_name": "Region Name"}]}],
    "states": [{"name": "State", "description": "A US state where customers live.", "synonyms": ["State Province"], "columns": [
        {"column": "state_id", "display_name": "State ID"}, {"column": "state_name", "display_name": "State Name"},
        {"column": "state_code", "display_name": "State Code"}]}],
    "cities": [{"name": "City", "description": "A city used for customer delivery and billing addresses.", "columns": [
        {"column": "city_id", "display_name": "City ID"}, {"column": "city_name", "display_name": "City Name"},
        {"column": "population", "display_name": "Population"}]}],
    "employees": [{"name": "Employee", "description": "A member of the sales organization.", "synonyms": ["Sales Rep"], "columns": [
        {"column": "employee_id", "display_name": "Employee ID"}, {"column": "full_name", "display_name": "Full Name"},
        {"column": "job_title", "display_name": "Job Title"}, {"column": "manager_id", "display_name": "Manager ID"},
        {"column": "hire_date", "display_name": "Hire Date"}]}],
    "customers": [{"name": "Customer", "description": "A customer account; one row per customer.", "synonyms": ["고객"], "columns": [
        {"column": "customer_id", "display_name": "Customer ID"}, {"column": "customer_name", "display_name": "Customer Name"},
        {"column": "email", "display_name": "Email"}, {"column": "segment", "display_name": "Segment"},
        {"column": "signup_date", "display_name": "Signup Date"}, {"column": "is_active", "display_name": "Is Active"}]}],
    "product_categories": [{"name": "Product Category", "description": "A two-level product category tree.", "columns": [
        {"column": "category_id", "display_name": "Category ID"}, {"column": "category_name", "display_name": "Category Name"}]}],
    "products": [{"name": "Product", "description": "A product in the catalog.", "columns": [
        {"column": "product_id", "display_name": "Product ID"}, {"column": "product_name", "display_name": "Product Name"},
        {"column": "unit_price", "display_name": "List Price"}, {"column": "unit_cost", "display_name": "Unit Cost"},
        {"column": "is_discontinued", "display_name": "Is Discontinued"}]}],
    "tags": [{"name": "Tag", "description": "A merchandising tag.", "columns": [
        {"column": "tag_id", "display_name": "Tag ID"}, {"column": "tag_name", "display_name": "Tag Name"}]}],
    "orders": [{"name": "Order", "description": "A customer order; one row per order.", "columns": [
        {"column": "order_id", "display_name": "Order ID"}, {"column": "order_date", "display_name": "Order Date"},
        {"column": "status", "display_name": "Order Status"}, {"column": "channel", "display_name": "Sales Channel"}]}],
    "order_lines": [{"name": "Order Line", "description": "One product line of an order; revenue lives here.", "synonyms": ["Invoice Line"], "columns": [
        {"column": "order_line_id", "display_name": "Order Line ID"}, {"column": "quantity", "display_name": "Quantity"},
        {"column": "unit_price", "display_name": "Unit Price"}, {"column": "discount_pct", "display_name": "Discount Fraction"},
        {"column": "line_total", "display_name": "Revenue"}]}],
}


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    base = Settings.from_env()
    return replace(base, home=tmp_path, store_path=tmp_path / "ontology.db", token=None)


@pytest.fixture()
def demo_path(tmp_path: Path) -> Path:
    return build_demo_database(tmp_path / "demo.sqlite")


@pytest.fixture()
def svc(settings: Settings, demo_path: Path) -> OntologyService:
    service = OntologyService(settings)
    service.add_datasource("demo", f"sqlite:///{demo_path}", description="demo")
    return service


@pytest.fixture()
def modeled(svc: OntologyService) -> OntologyService:
    """The demo after a host model has reviewed joins and modeled every table."""
    svc.set_relationship("accept", source="employees.manager_id", target="employees.employee_id")
    svc.set_relationship("add", source="orders.sales_rep_id", target="employees.employee_id", role="sales rep")
    svc.set_relationship("update", source="customers.delivery_city_id", target="cities.city_id", is_default=True)
    for table, terms in DEMO_TERMS.items():
        svc.save_table_terms(table, terms)
    return svc
