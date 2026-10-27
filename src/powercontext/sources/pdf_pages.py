"""PDF full-document physical page parser, source definition, and evidence page retrieval."""

from __future__ import annotations

from collections import Counter
import hashlib
import io
import math
import re
from urllib.parse import quote, unquote

from pydantic import BaseModel, ConfigDict, model_validator
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from powercontext.builtin.artifacts.memory.models import MemoryCitation
from powercontext.builtin.runtime.models import GetMemoryEntryRequest, SearchMemoryRequest
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

MAX_MEMORY_ENTRY_TEXT_BYTES = 8192
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"[a-z]+|\d+")


def _validate_document_id(document_id: object) -> str:
    if not isinstance(document_id, str) or not document_id or not document_id.strip():
        raise ValueError("document_id must be a non-empty string")
    if document_id != document_id.strip():
        raise ValueError("document_id must not contain leading or trailing whitespace")
    return document_id


def _validate_sha256(value: object) -> str:
    if not isinstance(value, str) or not _SHA256_RE.match(value):
        raise ValueError("document_sha256 must be a 64-character lowercase hex SHA-256 digest")
    return value


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """Build a collision-free, length-bounded source identity for one physical PDF page."""

    _validate_document_id(document_id)
    _validate_sha256(document_sha256)
    if type(page_number) is not int or page_number < 1:
        raise ValueError("page_number must be a positive integer")
    enc_doc = quote(document_id, safe="")
    candidate = f"pdf-page:{enc_doc}:{document_sha256}:{page_number}"
    if len(candidate) <= MAX_SOURCE_ID_LENGTH:
        return candidate
    doc_hash = hashlib.sha256(document_id.encode("utf-8")).hexdigest()[:32]
    return f"pdf-page:h:{doc_hash}:{document_sha256}:{page_number}"


def _parse_source_id_location(source_id: str) -> tuple[str, int] | None:
    if not source_id.startswith("pdf-page:"):
        return None
    parts = source_id.split(":")
    if len(parts) == 4 and parts[0] == "pdf-page":
        try:
            doc_id = unquote(parts[1])
            page_num = int(parts[3])
            return doc_id, page_num
        except ValueError:
            return None
    return None


def _fit_page_text_for_memory(document_id: str, raw_text: str) -> str:
    if not raw_text or not raw_text.strip():
        return ""
    prefix_bytes = len(f"{document_id}\n".encode("utf-8"))
    max_text_bytes = MAX_MEMORY_ENTRY_TEXT_BYTES - prefix_bytes
    if max_text_bytes <= 0:
        raise ValueError("document_id exceeds memory entry byte limit")
    if len(raw_text.encode("utf-8")) <= max_text_bytes:
        return raw_text
    compacted = re.sub(r"[ \t\f\v]+", " ", raw_text)
    compacted = re.sub(r" *\n *", "\n", compacted)
    compacted = re.sub(r"\n{2,}", "\n", compacted).strip()
    if len(compacted.encode("utf-8")) <= max_text_bytes:
        return compacted
    encoded = compacted.encode("utf-8")[:max_text_bytes]
    return encoded.decode("utf-8", errors="ignore").rstrip()


class PDFDocumentInput(BaseModel):
    """Complete raw PDF byte payload and caller-supplied document identifier."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    """Extracted text and provenance for one physical PDF page (1-indexed)."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    document_sha256: str
    page_number: int
    text: str

    @model_validator(mode="after")
    def _validate_page(self) -> PDFPageInput:
        _validate_document_id(self.document_id)
        _validate_sha256(self.document_sha256)
        if type(self.page_number) is not int or self.page_number < 1:
            raise ValueError("page_number must be a positive integer")
        if not isinstance(self.text, str):
            raise ValueError("text must be a string")
        pdf_page_source_id(self.document_id, self.document_sha256, self.page_number)
        return self


