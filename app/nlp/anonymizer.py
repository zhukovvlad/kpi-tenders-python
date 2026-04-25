"""Natasha-based PII anonymizer for Russian tender documents.

Pipeline:
  1. Natasha NER — PERSON detection only (ORG/LOC via regex, lower false-positive rate)
  2. stdnum     — checksum validation for INN, OGRN, SNILS
  3. Regex      — all remaining entity types

Public API:
  NatashaPipeline  — initialise once; call sanitize(text) per document.
  anonymize(text)  — module-level shortcut using a lazy singleton.
"""

from __future__ import annotations

import re
import threading
from collections import defaultdict
from dataclasses import dataclass, field

from natasha import Doc, NewsEmbedding, NewsNERTagger, Segmenter
from stdnum.ru import inn as stdnum_inn
from stdnum.ru import ogrn as stdnum_ogrn

try:
    from stdnum.ru import snils as _stdnum_snils
    _snils_is_valid = _stdnum_snils.is_valid
except (ImportError, AttributeError):
    def _snils_is_valid(number: str) -> bool:
        """Validate SNILS using Mod-101 checksum (ГОСТ Р 54471-2011)."""
        digits = re.sub(r"\D", "", number)
        if len(digits) != 11:
            return False
        body = [int(d) for d in digits[:9]]
        checksum = int(digits[9:])
        total = sum(body[i] * (9 - i) for i in range(9))
        control = total % 101
        if control >= 100:
            control = 0
        return control == checksum


# ══════════════════════════════════════════════════════════════════════════════
# Deny / allow lists
# ══════════════════════════════════════════════════════════════════════════════

_PERSON_DENY_SET: frozenset[str] = frozenset({
    "заказчик", "заказчика", "заказчику", "заказчиком", "заказчике",
    "исполнитель", "исполнителя", "исполнителю", "исполнителем", "исполнителе",
    "подрядчик", "подрядчика", "подрядчику", "подрядчиком", "подрядчике",
    "подрядчики", "подрядчиков", "подрядчикам", "подрядчиками",
    "поставщик", "поставщика", "поставщику", "поставщиком", "поставщике",
    "покупатель", "покупателя", "покупателю", "покупателем", "покупателе",
    "продавец", "продавца", "продавцу", "продавцом", "продавце",
    "арендатор", "арендатора", "арендатору", "арендатором", "арендаторе",
    "арендодатель", "арендодателя", "арендодателю", "арендодателем",
    "залогодатель", "залогодержатель",
    "страхователь", "страховщик",
    "принципал", "агент", "комиссионер", "комитент",
    "лицензиар", "лицензиат",
    "цедент", "цессионарий",
    "поручитель", "поручителя",
    "сторона", "стороны", "сторон", "сторонам", "сторонами",
    "стороне", "стороной", "сторону",
    "истец", "ответчик", "третье",
    "работодатель", "работодателя", "работодателю",
    "работник", "работника", "работнику",
    "кредитор", "кредитора", "кредитору",
    "должник", "должника", "должнику",
    "получатель", "отправитель", "грузополучатель", "грузоотправитель",
    "доверитель", "поверенный",
    "наследник", "наследодатель",
    "генподрядчик", "генподрядчика", "генподрядчику", "генподрядчиком",
    "генподрядчике", "генподрядчики", "генподрядчиков",
    "инвестор", "инвестора", "инвестору", "инвестором", "инвесторе",
    "застройщик", "застройщика", "застройщику", "застройщиком", "застройщике",
    "проектировщик", "проектировщика", "проектировщику", "проектировщиком",
    "управляющий", "управляющего", "управляющему", "управляющим", "управляющей",
    "управляющая", "управляющую",
    "договор", "договора", "договору", "договором", "договоре",
    "соглашение", "контракт",
    "протокол", "акт", "приложение", "дополнение", "спецификация",
    "счёт", "счет", "накладная", "товарная", "транспортная",
    "претензия", "уведомление", "согласие", "заявка", "заявление",
    "инструкция", "регламент", "положение",
    "отчет", "отчета", "отчету", "отчетом", "отчете",
    "отчёт", "отчёта", "отчёту", "отчётом", "отчёте",
    "отчеты", "отчетов", "отчетам", "отчетах", "отчетами",
    "работа", "работы", "работ", "работам", "работами", "работе",
    "услуга", "услуги", "услуг", "услугам", "услугами", "услуге",
    "предписание", "предписания", "предписаний", "предписанию",
    "устав", "устава", "уставу", "уставом", "уставе",
    "доверенность", "доверенности", "доверенностью",
    "приказ", "приказа", "приказу", "приказом", "приказе",
    "решение", "решения", "решению", "решением",
    "свидетельство", "свидетельства",
    "лицензия", "лицензии",
    "банк", "фонд", "суд", "орган", "служба",
    "министерство", "департамент", "управление", "комитет",
    "федерация", "инспекция", "комиссия",
    "россия", "российской", "москва",
    "объект", "объекта", "объекту", "объектом", "объекте",
    "почтой", "почте", "почта", "почту",
    "целью", "целей", "цели",
    "директор", "директора", "директору", "директором",
    "генеральный", "генерального", "генеральному",
    "президент", "президента",
    "председатель", "председателя",
    "руководитель", "руководителя",
    "представитель", "представителя",
    "учредитель", "учредителя",
    "собственник", "собственника",
    "технический", "технического",
    "заместитель", "заместителя",
    "строительный", "строительным", "строительного", "строительном",
    "строительному", "строительной", "строительное",
    "геодезическое", "геодезического", "геодезическая", "геодезический",
    "титульное", "титульного", "титульная", "титульный",
    "гарантийном", "гарантийного", "гарантийный", "гарантийная",
    "электронном", "электронного", "электронный", "электронная", "электронной",
    "проверка", "проверки", "проверку", "проверкой",
    "выданная", "выданный", "выданного", "выданной",
    "подписывая",
    "согласование", "согласования", "согласованию",
    "специализация", "специализации",
    "недостатках", "недостатков", "недостатки", "недостаток",
    "техзор", "техзора", "техзору",
    "архив", "архива", "архиву",
})

