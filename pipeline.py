"""Ridgeline Quote Desk: extract line items from every RFQ in rfqs/, match them to the catalog,
and write outputs/<stem>.json.

    python pipeline.py               # extract with the LLM, then match
    python pipeline.py --match-only  # reuse extractions/<stem>.json, no API calls
"""
import csv
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import anthropic
import openpyxl
import pdfplumber
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

ROOT = Path(__file__).parent
RFQ_DIR, OUT_DIR, EXTRACT_DIR = ROOT / "rfqs", ROOT / "outputs", ROOT / "extractions"
THRESHOLD = 0.8  # below this confidence a line needs review


# ---------- 1. Loaders: each returns plain text ----------

def load_txt(path):
    return path.read_text(encoding="utf-8", errors="replace")


def load_pdf(path):
    with pdfplumber.open(path) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def load_xlsx(path):
    rows = []
    for ws in openpyxl.load_workbook(path, data_only=True).worksheets:
        for row in ws.iter_rows(values_only=True):
            if any(c is not None and str(c).strip() for c in row):
                rows.append(" | ".join("" if c is None else str(c) for c in row))
    return "\n".join(rows)


LOADERS = {".txt": load_txt, ".pdf": load_pdf, ".xlsx": load_xlsx}


# ---------- 2. Extraction: one LLM call per RFQ ----------

class ExtractedLine(BaseModel):
    as_written: str
    quantity: float | None
    unit_as_written: str | None
    part_number_as_written: str | None
    description: str
    refers_to_previous_line: bool
    refers_to_unknown_history: bool
    amended_by_thread: bool
    flags: list[str]
    note: str | None


class Extraction(BaseModel):
    customer_name: str | None
    lines: list[ExtractedLine]
    non_item_rows: list[str]


SYSTEM_PROMPT = """You extract line items from a customer's request for quote (RFQ) sent to Ridgeline Industrial Supply.
You copy what is written. You do NOT choose or suggest Ridgeline part numbers.

Return JSON only (no prose, no code fences) with exactly this shape:
{
  "customer_name": string or null,      // the customer company name as written (not Ridgeline)
  "lines": [
    {
      "as_written": string,              // the line's text verbatim, including any part number on it
      "quantity": number or null,        // null if not written; vague amounts like "a few" are null
      "unit_as_written": string or null, // e.g. "ea", "boxes", "PAIL"; null if not written
      "part_number_as_written": string or null, // any part number on the line, exactly as written
      "description": string,             // item description with abbreviations expanded
      "refers_to_previous_line": boolean,
      "refers_to_unknown_history": boolean,
      "amended_by_thread": boolean,
      "flags": [string],
      "note": string or null
    }
  ],
  "non_item_rows": [string]              // text that is not a line item, e.g. delivery notes
}

Rules:
- Never invent a quantity, a part number or a price. If something is not written, return null and add a flag
  such as "quantity_missing", "unit_missing" or "part_number_missing".
- Never output prices.
- Expand abbreviations in description: hyd -> hydraulic, FP -> full port, gr5 -> grade 5, sealed -> 2RS,
  shielded -> ZZ, SS -> stainless steel.
- "Same as above" style lines: refers_to_previous_line = true, and description must be the previous line's
  description with the stated change applied.
- "Same as last order", "the usual" and similar: refers_to_unknown_history = true.
- Email chains: if a later reply changed a line, set amended_by_thread = true and return the FINAL values.
  as_written stays the original line text; explain the change in note.
- Keep line items in the order they appear. Do not list greetings, signatures or headers as line items.
- Use note for anything a quoting clerk should know (e.g. "customer accepts equivalents")."""


def make_client():
    load_dotenv(ROOT / ".env")
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"]), os.environ["MODEL_ID"]


def call_model(client, model, text):
    # No temperature: anthropic>=2.x (API version 2026-10-21) removed sampling parameters.
    return client.messages.create(model=model, max_tokens=16000, system=SYSTEM_PROMPT,
                                  messages=[{"role": "user", "content": f"<rfq>\n{text}\n</rfq>"}])


def parse_json(response):
    raw = "".join(b.text for b in response.content if b.type == "text").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)  # tolerate code fences
    return Extraction.model_validate_json(raw)


# ---------- 3. Extraction with one retry ----------

def extract_llm(client, model, text):
    """Returns (Extraction, None) or (None, reason)."""
    reason = None
    for _ in range(2):
        try:
            return parse_json(call_model(client, model, text)), None
        except (ValidationError, ValueError) as e:
            reason = f"invalid JSON from model: {str(e)[:300]}"
        except anthropic.APIError as e:
            reason = f"API error: {str(e)[:300]}"
    return None, reason