class PDFPageSource(Source):
    """Captured Source representing one physical PDF page."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    document_sha256: str
    page_number: int
    text: str

    @model_validator(mode="after")
    def _validate_source(self) -> PDFPageSource:
        expected_name = pdf_page_source_id(self.document_id, self.document_sha256, self.page_number)
        if self.name != expected_name:
            raise ValueError("PDFPageSource name must match pdf_page_source_id")
        if self.materialization != SourceMaterialization.CAPTURED:
            raise ValueError("PDFPageSource materialization must be CAPTURED")
        return self


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Atomically parse all physical pages of a complete PDF byte payload."""

    if not isinstance(value, PDFDocumentInput):
        raise ValueError("value must be a PDFDocumentInput")
    document_id = _validate_document_id(value.document_id)
    if not isinstance(value.content, bytes) or len(value.content) == 0:
        raise ValueError("PDF content must be non-empty bytes")

    document_sha256 = hashlib.sha256(value.content).hexdigest()

    try:
        reader = PdfReader(io.BytesIO(value.content), strict=False)
        if reader.is_encrypted:
            decrypt_status = reader.decrypt("")
            if not decrypt_status:
                raise ValueError("Encrypted PDF requires a non-empty user password")
        num_pages = len(reader.pages)
        if num_pages <= 0:
            raise ValueError("PDF document contains no pages")

        pages: list[PDFPageInput] = []
        for idx in range(num_pages):
            page_obj = reader.pages[idx]
            extracted = page_obj.extract_text()
            if extracted is None:
                extracted = ""
            page_text = _fit_page_text_for_memory(document_id, extracted)
            pages.append(
                PDFPageInput(
                    document_id=document_id,
                    document_sha256=document_sha256,
                    page_number=idx + 1,
                    text=page_text,
                )
            )
    except (PyPdfError, OSError, EOFError, IndexError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError(f"Failed to parse PDF document {document_id!r}: {exc}") from exc
    except Exception as exc:
        raise ValueError(f"Corrupt or unreadable PDF document {document_id!r}: {exc}") from exc

    return tuple(pages)


class _PDFPageTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type="pdf-page",
            source_id=source.name,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


class _PDFPageSourceDefinition:
    name = "pdf-page"
    version = "1"
    input_class = PDFPageInput
    source_class = PDFPageSource
    projections = (_PDFPageTextEvidenceProjection(),)

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        if not isinstance(value, PDFPageInput):
            raise ValueError("value must be PDFPageInput")
        source_id = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=source_id,
            definition_version=self.version,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        if not isinstance(source, PDFPageSource):
            raise ValueError("source must be PDFPageSource")
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


PDF_PAGE_SOURCE_DEFINITION: SourceDefinition[PDFPageInput, PDFPageSource, PDFPageInput] = (
    _PDFPageSourceDefinition()
)

_QUESTION_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "calculate", "compute",
    "define", "details", "did", "do", "does", "for", "from", "give", "had", "has",
    "have", "how", "if", "in", "into", "is", "it", "its", "many", "much", "not",
    "of", "on", "only", "or", "outlined", "perspective", "please", "primarily",
    "relying", "reported", "respond", "response", "shown", "state", "statement",
    "statements", "than", "that", "the", "their", "then", "there", "these", "they",
    "this", "to", "unadjusted", "use", "using", "was", "were", "what", "when",
    "where", "which", "who", "within", "with", "would", "usd", "millions", "million",
    "billions", "billion", "thousands", "thousand", "analyst", "investment", "assuming",
    "explicitly", "directly", "quantity", "materially", "important", "ongoing",
})

_BOILERPLATE_PATTERNS = (
    re.compile(r"respond to the question by assuming.*$", re.IGNORECASE),
    re.compile(r"give a response to the question by relying.*$", re.IGNORECASE),
    re.compile(r"if [a-z0-9 ,_-]+ are not explicitly outlined.*$", re.IGNORECASE),
    re.compile(r"answer in usd (?:millions|billions|thousands)\.?", re.IGNORECASE),
)


def _stem_token(tok: str) -> str:
    if len(tok) > 5 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 5 and tok.endswith("ing"):
        return tok[:-3]
    if len(tok) > 4 and tok.endswith("ed"):
        return tok[:-2]
    if len(tok) > 4 and tok.endswith("es"):
        return tok[:-2]
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def _tokenize_lower(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.casefold())