_PERSON_MIN_LEN_SINGLE_WORD = 4

_ORG_ABBREV_DENY_SET: frozenset[str] = frozenset({
    "ТЗ", "РД", "ИД", "ПД", "ИРД", "АПЗ", "ГПЗ", "ПСД", "СПД",
    "СОД", "БИМ", "ГОСТ", "СНИП", "СП", "НТД", "РДН", "ВСН",
    "УК", "ГИП", "РИП", "ОТК", "ОКС", "ОКСО", "ГП",
    "СМР", "ПНР", "КИ", "КТП",
    "НКО", "МГСН", "ЗОС", "ГПП", "ИСР",
    "НДС", "НДФЛ", "НК", "ФЗ", "ФОТ",
})


# ══════════════════════════════════════════════════════════════════════════════
# Regex patterns
# ══════════════════════════════════════════════════════════════════════════════

_CAP = r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?"
_INIT = r"[А-ЯЁ]\.[\s]?[А-ЯЁ]\."

# Each entry: (entity_type, compiled_regex, needs_context, context_words)
_REGEX_PATTERNS: list[tuple[str, re.Pattern[str], bool, frozenset[str]]] = []


def _r(
    entity_type: str,
    pattern: str,
    needs_context: bool = False,
    context: list[str] | None = None,
    flags: int = 0,
) -> None:
    ctx = frozenset(w.lower() for w in context) if context else frozenset()
    _REGEX_PATTERNS.append((entity_type, re.compile(pattern, flags), needs_context, ctx))


_FIO_CONTEXT_WORDS = [
    "в лице", "лице", "действующего", "действующей",
    "именуемого", "именуемой", "гражданина", "гражданки",
    "фамилия", "фио", "ф.и.о.", "имени", "паспорт", "снилс",
    "директор", "руководитель", "представитель",
    "президент", "заместитель", "начальник",
    "нотариус", "адвокат", "бухгалтер",
    "директора", "руководителя", "представителя",
    "генерального директора", "генеральный директор",
    "индивидуальный предприниматель",
    "уполномоченный", "подписант", "подписал", "подписала",
    "от имени", "контактное лицо",
]

