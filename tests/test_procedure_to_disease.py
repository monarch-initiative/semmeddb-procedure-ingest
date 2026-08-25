"""Tests for the SemMedDB procedure->disease transform (Koza 2.x KozaRunner pattern)."""

import pytest
from biolink_model.datamodel.pydanticmodel_v2 import Association, Procedure, Disease
from koza.io.writer.passthrough_writer import PassthroughWriter
from koza.runner import KozaRunner, KozaTransformHooks

import procedure_to_disease as mod
from procedure_to_disease import transform_record


@pytest.fixture(autouse=True)
def _reset_node_dedup():
    """Node dedup is module-level state; clear it so tests don't interfere."""
    mod._seen_procedures.clear()
    mod._seen_diseases.clear()
    yield


def run(rows: list[dict]) -> list:
    writer = PassthroughWriter()
    KozaRunner(
        data=iter(rows),
        writer=writer,
        hooks=KozaTransformHooks(transform_record=[transform_record]),
    ).run()
    return writer.data


def assoc(entities):
    return next(e for e in entities if isinstance(e, Association))


def nodes(entities):
    return [e for e in entities if not isinstance(e, Association)]


DIAGNOSES_SNOMED = {  # bucket-C: procedure grounded to SNOMED, label BLANK (licensing)
    "procedure_id": "SNOMED:84200003", "procedure_label": "",
    "predicate": "diagnoses",
    "disease_id": "MONDO:0005301", "disease_label": "pulmonary embolism",
    "disease_category": "biolink:Disease",
    "support_pmids": "329", "procedure_ic": "13.8", "disease_ic": "10.5",
    "publications": "PMID:11111|PMID:22222",
}

TREATS_NCIT = {  # procedure grounded to NCIT, open label kept
    "procedure_id": "NCIT:C15357", "procedure_label": "Esophagectomy",
    "predicate": "treats",
    "disease_id": "MONDO:0004992", "disease_label": "cancer",
    "disease_category": "biolink:Disease",
    "support_pmids": "692", "procedure_ic": "15.1", "disease_ic": "3.8",
    "publications": "PMID:33333",
}


def test_diagnoses_edge():
    a = assoc(run([DIAGNOSES_SNOMED]))
    assert a.subject == "SNOMED:84200003"
    assert a.predicate == "biolink:diagnoses"
    assert a.original_predicate == "SEMMEDDB:DIAGNOSES"
    assert a.object == "MONDO:0005301"
    assert a.publications == ["PMID:11111", "PMID:22222"]
    assert a.evidence_count == 2
    assert a.primary_knowledge_source == "infores:semmeddb"
    assert a.aggregator_knowledge_source == ["infores:monarchinitiative"]


def test_treats_predicate_mapping():
    a = assoc(run([TREATS_NCIT]))
    assert a.predicate == "biolink:treats_or_applied_or_studied_to_treat"
    assert a.original_predicate == "SEMMEDDB:TREATS"


def test_emits_procedure_and_disease_nodes():
    ns = nodes(run([TREATS_NCIT]))
    by_id = {n.id: n for n in ns}
    assert "NCIT:C15357" in by_id and "MONDO:0004992" in by_id
    assert isinstance(by_id["NCIT:C15357"], Procedure)
    assert isinstance(by_id["MONDO:0004992"], Disease)
    assert by_id["NCIT:C15357"].name == "Esophagectomy"       # NCIT label kept
    assert by_id["MONDO:0004992"].provided_by == ["infores:semmeddb"]


def test_snomed_node_label_blanked():
    node = next(n for n in nodes(run([DIAGNOSES_SNOMED])) if n.id == "SNOMED:84200003")
    assert node.name is None                                   # SNOMED label not distributed


def test_nodes_deduped_across_rows():
    out = run([TREATS_NCIT, {**TREATS_NCIT, "disease_id": "MONDO:0007254",
                             "disease_label": "breast cancer"}])
    proc_nodes = [n for n in nodes(out) if n.id == "NCIT:C15357"]
    assert len(proc_nodes) == 1                                # procedure emitted once


def test_deterministic_edge_id():
    a1 = assoc(run([DIAGNOSES_SNOMED]))
    a2 = assoc(run([DIAGNOSES_SNOMED]))
    assert a1.id == a2.id and a1.id.startswith("uuid:")


def test_unknown_predicate_dropped():
    assert run([{**DIAGNOSES_SNOMED, "predicate": "located_in"}]) == []


def test_missing_procedure_id_dropped():
    assert run([{**DIAGNOSES_SNOMED, "procedure_id": ""}]) == []


def test_no_publications_is_none():
    a = assoc(run([{**TREATS_NCIT, "publications": ""}]))
    assert a.publications is None
    assert a.evidence_count is None