class _IndexedPage:
    __slots__ = (
        "citation",
        "document_id",
        "head_text",
        "is_toc_like",
        "lower_text",
        "numeric_count",
        "page_number",
        "stem_counts",
        "token_counts",
        "token_len",
    )

    def __init__(
        self,
        *,
        citation: MemoryCitation,
        document_id: str,
        page_number: int,
        text: str,
    ) -> None:
        self.citation = citation
        self.document_id = document_id
        self.page_number = page_number
        self.lower_text = text.casefold()
        self.head_text = "\n".join(self.lower_text.splitlines()[:18])
        tokens = _tokenize_lower(self.lower_text)
        self.token_len = max(1, len(tokens))
        self.token_counts = Counter(tokens)
        self.stem_counts = Counter(_stem_token(t) for t in tokens)
        self.numeric_count = sum(1 for t in tokens if t.isdigit())
        dotted_leaders = len(re.findall(r"(?:\.\s*){4,}\d+", self.lower_text))
        self.is_toc_like = (
            "table of contents" in self.head_text
            or "index to consolidated financial statements" in self.head_text
            or dotted_leaders >= 3
        )


_SCOPE_INDEX_CACHE: dict[tuple[str, int], tuple[dict[tuple[str, int], _IndexedPage], dict[str, float], float]] = {}


def _detect_Question_intents(q_lower: str) -> dict[str, bool]:
    need_income = any(
        kw in q_lower
        for kw in (
            "income statement",
            "statements of income",
            "statement of operations",
            "statements of operations",
            "statement of earnings",
            "p&l",
            "ebitda",
            "ebit",
            "operating income",
            "operating profit",
            "operating margin",
            "net revenue",
            "net sales",
            "total revenue",
            "gross profit",
            "gross margin",
            "cost of sales",
            "cost of goods sold",
            "cogs",
            "sg&a",
            "selling, general",
            "research and development",
            "r&d",
            "restructuring",
            "net income",
            "net earnings",
            "diluted eps",
            "interest expense",
            "effective tax rate",
            "income tax",
        )
    )
    need_cashflow = any(
        kw in q_lower
        for kw in (
            "cash flow",
            "cash flows",
            "capex",
            "capital expenditure",
            "capital spending",
            "depreciation and amortization",
            "d&a",
            "depreciation",
            "amortization",
            "free cash flow",
            "fcf",
            "operating activities",
            "investing activities",
            "financing activities",
            "dividends paid",
            "share repurchase",
            "stock repurchase",
        )
    )
    need_balancesheet = any(
        kw in q_lower
        for kw in (
            "balance sheet",
            "financial position",
            "working capital",
            "current ratio",
            "quick ratio",
            "total assets",
            "current assets",
            "total liabilities",
            "current liabilities",
            "inventories",
            "inventory",
            "accounts receivable",
            "accounts payable",
            "long-term debt",
            "short-term debt",
            "total debt",
            "net debt",
            "cash and cash equivalents",
            "marketable securities",
            "property, plant and equipment",
            "pp&e",
            "goodwill",
            "intangible",
            "retained earnings",
            "shareholders' equity",
            "stockholders' equity",
            "total equity",
            "return on assets",
            "roa",
            "return on equity",
            "roe",
            "roic",
            "invested capital",
            "book value",
        )
    )
    need_legal = any(
        kw in q_lower
        for kw in (
            "legal battle",
            "legal battles",
            "legal proceeding",
            "legal proceedings",
            "litigation",
            "lawsuit",
            "lawsuits",
            "contingenc",
            "regulatory inquiry",
            "materially important ongoing legal",
        )
    )
    need_geography = any(
        kw in q_lower
        for kw in (
            "geograph",
            "primarily operates",
            "operates in",
            "countries",
            "regions",
            "international",
            "segment",
            "divisions",
            "revenue by region",
            "revenue by country",
        )
    )
    return {
        "income": need_income,
        "cashflow": need_cashflow,
        "balancesheet": need_balancesheet,
        "legal": need_legal,
        "geography": need_geography,
    }