# Фамилия И.О.
_r("PERSON", _CAP + r"\s+" + _INIT, flags=re.DOTALL | re.MULTILINE)
# И.О. Фамилия
_r("PERSON", _INIT + r"\s+" + _CAP, flags=re.DOTALL | re.MULTILINE)
# Фамилия Имя Отчество — context required
_r(
    "PERSON",
    _CAP + r"\s+" + _CAP + r"\s+" + _CAP,
    needs_context=True,
    context=_FIO_CONTEXT_WORDS,
    flags=re.DOTALL | re.MULTILINE,
)
# Фамилия Имя (2-word) — context required
_r(
    "PERSON",
    _CAP + r"\s+" + _CAP + r"(?=\s|,|\.|;|—|\)|$)",
    needs_context=True,
    context=_FIO_CONTEXT_WORDS,
    flags=re.DOTALL | re.MULTILINE,
)

_ORG_PREFIX = (
    r"(?<![А-ЯЁа-яёA-Za-z0-9])"
    r"(?:"
    r"ООО|ОАО|ПАО|ЗАО|АО|НКО|КФХ|КП|СНТ|ТСЖ|ТСН"
    r"|ПБОЮЛ|ИП"
    r"|ФГУП|ГУП|МУП|ФГБУ|ФГКУ|ГБОУ|МБОУ|ГБУ|МБУ"
    r"|(?:Общество\s+с\s+ограниченной\s+ответственностью)"
    r"|(?:Публичное\s+акционерное\s+общество)"
    r"|(?:Акционерное\s+общество)"
    r"|(?:Закрытое\s+акционерное\s+общество)"
    r"|(?:Индивидуальный\s+предприниматель)"
    r")"
)
_ORG_NAME = (
    r"(?:"
    r"[«\"][А-ЯЁа-яёA-Za-z0-9][^»\"]{1,120}[»\"]"
    r"|[А-ЯЁ][а-яёА-ЯЁ0-9\-]{1,40}"
    r")"
)
_r("ORGANIZATION", _ORG_PREFIX + r"\s+" + _ORG_NAME)
_r(
    "ORGANIZATION",
    (
        r"(?<![А-ЯЁа-яёA-Za-z])"
        r"[А-ЯЁ]{2,5}[\s\-][А-ЯЁ][а-яё]{2,20}"
        r"(?![а-яёА-ЯЁ])"
    ),
    needs_context=True,
    context=[
        "компания", "организация", "фирма", "предприятие", "общество",
        "холдинг", "группа", "партнер", "контрагент",
        "именуемое", "именуемый", "именуемая",
        "ооо", "оао", "пао", "зао", "ао", "гуп", "муп",
        "наименование", "на основании", "в лице",
    ],
    flags=re.DOTALL | re.MULTILINE,
)

_GEO_WORD = r"[А-ЯЁа-яё][а-яёА-ЯЁ\-]{1,40}(?:\s+[А-ЯЁ][а-яёА-ЯЁ\-]{1,40})?"
_GEO_WORD_UPPER = r"[А-ЯЁ][а-яёА-ЯЁ\-]{2,40}(?:\s+[А-ЯЁ][а-яёА-ЯЁ\-]{1,40}){0,2}"

_r("LOCATION", r"(?<![А-ЯЁа-яёA-Za-z])(?:г\.|город)[ ]?[А-ЯЁ][а-яёА-ЯЁ\-]{2,40}(?:[ ]+[А-ЯЁ][а-яёА-ЯЁ\-]{1,40}){0,2}(?=\s*[,;\n\r]|\s*$|\s+[а-яё])")
_r("LOCATION", r"\b[1-9]\d{5},\s+[А-ЯЁ][а-яё]{2,20}")
_r("LOCATION", r"вн\.тер\.\s+(?:г\.[ ]?)?" + _GEO_WORD_UPPER)
_r(
    "LOCATION",
    (
        r"(?<![А-ЯЁа-яёA-Za-z])(?<!и )(?:ул\.?|улица|пр\.?|проспект|пер\.?|переулок|пл\.|бул\.|ш\.)\s+"
        r"(?:[А-ЯЁ0-9][А-ЯЁа-яё0-9\-]*\.?\s+)*"
        r"[А-ЯЁ][А-ЯЁа-яё\-]{1,40}"
        r"(?:,?\s+(?:д\.|дом)\s*\d+[А-Яа-я]?(?:,\s*кв\.\s*\d+)?)?"
    ),
)
_r("LOCATION", _GEO_WORD + r"\s+(?:проезд|бульвар|набережная|тупик|аллея|шоссе)")
_r("LOCATION", _GEO_WORD + r"[ ]+(?:ул\.?|пр\.?|пер\.?|пл\.|бул\.|ш\.)(?=[,\s]|$)")
_r("LOCATION", _GEO_WORD + r"\s+(?:область|обл\.|край|республика|округ|р-н|район)")

