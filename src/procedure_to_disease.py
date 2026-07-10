"""SemMedDB procedure -> disease/phenotype transform.

Reads the preprocessing output (one row per clean, grounded, IC-scored edge)
and emits Biolink associations. All heavy lifting (NodeNorm categorisation,
MRCONSO/NCIT/SNOMED grounding, structural-IC scoring, predicted=True + typed +
broad-object filtering) happens in scripts/preprocess.py — this transform is
deliberately thin.

Licensing: when a procedure is grounded to SNOMED, the SCTID **code** is the
subject but the label is BLANK — SNOMED description text is not openly
redistributable (see ARCHITECTURE.md). Open-vocabulary labels (NCIT/MONDO/HP)
live in the node file, not here.

IC scores (procedure_ic / disease_ic) and support counts remain columns in the
preprocess sidecar `data/procedure_to_disease.tsv` for downstream thresholding;
they are not emitted as edge properties (biolink pydantic forbids extra slots —
promoting them to a qualifier/slot is a follow-up).
"""

import uuid
from typing import Any

import koza
from koza import KozaTransform
from biolink_model.datamodel.pydanticmodel_v2 import (
    Association,
    Procedure,
    Disease,
    PhenotypicFeature,
    DiseaseOrPhenotypicFeature,
    KnowledgeLevelEnum,
    AgentTypeEnum,
)

PREDICATE_MAP = {
    "diagnoses": "biolink:diagnoses",
    "treats": "biolink:treats_or_applied_or_studied_to_treat",
}
_OBJECT_CLASS = {
    "biolink:Disease": Disease,
    "biolink:PhenotypicFeature": PhenotypicFeature,
    "biolink:DiseaseOrPhenotypicFeature": DiseaseOrPhenotypicFeature,
}
_NS = uuid.UUID("00000000-0000-0000-0000-00000000c0de")  # stable namespace for edge ids
_seen_nodes: set[str] = set()  # emit each node once (serial transform; downstream re-dedups)


@koza.transform_record()
def transform_record(koza_transform: KozaTransform, row: dict[str, Any]) -> list:
    predicate = PREDICATE_MAP.get(row["predicate"])
    if predicate is None or not row.get("procedure_id") or not row.get("disease_id"):
        return []

    out: list = []
    # procedure node (label BLANK for SNOMED/UMLS per licensing; NCIT label kept)
    pid = row["procedure_id"]
    if pid not in _seen_nodes:
        _seen_nodes.add(pid)
        out.append(Procedure(id=pid, name=row.get("procedure_label") or None,
                             provided_by=["infores:semmeddb"]))
    # disease/phenotype node
    did = row["disease_id"]
    if did not in _seen_nodes:
        _seen_nodes.add(did)
        node_cls = _OBJECT_CLASS.get(row.get("disease_category"), Disease)
        out.append(node_cls(id=did, name=row.get("disease_label") or None,
                            provided_by=["infores:semmeddb"]))

    pubs = [p for p in (row.get("publications") or "").split("|") if p]
    key = f"{row['procedure_id']}|{row['predicate']}|{row['disease_id']}"

    out.append(
        Association(
            id=f"uuid:{uuid.uuid5(_NS, key)}",
            subject=row["procedure_id"],
            predicate=predicate,
            original_predicate=row["predicate"],
            object=row["disease_id"],
            publications=pubs or None,
            primary_knowledge_source="infores:semmeddb",
            aggregator_knowledge_source=["infores:monarchinitiative"],
            knowledge_level=KnowledgeLevelEnum.knowledge_assertion,
            agent_type=AgentTypeEnum.text_mining_agent,
        )
    )
    return out
