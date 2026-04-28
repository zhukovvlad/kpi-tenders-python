"""Unit tests for app/nlp/anonymizer.py.

Fast tests (ConsistencyMapper, span dedup, static filters, regex/stdnum)
do NOT load the Natasha NER models — the pipeline is created via
NatashaPipeline.__new__ which bypasses __init__ and the slow model load.

Tests that exercise the full NER pipeline are marked @pytest.mark.slow.
They use a session-scoped fixture so models are loaded only once.
"""

import pytest

from app.nlp.anonymizer import ConsistencyMapper, NatashaPipeline, Span

# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def real_pipeline():
    """Full NatashaPipeline with real Natasha models — loaded once per session."""
    return NatashaPipeline()


@pytest.fixture
def pipeline():
    """NatashaPipeline without model loading — for regex/stdnum tests."""
    return NatashaPipeline.__new__(NatashaPipeline)


# ═══════════════════════════════════════════════════════════════════════════
# ConsistencyMapper
# ═══════════════════════════════════════════════════════════════════════════


class TestConsistencyMapper:
    def test_same_value_returns_same_token(self):
        m = ConsistencyMapper()
        assert m.get("INN", "7707083893") == m.get("INN", "7707083893")

    def test_different_values_different_tokens(self):
        m = ConsistencyMapper()
        assert m.get("INN", "7707083893") != m.get("INN", "7736050003")

    def test_counter_increments_per_type(self):
        m = ConsistencyMapper()
        assert m.get("PERSON", "Иванов И.И.") == "<PERSON_1>"
        assert m.get("PERSON", "Петров П.П.") == "<PERSON_2>"
        assert m.get("INN", "7707083893") == "<INN_1>"

    def test_different_types_have_independent_counters(self):
        m = ConsistencyMapper()
        m.get("INN", "7707083893")
        m.get("INN", "7736050003")
        assert m.get("OGRN", "1027700132195") == "<OGRN_1>"

    def test_inverted_maps_token_to_original(self):
        m = ConsistencyMapper()
        m.get("PERSON", "Иванов И.И.")
        m.get("INN", "7707083893")
        inv = m.inverted()
        assert inv["<PERSON_1>"] == "Иванов И.И."
        assert inv["<INN_1>"] == "7707083893"

    def test_strips_whitespace_in_key(self):
        m = ConsistencyMapper()
        t1 = m.get("PERSON", " Иванов И.И. ")
        t2 = m.get("PERSON", "Иванов И.И.")
        assert t1 == t2


# ═══════════════════════════════════════════════════════════════════════════
# Span deduplication
# ═══════════════════════════════════════════════════════════════════════════


class TestDedupSpans:
    def test_non_overlapping_spans_preserved(self):
        spans = [Span(0, 5, "INN", "12345"), Span(10, 15, "BIK", "67890")]
        result = NatashaPipeline._dedup_spans(spans)
        assert len(result) == 2

    def test_overlapping_longer_span_wins(self):
        spans = [Span(0, 10, "PERSON", "Иванов И.И."), Span(0, 5, "PERSON", "Иванов")]
        result = NatashaPipeline._dedup_spans(spans)
        assert len(result) == 1
        assert result[0].end == 10

    def test_overlapping_higher_priority_wins_same_length(self):
        # INN (priority 0) beats PERSON (priority 20) at equal span
        spans = [Span(5, 15, "PERSON", "1234567890"), Span(5, 15, "INN", "1234567890")]
        result = NatashaPipeline._dedup_spans(spans)
        assert len(result) == 1
        assert result[0].entity_type == "INN"

    def test_adjacent_spans_both_kept(self):
        spans = [Span(0, 5, "INN", "12345"), Span(5, 10, "BIK", "67890")]
        result = NatashaPipeline._dedup_spans(spans)
        assert len(result) == 2

    def test_result_sorted_by_position(self):
        spans = [Span(10, 15, "BIK", "67890"), Span(0, 5, "INN", "12345")]
        result = NatashaPipeline._dedup_spans(spans)
        assert result[0].start == 0
        assert result[1].start == 10