_INN_CONTEXT = frozenset(
    w.lower() for w in ["инн", "ИНН", "налогоплательщик", "налоговый номер", "идентификационный номер"]
)
_RE_INN_12 = re.compile(r"(?<!\d)\d{12}(?!\d)")
_RE_INN_10 = re.compile(r"(?<!\d)\d{10}(?!\d)")

_OGRN_CONTEXT = frozenset(
    w.lower() for w in [
        "огрн", "ОГРН", "огрнип", "ОГРНИП",
        "основной государственный регистрационный",
        "регистрационный номер",
    ]
)
_RE_OGRN_13 = re.compile(r"(?<!\d)[15]\d{12}(?!\d)")
_RE_OGRN_15 = re.compile(r"(?<!\d)3\d{14}(?!\d)")

_SNILS_CONTEXT = frozenset(
    w.lower() for w in ["снилс", "СНИЛС", "страховой номер", "пенсионное страхование", "пфр"]
)
_RE_SNILS_FMT = re.compile(r"\b\d{3}[-–]\d{3}[-–]\d{3}\s{0,2}\d{2}\b")
_RE_SNILS_PLAIN = re.compile(r"(?<!\d)\d{11}(?!\d)")

_r(
    "RU_PASSPORT",
    r"\b\d{2}\s\d{2}\s{1,2}\d{6}\b",
    needs_context=True,
    context=["паспорт", "серия", "серия и номер", "документ, удостоверяющий", "выдан", "гражданина"],
)
_r(
    "RU_PASSPORT",
    r"(?<!\d)\d{4}\s*\d{6}(?!\d)",
    needs_context=True,
    context=["паспорт", "серия", "серия и номер", "документ, удостоверяющий", "выдан", "гражданина"],
)

_BANK_CONTEXT = ["счет", "счёт", "р/с", "к/с", "л/с", "расчетный", "расчётный", "корреспондентский", "лицевой", "реквизиты"]
_r("BIK", r"\b04\d{7}\b")
_r(
    "BANK_ACCOUNT",
    r"(?<!\d)\d{20}(?!\d)",
    needs_context=True,
    context=_BANK_CONTEXT,
)
_r(
    "BANK_ACCOUNT",
    r"\b\d{5} \d{3} \d{4} \d{4} \d{4}\b",
    needs_context=True,
    context=_BANK_CONTEXT,
)
_r("KPP", r"(?<!\d)\d{9}(?!\d)", needs_context=True, context=["кпп", "причина постановки"])
_r("CADASTRAL", r"\b\d{2}:\d{2}:\d{6,7}:\d{1,6}\b")
_r("PHONE_NUMBER", r"(?:\+7|8)[\s\-]?(?:\(\d{3}\)|\d{3})[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}")
_r("PHONE_NUMBER", r"\+\d{1,3}[\s\-]?\(?\d{1,4}\)?[\s\-]?\d{1,4}[\s\-]?\d{1,9}")
_r("EMAIL_ADDRESS", r"[a-zA-Z0-9_.%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_r("URL", r"https?://[^\s,;\"'<>\]\)]+(?<![.])")
_r("URL", r"\bwww\.[a-zA-Z0-9\-]+\.[a-zA-Z]{2,}[^\s,;\"'<>\]\)]*(?<![.])")
_r(
    "IP_ADDRESS",
    r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)(?!\.)\b",
    needs_context=True,
    context=["ip", "адрес", "сервер", "хост", "host", "address", "subnet", "подсеть", "сеть", "network"],
)
_r(
    "CREDIT_CARD",
    r"\b(?:\d[ -]*?){13,19}\b",
    needs_context=True,
    context=["карта", "card", "visa", "mastercard", "мир"],
)


