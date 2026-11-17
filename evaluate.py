"""Score outputs/ against answer_key/. This is the only code that reads answer_key/.

    python evaluate.py   # prints metrics and mismatches, writes evaluation/results.json
"""
import json
import re
import sys
from pathlib import Path

from pipeline import THRESHOLD

ROOT = Path(__file__).parent
KEY_DIR, OUT_DIR, EVAL_DIR = ROOT / "answer_key", ROOT / "outputs", ROOT / "evaluation"
SWEEP = (0.5, 0.6, 0.7, 0.8, 0.9)


def squash(s):
    return re.sub(r"[\W_]+", "", (s or "").lower())


def same_qty(a, b):
    return (a is None and b is None) or (a is not None and b is not None and float(a) == float(b))


def same_unit(a, b):
    return (a or "").strip().upper() == (b or "").strip().upper()


def ratio(num, den):
    return round(num / den, 4) if den else 0.0


def needs_review_at(line, threshold):
    conf = line.get("confidence")
    return (line.get("ridgeline_part_number") is None or conf is None or conf < threshold
            or bool(line.get("flags")))


def key_lines(data):
    lines = data.get("lines") if isinstance(data, dict) else data
    if not isinstance(lines, list):
        sys.exit("answer key format not recognised: expected a list of lines or an object with 'lines'")
    return lines


def load_pairs():
    """Returns [(rfq_id, key_line, our_line_or_None)] aligned by line number."""
    pairs = []
    for key_path in sorted(KEY_DIR.glob("*.json")):
        data = json.loads(key_path.read_text(encoding="utf-8"))
        rfq_id = data.get("rfq_id", key_path.stem) if isinstance(data, dict) else key_path.stem
        out_path = OUT_DIR / f"{rfq_id}.json"
        ours = json.loads(out_path.read_text(encoding="utf-8"))["lines"] if out_path.exists() else []
        by_number = {l["line"]: l for l in ours}
        for i, k in enumerate(key_lines(data), 1):
            pairs.append((rfq_id, k, by_number.get(k.get("line", i))))
    return pairs


def evaluate(pairs):
    extraction_ok = predicted = expected = correct = null_expected = null_correct = 0
    present = [o for _, _, o in pairs if o is not None]
    wrong = []  # our lines that returned a wrong or unwanted part
    for rfq_id, k, o in pairs:
        exp = k.get("expected_part_number")
        got = o.get("ridgeline_part_number") if o else None
        ext_ok = bool(o) and same_qty(k.get("quantity"), o.get("quantity")) and same_unit(
            k.get("unit"), o.get("unit")) and squash(k.get("as_written")) in squash(o.get("as_written"))
        extraction_ok += ext_ok
        predicted += got is not None
        expected += exp is not None
        correct += got is not None and got == exp
        null_expected += exp is None
        null_correct += exp is None and o is not None and got is None
        if got is not None and got != exp:
            wrong.append(o)
        if not ext_ok or got != exp:
            n = k.get("line")
            print(f"MISMATCH {rfq_id} line {n}: {k.get('as_written')!r}")
            if o is None:
                print("    got: line missing")
                continue
            for field, a, b in (("quantity", k.get("quantity"), o.get("quantity")),
                                ("unit", k.get("unit"), o.get("unit")),
                                ("part", exp, got)):
                if (field == "quantity" and not same_qty(a, b)) or (field == "unit" and not same_unit(a, b)) \
                        or (field == "part" and a != b):
                    print(f"    {field}: expected {a!r}, got {b!r}")
            if squash(k.get("as_written")) not in squash(o.get("as_written")):
                print(f"    as_written: expected {k.get('as_written')!r}, got {o.get('as_written')!r}")
            if got != exp:
                print(f"    method={o.get('match_method')} confidence={o.get('confidence')} "
                      f"needs_review={o.get('needs_review')} flags={o.get('flags')}")

    def at(threshold):
        return {"review_rate": ratio(sum(needs_review_at(o, threshold) for o in present), len(present)),
                "wrong_not_reviewed": sum(not needs_review_at(o, threshold) for o in wrong)}

    return {
        "threshold": THRESHOLD,
        "extraction_accuracy": ratio(extraction_ok, len(pairs)),
        "match_precision": ratio(correct, predicted),
        "match_recall": ratio(correct, expected),
        "additional_metrics": {
            "lines": len(pairs),
            "null_lines_correct": null_correct,
            "null_lines_expected": null_expected,
            "review_rate": ratio(sum(bool(o.get("needs_review")) for o in present), len(present)),
            "wrong_not_reviewed": sum(not o.get("needs_review") for o in wrong),
            "threshold_sweep": {str(t): at(t) for t in SWEEP},
        },
    }


def main():
    pairs = load_pairs()
    if not pairs:
        sys.exit("no answer key files found in answer_key/")
    results = evaluate(pairs)
    for key in ("threshold", "extraction_accuracy", "match_precision", "match_recall"):
        assert 0 <= results[key] <= 1, f"{key} out of range: {results[key]}"
    EVAL_DIR.mkdir(exist_ok=True)
    (EVAL_DIR / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