# ═══════════════════════════════════════════════════════════════════════════
# Static filters
# ═══════════════════════════════════════════════════════════════════════════


class TestAcceptPerson:
    def test_accepts_fio_with_initials(self):
        assert NatashaPipeline._accept_person("Иванов И.И.")

    def test_accepts_full_fio(self):
        assert NatashaPipeline._accept_person("Иванов Иван Иванович")

    def test_rejects_deny_list_role(self):
        assert not NatashaPipeline._accept_person("Заказчик")

    def test_rejects_all_lowercase(self):
        assert not NatashaPipeline._accept_person("иванов иван")

    def test_rejects_deny_list_compound(self):
        assert not NatashaPipeline._accept_person("Инвестором-Застройщиком")


class TestAcceptOrg:
    def test_accepts_ooo(self):
        assert NatashaPipeline._accept_org("ООО «Ромашка»")

    def test_accepts_pao(self):
        assert NatashaPipeline._accept_org("ПАО Сбербанк")

    def test_rejects_gost_abbreviation(self):
        assert not NatashaPipeline._accept_org("ГОСТ Строймонтаж")

    def test_rejects_smr_abbreviation(self):
        assert not NatashaPipeline._accept_org("СМР Ресурс")


class TestHasContext:
    def test_finds_word_before(self):
        ctx = frozenset(["инн"])
        text = "инн 7707083893"
        assert NatashaPipeline._has_context(text.lower(), 4, 14, ctx)

    def test_finds_word_after(self):
        ctx = frozenset(["счет"])
        text = "40702810400000012345 счет"
        assert NatashaPipeline._has_context(text.lower(), 0, 20, ctx)

    def test_no_context_outside_window(self):
        ctx = frozenset(["инн"])
        text = "инн" + " " * 300 + "7707083893"
        assert not NatashaPipeline._has_context(text.lower(), len(text) - 10, len(text), ctx)


# ═══════════════════════════════════════════════════════════════════════════
# Regex entities (no Natasha models needed)
# ═══════════════════════════════════════════════════════════════════════════


class TestRegexEntities:
    def test_detects_bik(self, pipeline):
        types = {s.entity_type for s in pipeline._regex_entities("БИК 044525225")}
        assert "BIK" in types

    def test_detects_email(self, pipeline):
        types = {s.entity_type for s in pipeline._regex_entities("Почта: info@company.ru")}
        assert "EMAIL_ADDRESS" in types

    def test_detects_phone_plus7(self, pipeline):
        types = {s.entity_type for s in pipeline._regex_entities("+7 (495) 123-45-67")}
        assert "PHONE_NUMBER" in types

    def test_detects_organization_ooo(self, pipeline):
        types = {s.entity_type for s in pipeline._regex_entities("ООО «Ромашка»")}
        assert "ORGANIZATION" in types

    def test_detects_organization_pao(self, pipeline):
        types = {s.entity_type for s in pipeline._regex_entities("ПАО Сбербанк")}
        assert "ORGANIZATION" in types

    def test_detects_cadastral(self, pipeline):
        types = {
            s.entity_type for s in pipeline._regex_entities("кадастровый номер 77:06:0004006:100")
        }
        assert "CADASTRAL" in types

    def test_detects_fio_lastname_initials(self, pipeline):
        types = {s.entity_type for s in pipeline._regex_entities("Иванов И.И.")}
        assert "PERSON" in types

    def test_detects_initials_lastname(self, pipeline):
        types = {s.entity_type for s in pipeline._regex_entities("И.И. Иванов")}
        assert "PERSON" in types

    def test_no_org_for_gost(self, pipeline):
        spans = pipeline._regex_entities("согласно ГОСТ Строймонтаж")
        org_texts = [s.text for s in spans if s.entity_type == "ORGANIZATION"]
        assert not any("ГОСТ" in t for t in org_texts)

    def test_detects_bank_account_with_context(self, pipeline):
        types = {s.entity_type for s in pipeline._regex_entities("р/с 40702810400000012345")}
        assert "BANK_ACCOUNT" in types

    def test_no_bank_account_without_context(self, pipeline):
        types = {s.entity_type for s in pipeline._regex_entities("40702810400000012345")}
        assert "BANK_ACCOUNT" not in types

    def test_detects_url(self, pipeline):
        types = {s.entity_type for s in pipeline._regex_entities("сайт: https://example.com/path")}
        assert "URL" in types

    def test_detects_location_city(self, pipeline):
        types = {s.entity_type for s in pipeline._regex_entities("г. Москва,")}
        assert "LOCATION" in types

    def test_detects_kpp_with_context(self, pipeline):
        types = {s.entity_type for s in pipeline._regex_entities("КПП 772301001")}
        assert "KPP" in types