# ══════════════════════════════════════════════════════════════════════════════
# Consistency Mapper
# ══════════════════════════════════════════════════════════════════════════════


class ConsistencyMapper:
    """Maps (entity_type, original_text) → ``<TYPE_N>`` token.

    Same value always receives the same token within a document.
    The angle-bracket format is optimised for downstream LLM parsing.
    """

    def __init__(self) -> None:
        self._map: dict[tuple[str, str], str] = {}
        self._counters: dict[str, int] = defaultdict(int)

    def get(self, entity_type: str, original: str) -> str:
        key = (entity_type, original.strip())
        if key not in self._map:
            self._counters[entity_type] += 1
            self._map[key] = f"<{entity_type}_{self._counters[entity_type]}>"
        return self._map[key]

    def inverted(self) -> dict[str, str]:
        """Return ``{token: original}`` mapping."""
        return {token: key[1] for key, token in self._map.items()}


# ══════════════════════════════════════════════════════════════════════════════
# Span container
# ══════════════════════════════════════════════════════════════════════════════


@dataclass(slots=True, order=True)
class Span:
    start: int
    end: int
    entity_type: str = field(compare=False)
    text: str = field(compare=False)


# ══════════════════════════════════════════════════════════════════════════════
# Pipeline
# ══════════════════════════════════════════════════════════════════════════════

_CONTEXT_WINDOW = 150


