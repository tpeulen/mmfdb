"""metadata.cif must conform to the dictionaries -- not merely use defined tags."""

from __future__ import annotations

import pytest

from mmfdb.schema.cif_validate import DdlDictionary, read_cif, validate_cif


@pytest.fixture(scope="module")
def dic() -> DdlDictionary:
    return DdlDictionary.bundled()


GOOD = """data_demo
loop_
_flr_instrument.id
_flr_instrument.details
1 'a confocal microscope'
loop_
_flr_sample_condition.id
_flr_sample_condition.details
1 'PBS pH 7.5'
loop_
_flr_exp_condition.id
_flr_exp_condition.details
1 '0 M urea'
"""


def test_the_reader_handles_quotes_text_fields_and_loops():
    block, cats = read_cif("""data_x
_entity.id 1
_entity.pdbx_description 'it''s fine'
loop_
_flr_instrument.id
_flr_instrument.details
1
;
two lines; with a semicolon
loop_ inside text
;
""")
    assert block == "x"
    assert cats["flr_instrument"]["rows"][0][1].startswith("two lines; with")
    assert "loop_ inside text" in cats["flr_instrument"]["rows"][0][1]


def test_a_conformant_document_has_no_problems(dic):
    assert validate_cif(GOOD, dic) == []


@pytest.mark.parametrize("bad, kind", [
    (GOOD + "_flr_instrument_x.y 1\n", "unknown-category"),
    (GOOD.replace("_flr_instrument.details", "_flr_instrument.colour"), "unknown-item"),
    (GOOD.replace("1 'a confocal microscope'", "one 'a confocal microscope'"), "type"),
    (GOOD.replace("1 'a confocal microscope'", "1 'a'\n1 'b'"), "duplicate-key"),
    ("""data_d
loop_
_flr_sample.id
_flr_sample.num_of_probes
_flr_sample.solvent_phase
_flr_sample.sample_condition_id
_flr_sample.entity_assembly_id
1 2 gaseous 1 1
""", "enumeration"),
    ("""data_d
loop_
_flr_sample.id
_flr_sample.num_of_probes
_flr_sample.solvent_phase
_flr_sample.sample_condition_id
1 2 liquid 1
""", "missing-mandatory"),
    ("""data_d
loop_
_flr_sample.id
_flr_sample.num_of_probes
_flr_sample.solvent_phase
_flr_sample.sample_condition_id
_flr_sample.entity_assembly_id
1 2 liquid 1 1
""", "missing-parent"),
    (GOOD.replace("_flr_exp_condition.details\n1", "_flr_exp_condition.details\n1") +
     """loop_
_flr_reference_measurement_group_link.group_id
_flr_reference_measurement_group_link.reference_measurement_id
7 7
loop_
_flr_reference_measurement_group.id
_flr_reference_measurement_group.num_measurements
1 1
loop_
_flr_reference_measurement.id
_flr_reference_measurement.reference_sample_probe_id
_flr_reference_measurement.num_species
7 1 1
""", "unresolved-reference"),
])
def test_each_kind_of_departure_is_found(dic, bad, kind):
    kinds = {p.kind for p in validate_cif(bad, dic)}
    assert kind in kinds, kinds


def test_the_dictionaries_are_read_whole(dic):
    """Looped item definitions, composite keys and multi-word enumerations."""
    assert dic.items["_chem_comp.type"].mandatory
    assert "L-peptide linking" in dic.items["_chem_comp.type"].enumerations
    assert dic.get_category("entity_poly_seq").keys == (
        "_entity_poly_seq.entity_id", "_entity_poly_seq.num", "_entity_poly_seq.mon_id")
    assert dic.items["_entity_poly.nstd_linkage"].enumerations == ("no", "n", "yes", "y")
    assert "Input data or restraints" in dic.items["_ihm_external_files.content_type"].enumerations
    for term in ("ht3", "spc_600_256", "pto"):
        assert term in dic.items["_mmfdb_artifact.data_format"].enumerations


def test_every_bundled_dictionary_tokenizes_strictly():
    """mmfdb's own dictionaries follow the CIF text-field rule, as the wwPDB ones do."""
    from mmfdb.schema.cif_validate import _tokens
    from mmfdb.schema.pdbx_metadata import MmcifDictionary

    for name in DdlDictionary.FILES:
        path = MmcifDictionary._resolve_dic(name)
        list(_tokens(path.read_text(encoding="utf-8")))