def _statement_page_scores(page: _IndexedPage) -> dict[str, float]:
    txt = page.lower_text
    head = page.head_text
    num_bonus = min(1.0, page.numeric_count / 25.0)
    toc_penalty = 0.25 if page.is_toc_like else 1.0

    # 1. Income Statement
    inc = 0.0
    if any(
        h in head
        for h in (
            "consolidated statement of income",
            "consolidated statements of income",
            "consolidated statement of operations",
            "consolidated statements of operations",
            "consolidated statement of earnings",
            "consolidated statements of earnings",
            "statements of consolidated income",
            "statement of consolidated income",
            "statements of consolidated operations",
        )
    ):
        inc += 35.0
    elif any(
        h in txt
        for h in (
            "consolidated statement of income",
            "consolidated statements of income",
            "consolidated statements of operations",
            "consolidated statements of earnings",
        )
    ):
        inc += 12.0
    inc_items = sum(
        1
        for item in (
            "net revenue",
            "net sales",
            "total revenue",
            "cost of sales",
            "cost of goods sold",
            "gross profit",
            "selling, general and administrative",
            "operating profit",
            "operating income",
            "net income",
            "basic",
            "diluted",
            "in millions",
        )
        if item in txt
    )
    if inc_items >= 4 and page.numeric_count >= 15:
        inc += 4.0 * inc_items * num_bonus
    inc *= toc_penalty

    # 2. Cash Flow Statement
    cf = 0.0
    if any(
        h in head
        for h in (
            "consolidated statement of cash flows",
            "consolidated statements of cash flows",
            "statement of consolidated cash flows",
            "statements of consolidated cash flows",
        )
    ):
        cf += 35.0
    elif "consolidated statement of cash flows" in txt or "consolidated statements of cash flows" in txt:
        cf += 12.0
    cf_items = sum(
        1
        for item in (
            "operating activities",
            "investing activities",
            "financing activities",
            "depreciation and amortization",
            "capital spending",
            "capital expenditures",
            "additions to property, plant",
            "cash and cash equivalents",
            "net cash provided by operating",
            "net cash used for investing",
            "net cash used for financing",
        )
        if item in txt
    )
    if cf_items >= 3 and page.numeric_count >= 15:
        cf += 4.5 * cf_items * num_bonus
    cf *= toc_penalty

    # 3. Balance Sheet
    bs = 0.0
    if any(
        h in head
        for h in (
            "consolidated balance sheet",
            "consolidated balance sheets",
            "consolidated statement of financial position",
            "consolidated statements of financial position",
        )
    ):
        bs += 35.0
    elif "consolidated balance sheet" in txt or "consolidated statements of financial position" in txt:
        bs += 12.0
    bs_items = sum(
        1
        for item in (
            "current assets",
            "total assets",
            "current liabilities",
            "total liabilities",
            "accounts receivable",
            "inventories",
            "property, plant and equipment",
            "goodwill",
            "accounts payable",
            "long-term debt",
            "retained earnings",
        )
        if item in txt
    )
    if bs_items >= 4 and page.numeric_count >= 15:
        bs += 4.0 * bs_items * num_bonus
    bs *= toc_penalty

    # 4. Legal Proceedings (Item 3 & Contingencies)
    leg = 0.0
    if "item 3. legal proceedings" in txt or "item 3.  legal proceedings" in txt or "item 3.legal proceedings" in txt:
        if not page.is_toc_like:
            leg += 45.0
    if "legal proceedings" in txt and ("contingencies" in txt or "litigation" in txt or "environmental" in txt):
        leg += 18.0 * toc_penalty

    # 5. Geography / Segments / Item 1 Business
    geo = 0.0
    if "item 1. business" in txt or "item 1.  business" in txt:
        if not page.is_toc_like:
            geo += 30.0
    if ("united states" in txt and ("international" in txt or "countries" in txt or "mexico" in txt or "canada" in txt or "europe" in txt)) and (
        "net revenue" in txt or "revenue" in txt or "long-lived assets" in txt or "segment" in txt or "divisions" in txt
    ):
        geo += 28.0 * toc_penalty
    if ("latin america" in txt or "europe" in txt or "asia" in txt or "africa" in txt) and (
        "north america" in txt or "united states" in txt
    ):
        geo += 18.0 * toc_penalty

    return {
        "income": inc,
        "cashflow": cf,
        "balancesheet": bs,
        "legal": leg,
        "geography": geo,
    }


