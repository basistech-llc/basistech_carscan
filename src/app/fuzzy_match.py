import re
from rapidfuzz import process, fuzz, utils

def normalize_plate(text:str):
    """Take a plate and return a list of possible candidates"""
    assert isinstance(text,str)

    # Clean: remove non-alphanumeric
    clean = re.sub(r'[^a-zA-Z0-9]', '', text).upper()
    candidates = [clean]

    # MA EV plates are usually "EV" + 3 or 4 chars (e.g., "EV123" or "EVF895").
    # If OCR missed the stacked "EV", we are left with a 3-4 char string.
    # We allow the first char to be a digit OR a letter (to catch 'F895').
    if len(clean) in [3, 4]:
        candidates.append(f"EV{clean}")

    return candidates

def find_closest_plate_entry(plates, ocr_text, aws_confidence, min_score_thresh=60):
    """Plates a an array of all the plates in the database"""
    ocr_candidates = normalize_plate(ocr_text)

    best_match = None
    highest_score = 0

    for candidate in ocr_candidates:
        result = process.extractOne(
            candidate,
            plates,
            scorer=fuzz.ratio,
            processor=utils.default_process,
            score_cutoff=min_score_thresh
        )

        if result:
            match_str, score, index = result
            if score > highest_score:
                highest_score = score
                best_match = plates[index]

    composite_score = (highest_score + aws_confidence) / 2 if best_match else 0

    return {
        "matched_record": best_match,
        "match_score": highest_score,
        "aws_confidence": aws_confidence,
        "composite_score": composite_score,
        "is_reliable": composite_score > 80
    }

if __name__=="__main__":
    """quick test program"""
    plates = ['EVF895', '12345','ABCDEF','12845']

    # OCR sees "F895" (4 chars), misses the EV.
    # Logic: "F895" is length 4 -> Try "EVF895" -> Perfect Match.
    print(find_closest_plate_entry(plates, "F895", 65.0))
    print(find_closest_plate_entry(plates, "12B45", 65.0))
