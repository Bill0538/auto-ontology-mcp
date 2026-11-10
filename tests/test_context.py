"""What get_query_context, get_term and validate_sql tell the model about columns, and tool groups."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

from fastmcp import Client

from auto_ontology_mcp.profiling import infer_list_separator
from auto_ontology_mcp.server import TOOL_GROUPS, build_server
from auto_ontology_mcp.service import OntologyService


def test_list_separator_needs_items_that_also_stand_alone():
    assert infer_list_separator(["SLE", "RA", "SLE, RA", "RA, PSS", "PSS", "SLE"]) == ","
    # Commas in free text (addresses, names) do not make a list column: the pieces never occur alone.
    assert infer_list_separator(["12 Main St, Springfield", "4 Elm Rd, Shelbyville", "PO Box 9", "1 Oak Ave, Ogden"]) is None
    assert infer_list_separator(["a", "b"]) is None


def _clinic(tmp_path):
    path = tmp_path / "clinic.sqlite"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE patient (id INTEGER PRIMARY KEY, diagnosis TEXT, address TEXT, ward_code TEXT);
        CREATE TABLE visit (id INTEGER PRIMARY KEY, patient_id INTEGER REFERENCES patient(id), ward TEXT);
        """
    )
    diagnoses = ["SLE", "RA", "SLE, RA", "PSS", "RA, PSS", "SLE", "MCTD", "SLE, MCTD"]
    wards = ["North", "South", "East"]
    for i in range(1, 41):
        con.execute(
            "INSERT INTO patient VALUES (?, ?, ?, ?)",
            (i, diagnoses[i % len(diagnoses)], f"{i} Main St, Springfield", wards[i % 3]),
        )
        con.execute("INSERT INTO visit VALUES (?, ?, ?)", (i, i, wards[(i + 1) % 3]))
    con.commit()
    con.close()
    return path


def _modeled_clinic(settings, tmp_path) -> OntologyService:
    svc = OntologyService(settings)
    svc.add_datasource("clinic", f"sqlite:///{_clinic(tmp_path)}")
    svc.save_table_terms("patient", [{"name": "Patient", "description": "One row per patient.", "columns": [
        {"column": "id", "display_name": "Patient ID"}, {"column": "diagnosis", "display_name": "Diagnosis"},
        {"column": "address", "display_name": "Address"}, {"column": "ward_code", "display_name": "Home Ward"}]}],
        datasource="clinic")
    svc.save_table_terms("visit", [{"name": "Visit", "description": "One row per visit.", "columns": [
        {"column": "id", "display_name": "Visit ID"}, {"column": "ward", "display_name": "Visit Ward"}]}],
        datasource="clinic")
    return svc


def test_list_columns_are_shown_and_checked(settings, tmp_path):
    svc = _modeled_clinic(settings, tmp_path)
    attrs = {a["column"]: a for a in svc.get_term("Patient", datasource="clinic")["attributes"]}
    assert "holds_lists" in attrs["diagnosis"]
    assert "holds_lists" not in attrs["address"]

    warnings = svc.validate_sql("SELECT COUNT(*) FROM patient WHERE diagnosis = 'SLE'", datasource="clinic")["warnings"]
    assert any("only matches rows whose whole list is 'SLE'" in w for w in warnings)
    ok = svc.validate_sql(
        "SELECT COUNT(*) FROM patient WHERE instr(',' || replace(diagnosis, ' ', '') || ',', ',SLE,') > 0", datasource="clinic"
    )
    assert not any("whole list" in w for w in ok["warnings"])
    # run_sql carries the same warning without a separate validate_sql call.
    assert any("whole list" in w for w in svc.run_sql("SELECT COUNT(*) FROM patient WHERE diagnosis = 'SLE'", datasource="clinic")["warnings"])


def test_same_field_in_two_tables_is_pointed_out(settings, tmp_path):
    svc = _modeled_clinic(settings, tmp_path)
    ctx = svc.get_query_context(["Patient", "Visit"], datasource="clinic")
    notes = ctx["same_attribute_in_several_tables"]
    assert len(notes) == 1 and "patient.ward_code" in notes[0] and "visit.ward" in notes[0]


def test_complete_value_sets_are_marked_complete(modeled):
    attrs = {a["column"]: a for a in modeled.get_term("Order")["attributes"]}
    assert set(attrs["status"]["all_values"]) == {"Delivered", "Shipped", "Placed", "Cancelled", "Returned"}
    assert "sample_values" not in attrs["status"]
    assert len(attrs["order_date"]["sample_values"]) <= 3
    assert "unique_in_sample" not in attrs["order_date"]


async def test_toolsets_limit_tools_and_instructions(settings, svc):
    answer_only = build_server(replace(settings, toolsets=frozenset({"answer"})), svc)
    full = build_server(settings, svc)
    async with Client(answer_only) as client:
        names = {t.name for t in await client.list_tools()}
    assert names and all(TOOL_GROUPS[n] == "answer" for n in names)
    assert {"get_query_context", "run_sql", "validate_sql", "lookup_values"} <= names
    assert "Keeping personal knowledge" not in answer_only.instructions
    assert "Keeping personal knowledge" in full.instructions
    async with Client(full) as client:
        assert len(await client.list_tools()) > len(names)