class NatashaPipeline:
    """Initialise once at worker startup; call :meth:`sanitize` per document."""

    def __init__(self) -> None:
        self.segmenter = Segmenter()
        self.emb = NewsEmbedding()
        self.ner_tagger = NewsNERTagger(self.emb)

    @dataclass
    class Result:
        sanitized_text: str
        entities_map: dict[str, str]

    def sanitize(self, text: str) -> Result:
        if not text or not text.strip():
            return self.Result(sanitized_text=text, entities_map={})

        mapper = ConsistencyMapper()
        spans = self._dedup_spans(self._detect_all(text))

        parts: list[str] = []
        prev = 0
        for sp in spans:
            parts.append(text[prev:sp.start])
            parts.append(mapper.get(sp.entity_type, sp.text))
            prev = sp.end
        parts.append(text[prev:])

        return self.Result(sanitized_text="".join(parts), entities_map=mapper.inverted())

    def _detect_all(self, text: str) -> list[Span]:
        spans: list[Span] = []
        spans.extend(self._natasha_ner(text))
        spans.extend(self._stdnum_entities(text))
        spans.extend(self._regex_entities(text))
        return spans

    def _natasha_ner(self, text: str) -> list[Span]:
        """PERSON detection only — ORG/LOC handled by regex (fewer false positives)."""
        doc = Doc(text)
        doc.segment(self.segmenter)
        doc.tag_ner(self.ner_tagger)

        spans: list[Span] = []
        for sp in doc.spans:
            if sp.type != "PER":
                continue
            raw = text[sp.start:sp.stop]
            if "\n" in raw or "\t" in raw:
                continue
            stripped = raw.strip()
            if " " not in stripped and "." not in stripped:
                continue
            if not self._accept_person(raw):
                continue
            spans.append(Span(sp.start, sp.stop, "PERSON", raw))
        return spans

    def _stdnum_entities(self, text: str) -> list[Span]:
        spans: list[Span] = []
        text_lower = text.lower()

        for rx in (_RE_INN_12, _RE_INN_10):
            for m in rx.finditer(text):
                digits = m.group()
                if not stdnum_inn.is_valid(digits):
                    continue
                if len(digits) == 10 and not self._has_context(text_lower, m.start(), m.end(), _INN_CONTEXT):
                    continue
                spans.append(Span(m.start(), m.end(), "INN", digits))

        for rx in (_RE_OGRN_13, _RE_OGRN_15):
            for m in rx.finditer(text):
                digits = m.group()
                if not stdnum_ogrn.is_valid(digits):
                    continue
                spans.append(Span(m.start(), m.end(), "OGRN", digits))

        for m in _RE_SNILS_FMT.finditer(text):
            raw = m.group()
            digits_only = re.sub(r"[^0-9]", "", raw)
            if _snils_is_valid(digits_only):
                spans.append(Span(m.start(), m.end(), "SNILS", raw))

        for m in _RE_SNILS_PLAIN.finditer(text):
            digits = m.group()
            if not self._has_context(text_lower, m.start(), m.end(), _SNILS_CONTEXT):
                continue
            if _snils_is_valid(digits):
                spans.append(Span(m.start(), m.end(), "SNILS", digits))

        return spans

    def _regex_entities(self, text: str) -> list[Span]:
        spans: list[Span] = []
        text_lower = text.lower()

        for entity_type, regex, needs_ctx, ctx_words in _REGEX_PATTERNS:
            for m in regex.finditer(text):
                raw = m.group()
                if needs_ctx and not self._has_context(text_lower, m.start(), m.end(), ctx_words):
                    continue
                if entity_type == "PERSON" and not self._accept_person(raw):
                    continue
                if entity_type == "ORGANIZATION" and not self._accept_org(raw):
                    continue
                spans.append(Span(m.start(), m.end(), entity_type, raw))

        return spans

    @staticmethod
    def _accept_person(raw: str) -> bool:
        text = raw.strip()
        if text.lower() in _PERSON_DENY_SET:
            return False
        parts = re.split(r"[\s\-/]+", text)
        if any(w.lower() in _PERSON_DENY_SET for w in parts if w):
            return False
        words = [w for w in text.split() if len(w) > 1]
        if not words or not all(w[0].isupper() for w in words):
            return False
        return " " in text or "." in text or len(text) >= _PERSON_MIN_LEN_SINGLE_WORD

    @staticmethod
    def _accept_org(raw: str) -> bool:
        first_word = raw.strip().split()[0].rstrip("-")
        return first_word not in _ORG_ABBREV_DENY_SET

    @staticmethod
    def _has_context(
        text_lower: str,
        start: int,
        end: int,
        ctx_words: frozenset[str],
    ) -> bool:
        win_start = max(0, start - _CONTEXT_WINDOW)
        win_end = min(len(text_lower), end + _CONTEXT_WINDOW)
        window = text_lower[win_start:win_end]
        return any(
            re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", window)
            for w in ctx_words
        )

    @staticmethod
    def _dedup_spans(spans: list[Span]) -> list[Span]:
        """Remove overlapping spans using greedy left-to-right selection.

        Spans are sorted by (start, -length, priority), so for spans starting
        at the same position the longest one wins. Once a span is accepted,
        any later span whose start falls inside it is dropped. This does NOT
        resolve partial overlaps between spans starting at different positions
        — the earlier-starting span always wins regardless of length.
        """
        _priority: dict[str, int] = {
            "INN": 0, "OGRN": 1, "SNILS": 2, "BIK": 3,
            "BANK_ACCOUNT": 4, "KPP": 5, "CADASTRAL": 6,
            "RU_PASSPORT": 7, "PHONE_NUMBER": 8, "EMAIL_ADDRESS": 9,
            "URL": 10, "IP_ADDRESS": 11, "CREDIT_CARD": 12,
            "PERSON": 20, "ORGANIZATION": 21, "LOCATION": 22,
        }

        spans.sort(
            key=lambda s: (
                s.start,
                -(s.end - s.start),
                _priority.get(s.entity_type, 99),
            )
        )

        result: list[Span] = []
        last_end = -1
        for sp in spans:
            if sp.start >= last_end:
                result.append(sp)
                last_end = sp.end
        return result


# ══════════════════════════════════════════════════════════════════════════════
# Module-level singleton + public API
# ══════════════════════════════════════════════════════════════════════════════

_pipeline: NatashaPipeline | None = None
_pipeline_lock = threading.Lock()


def _get_pipeline() -> NatashaPipeline:
    global _pipeline
    if _pipeline is None:
        with _pipeline_lock:
            if _pipeline is None:
                _pipeline = NatashaPipeline()
    return _pipeline


def anonymize(text: str) -> tuple[str, dict[str, str]]:
    """Anonymize PII in *text* and return (anonymized_text, entities_map)."""
    result = _get_pipeline().sanitize(text)
    return result.sanitized_text, result.entities_map
