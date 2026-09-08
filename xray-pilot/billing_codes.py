"""
Manitoba Health diagnostic radiology tariff codes.
Source: Physician's Manual, "Diagnostic Radiological Procedures" section,
pages T-1 through T-4, effective April 1, 2026 (as uploaded by the user).

Every code below was copied directly from that document. Do not add or guess
codes for exams not covered here -- extend this table only from a verified
source (Velox's own tariff export, or a newer Physician's Manual excerpt).
"""

# Base code per exam -- covers the routine/standard view count for that study
BASE_CODES = {
    "Chest": {"code": "7025", "description": "Chest, P.A. and lateral"},
    "Wrist": {"code": "7050", "description": "Wrist"},
    "Ankle": {"code": "7059", "description": "Ankle"},
    "Hand": {"code": "7051", "description": "Hand"},
    "Knee": {"code": "7056", "description": "Knee or patella"},
    "Shoulder": {"code": "7044", "description": "Shoulder, A.P. and lateral routine"},
    "Elbow": {"code": "7048", "description": "Elbow"},
    "Foot": {"code": "7060", "description": "Foot"},
    "Hip": {"code": "7053", "description": "Hip"},
    "Lumbar spine": {"code": "7193", "description": "Lumbo-sacral, routine views"},
    "Cervical spine": {"code": "7036", "description": "Cervical spine, routine views"},
    "Pelvis": {"code": "7039", "description": "Pelvis, A.P. view"},
}

# Ribs are billed by side count, not specific side -- Left and Right both use
# the "one side" code; only Bilateral gets the separate "both sides" code.
RIB_CODES = {
    "one_side": {"code": "7031", "description": "Ribs, one (1) side"},
    "both_sides": {"code": "7331", "description": "Ribs, both sides"},
}

# "Added views" add-on codes, billed on top of a base code when extra views
# beyond the routine set are taken (e.g. patellar view added to a knee).
# Which add-on code applies depends on which body-part section the exam
# falls under in the tariff schedule.
ADDED_VIEWS_UPPER_EXTREMITY = {"code": "7403", "description": "Added views, upper extremity"}
ADDED_VIEWS_LOWER_EXTREMITY = {"code": "7404", "description": "Added views, lower extremity"}
ADDED_VIEWS_CHEST = {"code": "7401", "description": "Added views, chest"}
ADDED_VIEWS_HEAD_NECK = {"code": "7400", "description": "Added views, head and neck"}

# Maps each exam to which "added views" add-on code applies when a modifier
# (extra view) is selected for it in the widget.
ADDED_VIEWS_BY_EXAM = {
    "Wrist": ADDED_VIEWS_UPPER_EXTREMITY,
    "Hand": ADDED_VIEWS_UPPER_EXTREMITY,
    "Shoulder": ADDED_VIEWS_UPPER_EXTREMITY,
    "Elbow": ADDED_VIEWS_UPPER_EXTREMITY,
    "Ankle": ADDED_VIEWS_LOWER_EXTREMITY,
    "Knee": ADDED_VIEWS_LOWER_EXTREMITY,
    "Foot": ADDED_VIEWS_LOWER_EXTREMITY,
    "Hip": ADDED_VIEWS_LOWER_EXTREMITY,
    "Chest": ADDED_VIEWS_CHEST,
}


def resolve_billing_codes(study_line):
    """
    Given one study description line (as stored in an order's `studies`
    field, e.g. "Knee (Left) - 4 view (Patellar view, Tunnel view)" or
    "Ribs (Right)"), return a list of {code, description} dicts representing
    every tariff code that applies to that line.

    This is best-effort text parsing over our own generated study strings --
    it is not a general-purpose parser for arbitrary text. If the exam name
    isn't recognized, an empty list is returned rather than a guess.
    """
    codes = []

    # Ribs are a distinct study line, not a modifier on Chest
    if study_line.startswith("Ribs ("):
        if "Bilateral" in study_line:
            codes.append(RIB_CODES["both_sides"])
        else:
            codes.append(RIB_CODES["one_side"])
        return codes

    # Find which base exam this line refers to (exam name appears at the start)
    exam_name = None
    for name in BASE_CODES:
        if study_line.startswith(name):
            exam_name = name
            break
    if not exam_name:
        return codes  # unrecognized exam -- do not guess

    codes.append(BASE_CODES[exam_name])

    # If the line shows added views in parentheses after the view count
    # (e.g. "... 4 view (Patellar view, Tunnel view)"), add the add-on code once
    if "view (" in study_line and exam_name in ADDED_VIEWS_BY_EXAM:
        codes.append(ADDED_VIEWS_BY_EXAM[exam_name])

    return codes