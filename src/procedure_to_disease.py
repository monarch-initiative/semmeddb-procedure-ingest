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

The PMID support count goes out as `evidence_count` (biolink's slot for the
number of evidence instances behind an association) — it is just len(publications).

The IC scores (procedure_ic / disease_ic) stay columns in the preprocess sidecar
`data/procedure_to_disease.tsv` and are NOT emitted. The earlier claim that "biolink
forbids extra slots so there is nowhere to put them" was wrong on the second half:
`information_content` IS a real slot on `named thing`, and Procedure/Disease/
PhenotypicFeature all accept it.

The actual reason not to emit it: **Monarch already owns that slot.** monarch-kg
computes its own IC over the merged graph and ships it for 421,065 terms — and it is
a different quantity from ours. For MONDO:0004992 (cancer) Monarch says 7.53; our
per-ontology structural IC says 4.52. Writing ours into `information_content` would
either be overwritten at merge or, worse, silently win and corrupt a value the rest
of Monarch's tooling depends on. Ours is a build-time triage signal for deciding what
to ship, so it belongs in the sidecar, not on the node.
"""

import uuid
from typing import Any

import koza
from koza import KozaTransform
from biolink_model.datamodel.pydanticmodel_v2 import (
    Association,
    Procedure,
    Device,
    Activity,
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
# original_predicate's range is uriorcurie, and these are the CURIEs biolink itself
# carries as exact_mappings on the two predicates above.
ORIGINAL_PREDICATE = {
    "diagnoses": "SEMMEDDB:DIAGNOSES",
    "treats": "SEMMEDDB:TREATS",
}
# The preprocess admits subjects NodeNorm typed as Procedure, Device OR Activity.
# Forcing all three into Procedure asserted things like "Contact Lenses is a
# Procedure" and hid ~4.3k device/activity subjects from any device-aware query.
_SUBJECT_CLASS = {
    "biolink:Procedure": Procedure,
    "biolink:Device": Device,
    "biolink:Activity": Activity,
}
_OBJECT_CLASS = {
    "biolink:Disease": Disease,
    "biolink:PhenotypicFeature": PhenotypicFeature,
    "biolink:DiseaseOrPhenotypicFeature": DiseaseOrPhenotypicFeature,
}
_NS = uuid.UUID("00000000-0000-0000-0000-00000000c0de")  # stable namespace for edge ids
# Separate sets per side: a shared set would emit an id that appears as both a
# procedure and a disease with whichever class it was seen as first. The overlap is
# 0 in this build, but NCIT ids legitimately occur on both sides, so don't rely on it.
_seen_procedures: set[str] = set()
_seen_diseases: set[str] = set()


@koza.transform_record()
def transform_record(koza_transform: KozaTransform, row: dict[str, Any]) -> list:
    predicate = PREDICATE_MAP.get(row["predicate"])
    if predicate is None or not row.get("procedure_id") or not row.get("disease_id"):
        return []

    out: list = []
    # procedure node (label BLANK for SNOMED/UMLS per licensing; NCIT label kept)
    pid = row["procedure_id"]
    if pid not in _seen_procedures:
        _seen_procedures.add(pid)
        node_cls = _SUBJECT_CLASS.get(row.get("procedure_category"), Procedure)
        out.append(node_cls(id=pid, name=row.get("procedure_label") or None,
                            provided_by=["infores:semmeddb"]))
    # disease/phenotype node
    did = row["disease_id"]
    if did not in _seen_diseases:
        _seen_diseases.add(did)
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
            original_predicate=ORIGINAL_PREDICATE[row["predicate"]],
            object=row["disease_id"],
            publications=pubs or None,
            evidence_count=len(pubs) or None,
            primary_knowledge_source="infores:semmeddb",
            aggregator_knowledge_source=["infores:monarchinitiative"],
            knowledge_level=KnowledgeLevelEnum.knowledge_assertion,
            agent_type=AgentTypeEnum.text_mining_agent,
        )
    )
    return out