def extract_cached(stem):
    path = EXTRACT_DIR / f"{stem}.json"
    if not path.exists():
        return None, "no cached extraction (run without --match-only)"
    return Extraction.model_validate_json(path.read_text(encoding="utf-8")), None


# ---------- 4. Customer matching ----------

def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def load_customers():
    with open(ROOT / "customers.csv", newline="", encoding="utf-8-sig") as f:
        return [(r["customer_id"], norm(r["name"])) for r in csv.DictReader(f)]


def match_customer(customers, name, text):
    n = norm(name)
    for cid, cname in customers:
        if n and len(n) >= 5 and (cname.startswith(n) or n.startswith(cname)):
            return cid
    domains = {d.split(".")[0] for d in re.findall(r"@([\w.-]+)", text) if "ridgeline" not in d.lower()}
    for cid, cname in customers:
        if any(len(d) >= 5 and (cname.startswith(norm(d)) or norm(d).startswith(cname)) for d in domains):
            return cid
    return None


# ---------- 5. Matching: deterministic, no LLM ----------

ABBREVIATIONS = [  # same expansions as the extraction prompt, plus bolt ~ cap screw
    (r"\bhyd\b\.?", "hydraulic"), (r"\bfp\b", "full port"), (r"\bgr\.?\s?(\d)\b", r"grade \1"),
    (r"\bsealed\b", "2rs"), (r"\bshielded\b", "zz"), (r"\bss\b", "stainless steel"), (r"\bbolts?\b", "screw"),
    (r"\b(?:x-?large|extra large)\b", "xl"), (r"\blarge\b", "l"), (r"\bmedium\b", "m"), (r"\bsmall\b", "s"),
]
STOPWORDS = {"in", "inch", "x", "of", "and", "the", "a", "to", "for", "with", "per", "qty",
             "ea", "each", "box", "case", "pack", "pair"}
SIZE_WORDS = {"s", "m", "l", "xl"}
UNIT_MAP = {"ea": "EA", "each": "EA", "pc": "EA", "pcs": "EA", "piece": "EA", "pieces": "EA",
            "box": "BX", "boxes": "BX", "bx": "BX", "case": "CS", "cases": "CS", "cs": "CS",
            "pair": "PR", "pairs": "PR", "pr": "PR", "pack": "PK", "packs": "PK", "pk": "PK",
            "roll": "RL", "rolls": "RL", "ft": "FT", "foot": "FT", "feet": "FT"}
PACK_UNITS = {"BX", "PK", "CS"}
RESOLVED_BY_MATCH = {"unit_missing", "part_number_missing", "competitor_part_number", "description_missing"}
EXTRACTION_BOOLEANS = {"refers_to_previous_line", "refers_to_unknown_history", "amended_by_thread"}


def _fraction(m):
    return f"{int(m[1] or 0) + int(m[2]) / int(m[3]):g}"


def tokenize(text):
    s = (text or "").lower()
    for pattern, repl in ABBREVIATIONS:
        s = re.sub(pattern, repl, s)
    s = re.sub(r"\b(?:(\d+)[ -])?(\d+)/(\d+)\b", _fraction, s)  # 1-1/2, 1 1/2 -> 1.5; 3/8 -> 0.375
    s = re.sub(r"(\d)\s*#", r"\1 lb", s)                       # 150# -> 150 lb
    s = re.sub(r"(\d)(hp|gal|ft|mil|ml|oz|lb)\b", r"\1 \2", s)  # 5hp -> 5 hp
    tokens = []
    for t in re.split(r"[^a-z0-9.]+", s.replace('"', " ")):
        t = t.strip(".")
        if not t or t in STOPWORDS:
            continue
        if not any(c.isdigit() for c in t) and len(t) > 3:  # plurals
            t = t[:-2] if t.endswith("xes") else t[:-1] if t.endswith("s") and not t.endswith("ss") else t
        tokens.append(t)
    return tokens


def is_size(token):
    return any(c.isdigit() for c in token) or token in SIZE_WORDS


def norm_pn(pn):
    return re.sub(r"\s+", "", pn or "").upper()


class Catalog:
    def __init__(self):
        with open(ROOT / "products.csv", newline="", encoding="utf-8-sig") as f:
            self.products = {r["part_number"]: r for r in csv.DictReader(f)}
        with open(ROOT / "cross_reference.csv", newline="", encoding="utf-8-sig") as f:
            self.xref = defaultdict(list)
            for r in csv.DictReader(f):
                self.xref[norm_pn(r["competitor_part_number"])].append(r)
        self.units = {r["unit"] for r in self.products.values()}
        self.active = [(pn, set(tokenize(r["description"]))) for pn, r in self.products.items()
                       if r["status"] == "active"]
        self.df = Counter(t for _, toks in self.active for t in toks)

    def weight(self, token):  # rarer tokens count more; sizes and numbers count double
        idf = math.log((len(self.active) + 1) / (self.df[token] + 1)) + 1
        return idf * (2 if is_size(token) else 1)

    def describe(self, pn):
        return f'{pn} "{self.products[pn]["description"]}"'