async def search_pdf_pages(
    memory: object,
    question: str,
    limit: int = 10,
) -> tuple[MemoryCitation, ...]:
    """Search scoped Memory and return up to `limit` distinct page-level MemoryCitations."""

    if limit <= 0 or not isinstance(question, str) or not question.strip():
        return ()

    cleaned_question = question
    for pat in _BOILERPLATE_PATTERNS:
        cleaned_question = pat.sub(" ", cleaned_question)
    q_tokens_raw = _tokenize_lower(cleaned_question)
    q_terms = [t for t in q_tokens_raw if t not in _QUESTION_STOPWORDS and (len(t) > 1 or t.isdigit())]
    if not q_terms:
        q_terms = _tokenize_lower(question)[:6]

    short_fts_query = " ".join(q_terms[:2]) if q_terms else question.strip()
    search_hits = ()
    if hasattr(memory, "search"):
        page_res = await memory.search(  # type: ignore[attr-defined]
            SearchMemoryRequest(
                query=short_fts_query,
                mode="fts",
                limit=min(max(limit, 1), 50),
            )
        )
        search_hits = page_res.hits

    entries_page = await memory.list() if hasattr(memory, "list") else None  # type: ignore[attr-defined]
    if entries_page is None or entries_page.memory_ref is None or not entries_page.entries:
        # Fallback if custom memory object only supports search + get
        seen_locs: set[tuple[str, int]] = set()
        fallback_citations: list[MemoryCitation] = []
        for hit in search_hits:
            cit = MemoryCitation(
                memory_ref=hit.memory_ref,
                entry_id=hit.entry_id,
                entry_version_id=hit.entry_version_id,
            )
            if hasattr(memory, "get"):
                rec = await memory.get(GetMemoryEntryRequest(citation=cit))  # type: ignore[attr-defined]
                for ref in rec.entry.sources:
                    loc = _parse_source_id_location(ref.source_id)
                    if loc is not None and loc not in seen_locs:
                        seen_locs.add(loc)
                        fallback_citations.append(cit)
                        break
            else:
                fallback_citations.append(cit)
            if len(fallback_citations) >= limit:
                break
        return tuple(fallback_citations)

    cache_key = (entries_page.memory_ref.artifact_id, entries_page.memory_ref.revision)
    cached = _SCOPE_INDEX_CACHE.get(cache_key)
    if cached is None:
        pages_by_loc: dict[tuple[str, int], _IndexedPage] = {}
        df_stem: Counter[str] = Counter()
        total_len = 0
        for rec in entries_page.entries:
            loc: tuple[str, int] | None = None
            for ref in rec.entry.sources:
                loc = _parse_source_id_location(ref.source_id)
                if loc is not None:
                    break
            if loc is None or loc in pages_by_loc:
                continue
            doc_id, page_num = loc
            idx_page = _IndexedPage(
                citation=rec.citation,
                document_id=doc_id,
                page_number=page_num,
                text=rec.entry.text,
            )
            pages_by_loc[loc] = idx_page
            total_len += idx_page.token_len
            for st in idx_page.stem_counts:
                df_stem[st] += 1

        n_docs = max(1, len(pages_by_loc))
        idf_map = {
            st: math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
            for st, df in df_stem.items()
        }
        avgdl = total_len / n_docs if pages_by_loc else 1.0
        cached = (pages_by_loc, idf_map, avgdl)
        if len(_SCOPE_INDEX_CACHE) > 8:
            _SCOPE_INDEX_CACHE.clear()
        _SCOPE_INDEX_CACHE[cache_key] = cached

    pages_by_loc, idf_map, avgdl = cached
    if not pages_by_loc:
        return ()

    q_lower = question.casefold()
    q_norm = re.sub(r"[^a-z0-9]", "", q_lower)
    q_years = set(re.findall(r"(?:19|20)\d{2}", q_lower))
    for fy_short in re.findall(r"\bfy\s*(\d{2})\b", q_lower):
        q_years.add(f"20{fy_short}")

    # Expand synonyms into weighted query stems
    query_weights: dict[str, float] = {}
    for t in q_terms:
        st = _stem_token(t)
        query_weights[st] = max(query_weights.get(st, 0.0), 2.2)

    intents = _detect_Question_intents(q_lower)
    expansion_terms: list[tuple[str, float]] = []
    if intents["income"]:
        expansion_terms.extend([
            ("consolidated", 1.2), ("income", 1.6), ("operations", 1.4), ("revenue", 1.5),
            ("sales", 1.3), ("operating", 1.5), ("profit", 1.5), ("cost", 1.2), ("gross", 1.2),
            ("selling", 1.0), ("administrative", 1.0), ("restructuring", 1.8), ("diluted", 1.0),
        ])
    if intents["cashflow"]:
        expansion_terms.extend([
            ("cash", 1.6), ("flows", 1.6), ("operating", 1.3), ("investing", 1.3),
            ("depreciation", 1.8), ("amortization", 1.8), ("capital", 1.6), ("spending", 1.6),
            ("expenditures", 1.6), ("additions", 1.2), ("property", 1.1),
        ])
    if intents["balancesheet"]:
        expansion_terms.extend([
            ("balance", 1.6), ("sheets", 1.6), ("assets", 1.6), ("liabilities", 1.6),
            ("current", 1.3), ("inventories", 1.4), ("receivable", 1.4), ("payable", 1.4),
            ("debt", 1.4), ("equity", 1.4), ("retained", 1.2),
        ])
    if intents["legal"]:
        expansion_terms.extend([
            ("item", 1.4), ("legal", 2.2), ("proceedings", 2.4), ("litigation", 2.0),
            ("contingencies", 1.8), ("claims", 1.4), ("environmental", 1.2), ("lawsuits", 1.8),
        ])
    if intents["geography"]:
        expansion_terms.extend([
            ("united", 1.5), ("states", 1.5), ("international", 1.6), ("countries", 1.8),
            ("country", 1.8), ("geographic", 1.8), ("divisions", 1.6), ("segment", 1.5),
            ("europe", 1.4), ("latin", 1.4), ("america", 1.4), ("asia", 1.3), ("item", 1.1), ("business", 1.3),
        ])
    for yr in q_years:
        expansion_terms.append((yr, 1.8))

    for term, weight in expansion_terms:
        st = _stem_token(term)
        if st not in query_weights:
            query_weights[st] = weight

    # Compute document-level match scores
    all_doc_ids = {doc_id for doc_id, _ in pages_by_loc}
    doc_bonuses: dict[str, float] = {}
    for doc_id in all_doc_ids:
        doc_lower = doc_id.casefold()
        parts = re.split(r"[_-]+", doc_lower)
        company_parts = [p for p in parts if not re.match(r"^(?:19|20)\d{2}$|^10[kq]$|^fy\d+$|^q[1-4]$", p)]
        company_norm = re.sub(r"[^a-z0-9]", "", "".join(company_parts))
        bonus = 0.0
        if company_norm and len(company_norm) >= 2 and company_norm in q_norm:
            bonus += 60.0
        else:
            matched_parts = sum(1 for p in company_parts if len(p) >= 3 and p in q_lower)
            if matched_parts > 0:
                bonus += 35.0 * matched_parts
        for yr in q_years:
            if yr in doc_lower:
                bonus += 15.0
        doc_bonuses[doc_id] = bonus

    fts_hit_keys = {(h.entry_id, h.entry_version_id) for h in search_hits}

    page_scores: dict[tuple[str, int], float] = {}
    stmt_scores_by_loc: dict[tuple[str, int], dict[str, float]] = {}

    k1 = 1.5
    b = 0.75
    for loc, page in pages_by_loc.items():
        bm25 = 0.0
        matched_q_terms = 0
        for st, q_w in query_weights.items():
            tf = page.stem_counts.get(st, 0)
            if tf <= 0:
                continue
            if q_w >= 2.0:
                matched_q_terms += 1
            idf = idf_map.get(st, 1.0)
            denom = tf + k1 * (1.0 - b + b * (page.token_len / avgdl))
            bm25 += q_w * idf * ((tf * (k1 + 1.0)) / denom)

        stmt = _statement_page_scores(page)
        stmt_scores_by_loc[loc] = stmt

        intent_boost = 0.0
        for intent_name, active in intents.items():
            if active:
                intent_boost += stmt[intent_name]

        # Specific phrase matches
        phrase_boost = 0.0
        if "restructuring" in q_lower and "restructuring" in page.lower_text:
            phrase_boost += 20.0
            if stmt["income"] > 15.0 or "impairment" in page.lower_text:
                phrase_boost += 18.0
        if intents["legal"] and ("item 3." in page.lower_text or "legal proceedings" in page.lower_text):
            phrase_boost += 25.0
        if intents["geography"] and "long-lived assets" in page.lower_text and "united states" in page.lower_text:
            phrase_boost += 30.0

        fts_bonus = (
            4.0
            if (page.citation.entry_id, page.citation.entry_version_id) in fts_hit_keys
            else 0.0
        )
        toc_pen = -12.0 if page.is_toc_like and intent_boost == 0 else 0.0
        coverage_bonus = 2.5 * matched_q_terms

        page_scores[loc] = (
            doc_bonuses.get(page.document_id, 0.0)
            + bm25
            + coverage_bonus
            + intent_boost
            + phrase_boost
            + fts_bonus
            + toc_pen
        )

    # Neighbor page propagation: multi-page financial statements / tables often continue on page + 1 or page - 1
    neighbor_adjusted = dict(page_scores)
    for (doc_id, pno), base_score in page_scores.items():
        stmt = stmt_scores_by_loc[(doc_id, pno)]
        if any(stmt[k] >= 25.0 for k in ("income", "cashflow", "balancesheet", "legal", "geography")):
            for delta, factor in ((1, 0.35), (-1, 0.20)):
                n_loc = (doc_id, pno + delta)
                if n_loc in neighbor_adjusted:
                    neighbor_adjusted[n_loc] = max(
                        neighbor_adjusted[n_loc],
                        doc_bonuses.get(doc_id, 0.0) + 0.55 * (base_score - doc_bonuses.get(doc_id, 0.0)),
                    )
                    neighbor_adjusted[n_loc] += factor * max(stmt.values())

    # Ensure top primary statement pages for every active intent in the top-scoring document(s) are pinned near the top
    target_docs = sorted(all_doc_ids, key=lambda d: doc_bonuses.get(d, 0.0), reverse=True)
    if target_docs and doc_bonuses.get(target_docs[0], 0.0) > 0:
        max_doc_bonus = doc_bonuses[target_docs[0]]
        primary_docs = [d for d in target_docs if doc_bonuses.get(d, 0.0) >= max_doc_bonus - 15.0]
    else:
        primary_docs = list(target_docs[:2])

    pinned_locs: list[tuple[str, int]] = []
    for d_id in primary_docs:
        doc_locs = [loc for loc in pages_by_loc if loc[0] == d_id]
        for intent_name in ("income", "cashflow", "balancesheet", "legal", "geography"):
            if not intents[intent_name]:
                continue
            best_for_intent = sorted(
                doc_locs,
                key=lambda loc: (stmt_scores_by_loc[loc][intent_name], neighbor_adjusted[loc]),
                reverse=True,
            )
            for candidate_loc in best_for_intent[:2]:
                if stmt_scores_by_loc[candidate_loc][intent_name] >= 20.0 and candidate_loc not in pinned_locs:
                    pinned_locs.append(candidate_loc)

    ranked_locs = sorted(
        pages_by_loc.keys(),
        key=lambda loc: (-neighbor_adjusted[loc], loc[0], loc[1]),
    )

    final_locs: list[tuple[str, int]] = []
    seen_final: set[tuple[str, int]] = set()
    for loc in pinned_locs + ranked_locs:
        if loc in seen_final:
            continue
        seen_final.add(loc)
        final_locs.append(loc)
        if len(final_locs) >= limit:
            break

    return tuple(pages_by_loc[loc].citation for loc in final_locs)


__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]