# ═══════════════════════════════════════════════════════════════════════════
# stdnum-validated entities (no Natasha models needed)
# ═══════════════════════════════════════════════════════════════════════════


class TestStdnumEntities:
    def test_detects_inn_10_digit_with_context(self, pipeline):
        # 7707083893 = ПАО Сбербанк, valid INN
        types = {s.entity_type for s in pipeline._stdnum_entities("ИНН 7707083893")}
        assert "INN" in types

    def test_detects_inn_12_digit(self, pipeline):
        # 500100732259 — valid 12-digit INN (verified via stdnum)
        types = {s.entity_type for s in pipeline._stdnum_entities("ИНН 500100732259")}
        assert "INN" in types

    def test_ignores_inn_10_digit_without_context(self, pipeline):
        # No context word — should not be flagged
        types = {s.entity_type for s in pipeline._stdnum_entities("7707083893")}
        assert "INN" not in types

    def test_ignores_invalid_inn(self, pipeline):
        types = {s.entity_type for s in pipeline._stdnum_entities("ИНН 1234567890")}
        assert "INN" not in types

    def test_detects_ogrn(self, pipeline):
        # 1027700132195 — valid OGRN (verified via stdnum)
        types = {s.entity_type for s in pipeline._stdnum_entities("ОГРН 1027700132195")}
        assert "OGRN" in types

    def test_detects_snils_formatted(self, pipeline):
        # 112-233-445 95 → 11 digits, valid SNILS
        types = {s.entity_type for s in pipeline._stdnum_entities("СНИЛС 112-233-445 95")}
        assert "SNILS" in types

    def test_ignores_ogrn_wrong_checksum(self, pipeline):
        # Starts with 1, 13 digits but invalid checksum
        types = {s.entity_type for s in pipeline._stdnum_entities("1027700132196")}
        assert "OGRN" not in types


# ═══════════════════════════════════════════════════════════════════════════
# Full pipeline tests — slow (loads Natasha models)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.slow
class TestFullPipeline:
    def test_empty_text_unchanged(self, real_pipeline):
        result = real_pipeline.sanitize("")
        assert result.sanitized_text == ""
        assert result.entities_map == {}

    def test_no_pii_text_unchanged(self, real_pipeline):
        text = "Стоимость работ составляет 100 000 рублей."
        result = real_pipeline.sanitize(text)
        assert result.sanitized_text == text
        assert result.entities_map == {}

    def test_inn_replaced(self, real_pipeline):
        result = real_pipeline.sanitize("ИНН 7707083893")
        assert "7707083893" not in result.sanitized_text
        assert any(t.startswith("<INN_") for t in result.entities_map)

    def test_email_replaced(self, real_pipeline):
        result = real_pipeline.sanitize("Контакт: info@company.ru")
        assert "info@company.ru" not in result.sanitized_text

    def test_same_value_same_token(self, real_pipeline):
        text = "ИНН 7707083893 и снова ИНН 7707083893"
        result = real_pipeline.sanitize(text)
        inn_tokens = [t for t in result.entities_map if t.startswith("<INN_")]
        assert len(inn_tokens) == 1

    def test_contract_snippet_detects_multiple_entities(self, real_pipeline):
        text = (
            "ООО «Ромашка», ИНН 7736050003, ОГРН 1027700132195, "
            "в лице директора Иванова И.И., р/с 40702810400000012345, "
            "БИК 044525225, e-mail: info@romashka.ru"
        )
        result = real_pipeline.sanitize(text)
        assert len(result.entities_map) >= 4