def match_description(cat, description, unit=None):
    """Returns (part, confidence, flags, notes). On a near-tie, prefers a product sold in the written unit."""
    req = set(tokenize(description))
    if not req:
        return None, None, ["not_carried"], ["no description to match against the catalog"]
    w = {t: cat.weight(t) for t in req}
    total = sum(w.values())
    scored = [(round(sum(w[t] for t in req & toks) / total, 6), pn, toks) for pn, toks in cat.active]
    top = max(s for s, _, _ in scored)

    def rank(s):  # near-ties (within 0.05 of the top) sold in the written unit come first
        near = top - s[0] <= 0.05
        return (not near, not (near and cat.products[s[1]]["unit"] == unit), -s[0], s[1])

    (best, pn, toks), (second, pn2, _) = sorted(scored, key=rank)[:2]
    missing_sizes = sorted(t for t in req - toks if is_size(t))
    if best < 0.5 or missing_sizes:
        why = f"missing {', '.join(missing_sizes)}" if missing_sizes else f"coverage {best:.2f}"
        return None, None, ["not_carried"], [f"no catalog item matches; closest is {cat.describe(pn)} ({why})"]
    margin = abs(best - second)
    confidence = round(best * min(1.0, 0.7 + 2 * margin), 2)  # close runner-up -> lower confidence
    if margin <= 0.05:
        return pn, confidence, ["ambiguous_candidates"], [
            f"ambiguous: {cat.describe(pn)} vs {cat.describe(pn2)}"]
    return pn, confidence, [], []


def match_part(cat, line):
    """Returns (part, method, confidence, flags, notes). Only returns part numbers in products.csv."""
    if line.refers_to_unknown_history:
        return None, None, None, ["needs_human_review"], ["refers to a previous order we cannot see"]
    written = norm_pn(line.part_number_as_written)
    part = method = confidence = None
    flags, notes = [], []
    if written in cat.products:
        part, method, confidence = written, "ridgeline_part_number", 0.99
    elif written in cat.xref:
        rows = cat.xref[written]
        targets = sorted({r["ridgeline_part_number"] for r in rows})
        if len(targets) > 1:
            return None, None, None, ["cross_reference_ambiguous"], [f"{written} maps to {', '.join(targets)}"]
        if targets[0] in cat.products:
            part, method, confidence = targets[0], "cross_reference", 0.95
            xref_note = "; ".join(r["notes"].strip() for r in rows if r["notes"].strip())
            if xref_note:
                confidence = 0.7
                flags.append("cross_reference_note")
                notes.append(f"cross-reference note: {xref_note}")
    if part is None:
        part, confidence, more_flags, more_notes = match_description(cat, line.description,
                                                                     map_unit(line.unit_as_written))
        method = "description" if part else None
        flags += more_flags
        notes += more_notes
    if part:
        row = cat.products[part]
        if row["status"] == "discontinued" and row["replaced_by"] in cat.products:
            notes.append(f"{part} is discontinued; quoting its replacement {cat.describe(row['replaced_by'])}")
            part = row["replaced_by"]
            flags.append("discontinued_replaced")
    return part, method, confidence, flags, notes


def map_unit(written):
    raw = (written or "").strip()
    return UNIT_MAP.get(raw.lower().rstrip("."), raw.upper() or None)


def resolve_unit(cat, written, quantity, part, by_part_number=False):
    """Returns (unit, flags, notes). Never fills in a unit that changes what the written quantity means:
    a bare count against a box/pack/case product is only read as packs when the customer ordered by part
    number (the count is then in that SKU's selling unit)."""
    mapped = map_unit(written)
    product = cat.products.get(part)
    flags, notes = [], []
    if mapped in cat.units or not product:
        unit = mapped
    elif mapped is None and quantity is not None and product["unit"] in PACK_UNITS and not by_part_number:
        unit = None  # "300 bolts" vs "300 boxes" is not established by the RFQ
    else:
        unit = product["unit"]
    if product and product["unit"] in PACK_UNITS and quantity is not None and (
            mapped == "EA" or (unit is None and mapped is None)):
        size = re.search(r"(?:box|pack|case) of (\d+)", product["description"])
        flags.append("pack_quantity_check")
        notes.append(f"quantity {quantity} has no pack unit; {part} is sold per {product['unit']}"
                     + (f" ({size[0]})" if size else "") + ": confirm pieces or packs")
    if product and unit is not None and unit != product["unit"]:
        flags.append("unit_mismatch")
        notes.append(f"requested in {unit}, {part} is sold per {product['unit']}")
    return unit, flags, notes


def match_line(cat, n, line):
    part, method, confidence, flags, notes = match_part(cat, line)
    qty = int(line.quantity) if line.quantity is not None and line.quantity.is_integer() else line.quantity
    unit, unit_flags, unit_notes = resolve_unit(cat, line.unit_as_written, qty, part,
                                                by_part_number=method in ("ridgeline_part_number", "cross_reference"))
    llm_flags = [f for f in line.flags if f not in EXTRACTION_BOOLEANS and not (part and f in RESOLVED_BY_MATCH)]
    flags = list(dict.fromkeys(llm_flags + flags + unit_flags
                               + (["amended_in_thread"] if line.amended_by_thread else [])))
    if line.refers_to_previous_line:
        notes.insert(0, "same as the previous line with the stated change")
    note = "; ".join(x for x in [line.note] + notes + unit_notes if x) or None
    needs_review = part is None or confidence is None or confidence < THRESHOLD or bool(flags)
    return out_line(n, line.as_written, qty, unit, flags, note, part, method, confidence, needs_review)


# ---------- 6. Output ----------

def out_line(n, as_written, quantity=None, unit=None, flags=(), note=None,
             part=None, method=None, confidence=None, needs_review=True):
    return {"line": n, "as_written": as_written, "quantity": quantity, "unit": unit,
            "ridgeline_part_number": part, "match_method": method, "confidence": confidence,
            "needs_review": needs_review, "flags": list(flags), "note": note}


def process(path, get_extraction, customers, cat):
    result = {"rfq_id": path.stem, "customer_id": None, "customer_name": None, "lines": [], "non_item_rows": []}
    if path.suffix.lower() == ".png":  # stub
        result["lines"] = [out_line(1, "entire RFQ", flags=["stub"], note="photo source: needs OCR or a vision model")]
        return result
    text = LOADERS[path.suffix.lower()](path)
    ex, reason = get_extraction(path.stem, text)
    if ex is None or not ex.lines:
        reason = reason or "model returned no line items"
        result["lines"] = [out_line(1, "entire RFQ", flags=["extraction_failed"], note=reason)]
        result["customer_id"] = match_customer(customers, None, text)
        return result
    result.update(customer_name=ex.customer_name, non_item_rows=ex.non_item_rows,
                  customer_id=match_customer(customers, ex.customer_name, text),
                  lines=[match_line(cat, i, l) for i, l in enumerate(ex.lines, 1)])
    return result


# ---------- 7. Run ----------

def main():
    OUT_DIR.mkdir(exist_ok=True)
    EXTRACT_DIR.mkdir(exist_ok=True)
    customers, cat = load_customers(), Catalog()
    if "--match-only" in sys.argv:
        get_extraction = lambda stem, text: extract_cached(stem)
    else:
        client, model = make_client()

        def get_extraction(stem, text):
            ex, reason = extract_llm(client, model, text)
            if ex:
                (EXTRACT_DIR / f"{stem}.json").write_text(ex.model_dump_json(indent=2), encoding="utf-8")
            return ex, reason

    print(f"{'rfq':<23}{'ln':<4}{'review':<8}{'part':<10}{'method':<23}{'conf':<6}{'qty':<6}{'unit':<6}"
          f"{'as_written':<48}flags")
    for path in sorted(RFQ_DIR.iterdir()):
        if path.suffix.lower() not in LOADERS and path.suffix.lower() != ".png":
            print(f"{path.name}: skipped (unsupported format)")
            continue
        try:
            result = process(path, get_extraction, customers, cat)
        except Exception as e:  # one bad file must never crash the run
            result = {"rfq_id": path.stem, "customer_id": None, "customer_name": None, "non_item_rows": [],
                      "lines": [out_line(1, "entire RFQ", flags=["extraction_failed"], note=f"{type(e).__name__}: {e}")]}
        (OUT_DIR / f"{path.stem}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        for l in result["lines"]:
            show = lambda v: "-" if v is None else str(v)
            print(f"{result['rfq_id']:<23}{l['line']:<4}{'REVIEW' if l['needs_review'] else 'ok':<8}"
                  f"{show(l['ridgeline_part_number']):<10}{show(l['match_method']):<23}{show(l['confidence']):<6}"
                  f"{show(l['quantity']):<6}{show(l['unit']):<6}{l['as_written'].replace(chr(10), ' ')[:46]:<48}"
                  f"{','.join(l['flags']) or '-'}")


if __name__ == "__main__":
    main()
