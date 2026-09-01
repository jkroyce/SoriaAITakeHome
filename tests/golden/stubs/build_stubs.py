"""Build the stub model payloads the runner is developed and proven against.

WHY THIS EXISTS. ``cache/llm/`` is empty and this section is not allowed to spend a
cent, so the extractor cannot actually run. The golden fixtures in ``../cases/`` are
pure data and cost nothing to write, but a runner nobody has ever seen score anything
is not a gate. These stubs are the stand-in: hand-entered "what the model should have
said" payloads that flow through the real ``extract_document`` path, so the selectors,
the comparisons and the failure reporting are exercised for real.

WHAT THEY ARE NOT. A stub is a test double for the RUNNER. It is not evidence about
the model, and it is not the golden expectation -- the expectations live in
``../cases/*.json`` with the sentence each one came from. When ``cache/llm/`` is warm,
``runner.py`` scores real extractor output through exactly the same code with no
changes; the stubs stay behind as the runner's own unit tests.

THE BAD STUBS ARE DERIVED, NOT INVENTED. Each ``bad_rule_*`` directory is produced by
applying, mechanically, the transformation that the matching candidate rule in
``../candidate_rules/`` prescribes. So the bad-rule test asks exactly the right
question: *if an extraction behaved the way this plausible-sounding rule tells it to,
does the golden set reject it?*

Regenerate with::

    .venv/Scripts/python.exe tests/golden/stubs/build_stubs.py
"""
from __future__ import annotations

import copy
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent


def row(contractor_raw, city, state, branch, amount, *,
        action_type="new_award", contract_number=None, **kw):
    r = {
        "service_branch": branch,
        "contractor_raw": contractor_raw,
        "contractor_city": city,
        "contractor_state": state,
        "amount_usd": amount,
        "action_type": action_type,
        "contract_number": contract_number,
        "base_contract_number": None,
        "modification_number": None,
        "cumulative_face_value_usd": None,
        "pricing_type": None,
        "is_idiq": False,
        "is_multi_award": False,
        "work_description": None,
        "place_of_performance": None,
        "completion_date": None,
        "contracting_activity": None,
        "bids_solicited": None,
        "bids_received": None,
        "small_business": contractor_raw.endswith("*"),
        "extraction_confidence": 0.95,
        "extraction_notes": None,
    }
    r.update(kw)
    return r


def pool(members, amount, branch, **shared):
    """One row per company, all carrying the same shared ceiling."""
    out = []
    for name, city, state, cn in members:
        out.append(row(name, city, state, branch, amount,
                       action_type="multi_award_pool", contract_number=cn,
                       is_multi_award=True, **shared))
    return out


# --------------------------------------------------------------------------------
# 4586879 -- Contracts for Aug. 31, 2026
# --------------------------------------------------------------------------------

ARMY_POOL = [
    ("AAECON General Contracting LLC,*", "Louisville", "Kentucky", "W912QR-26-D-A044"),
    ("Amerifield LLC,*", "Berlin", "Connecticut", "W912QR-26-D-A045"),
    ("Huot Construction and Services Inc.,*", "South Saint Paul", "Minnesota", "W912QR-26-D-A046"),
    ("Red Eagle 3 JV,*", "Coweta", "Oklahoma", "W912QR-26-D-A047"),
    ("Semper Tek Inc.,*", "Lexington", "Kentucky", "W912QR-26-D-A048"),
    ("Valiant Construction LLC,*", "Louisville", "Kentucky", "W912QR-26-D-A049"),
    ("Tri Coast-Pac Tech JV 2,*", "Kelso", "Washington", "W912QR-26-D-A050"),
]

NAVFAC_POOL = [
    ("AECOM Technical Services Inc.", "Los Angeles", "California", "N39430-26-D-2011"),
    ("Aptim Federal Services LLC", "Baton Rouge", "Louisiana", "N39430-26-D-2012"),
    ("Argus Consulting Inc.,*", "Overland Park", "Kansas", "N39430-26-D-2013"),
    ("Austin Brockenbrough & Associates LLC", "Richmond", "Virginia", "N39430-26-D-2014"),
    ("Burns & McDonnell Engineering Co. Inc.", "Kansas City", "Missouri", "N39430-26-D-2015"),
    ("Enterprise Engineering Inc.,*", "Anchorage", "Alaska", "N39430-26-D-2016"),
    ("HDR Engineering Inc.", "Omaha", "Nebraska", "N39430-26-D-2017"),
    ("Pond & Co.", "Peachtree Corners", "Georgia", "N39430-26-D-2018"),
    ("Robert and Co. Inc.,*", "Atlanta", "Georgia", "N39430-26-D-2019"),
    ("Tetra Tech Inc.", "Collinsville", "Illinois", "N39430-26-D-2020"),
]

AWARDS_4586879 = [
    row("Action Manufacturing Co.,*", "Bristol", "Pennsylvania", "ARMY", 712_480_000,
        contract_number="W15QKN-26-D-A084", is_idiq=True,
        pricing_type="firm-fixed-price, indefinite-delivery/indefinite-quantity",
        bids_solicited=None, bids_received=1,
        completion_date="Aug. 31, 2031",
        contracting_activity="Army Contracting Command, Newark, New Jersey",
        work_description="Manufacture, inspect, test, package and deliver M739A1 point "
                         "detonating/delay fuzes."),
    row("South Carolina Commission for the Blind", "Columbia", "South Carolina", "ARMY",
        180_000_000, action_type="modification",
        contract_number="W9124C-25-D-A003", base_contract_number="W9124C-25-D-A003",
        modification_number="P00002", cumulative_face_value_usd=280_000_000,
        bids_solicited=1, bids_received=1, completion_date="March 3, 2029",
        contracting_activity="Army 419th Contracting Support Brigade, Fort Jackson, "
                             "South Carolina",
        work_description="Full food services for eleven Army dining facilities."),
    *pool(ARMY_POOL, 160_000_000, "ARMY",
          pricing_type="firm-fixed-price", bids_solicited=None, bids_received=19,
          completion_date="Feb. 29, 2032",
          contracting_activity="U.S. Army Corps of Engineers, Louisville, Kentucky",
          work_description="Design-bid-build construction services ordered competitively "
                           "under a shared ceiling."),
    row("Rockwell Collins Inc.", "Cedar Rapids", "Iowa", "ARMY", 38_916_896,
        contract_number="W58RGZ-26-F-A009", is_idiq=True,
        pricing_type="cost-plus-fixed-fee", cumulative_face_value_usd=55_146_864,
        bids_solicited=1, bids_received=1, completion_date="Feb. 28, 2029",
        contracting_activity="Army Contracting Command, Redstone Arsenal, Alabama",
        work_description="Command, control and communications integration and engineering "
                         "support for Common Avionics Architecture System 10.3 software."),
    row("Paligen Aerospace & Defense LLC,*", "Tampa", "Florida", "ARMY", 30_840_628,
        action_type="modification", base_contract_number="W519TC-25-F-0015",
        contract_number="W519TC-25-F-0015", modification_number="P00005",
        cumulative_face_value_usd=435_000_000, completion_date="Feb. 15, 2029",
        contracting_activity="Army Contracting Command, Rock Island, Illinois",
        work_description="Design, construction and commissioning of a trinitrotoluene "
                         "production facility."),
    row("CRB-PLS LLC", "O'Brien", "Florida", "ARMY", 19_000_000,
        contract_number="W9126G-26-D-A060", is_idiq=True,
        pricing_type="firm-fixed-price, indefinite-delivery/indefinite-quantity",
        bids_solicited=None, bids_received=1, completion_date="Aug. 30, 2031",
        contracting_activity="U.S. Army Corps of Engineers, Fort Worth, Texas",
        work_description="Real estate title services."),
    row("KBR Services LLC", "Houston", "Texas", "ARMY", 17_000_000,
        action_type="modification", base_contract_number="W52P1J-12-G-0061",
        contract_number="W52P1J-12-G-0061", modification_number="001 CB",
        cumulative_face_value_usd=165_655_369, completion_date="June 1, 2027",
        contracting_activity="Army Contracting Command, Rock Island, Illinois",
        work_description="Care of supplies in storage and maintenance of government "
                         "equipment."),
    row("Tern AI Inc.", "Austin", "Texas", "ARMY", 11_263_793,
        contract_number="W51701-26-C-A205", pricing_type="firm-fixed-price",
        bids_solicited=1, bids_received=1, completion_date="Aug. 19, 2027",
        place_of_performance="Austin, Texas",
        contracting_activity="Army FUZE Contracting Center, Arlington, Virginia",
        work_description="Independently derived positioning system and vehicle health "
                         "and maintenance system."),

    *pool(NAVFAC_POOL, 145_000_000, "NAVY",
          is_idiq=True,
          pricing_type="firm-fixed-price, indefinite-delivery/indefinite-quantity",
          bids_solicited=None, bids_received=14, completion_date="Aug. 2031",
          contracting_activity="Naval Facilities Engineering and Expeditionary Warfare "
                               "Center, Port Hueneme, California",
          work_description="Petroleum, oil and lubricants engineering and design services "
                           "for Navy and Marine Corps installations worldwide."),
    row("Environmental Chemical Corp.", "Burlingame", "California", "NAVY", 128_363_354,
        contract_number="N40085-26-C-0020", pricing_type="firm-fixed-price",
        bids_solicited=None, bids_received=4, completion_date="July 2028",
        place_of_performance="Naval Support Activity, Norfolk, Virginia",
        contracting_activity="Naval Facilities Engineering Systems Command, Mid-Atlantic, "
                             "Norfolk, Virginia",
        work_description="Construction of the NATO Joint Force Command Norfolk Facilities "
                         "Phase II Interim Modular Facility.",
        extraction_notes="Maximum dollar value including base and 13 options is "
                         "$174,443,268; not recorded as amount_usd."),
    row("General Dynamics Information Technology Inc.", "Falls Church", "Virginia", "NAVY",
        43_874_231, action_type="modification", base_contract_number="N6339423C0009",
        contract_number="N6339423C0009", pricing_type="cost-only",
        completion_date="Aug. 2028", place_of_performance="Falls Church, Virginia",
        contracting_activity="Naval Surface Warfare Center, Port Hueneme Division, "
                             "Port Hueneme, California",
        work_description="In-service engineering support of the MK 41 Vertical Launching "
                         "System."),
    row("Lockheed Martin Corp.", "Moorestown", "New Jersey", "NAVY", 26_152_726,
        contract_number="N00104-26-C-K215", pricing_type="firm-fixed-price",
        bids_solicited=1, bids_received=1, completion_date="May 2029",
        place_of_performance="Moorestown, New Jersey",
        contracting_activity="Naval Supply Systems Command Weapon Systems Support, "
                             "Mechanicsburg, Pennsylvania",
        work_description="Refurbishment of 44 5W83 VLA thrust vectors and 44 VH07 VLA "
                         "rocket motors."),
    row("KBR Wyle Services LLC", "Lexington Park", "Maryland", "NAVY", 25_162_568,
        action_type="option_exercise", base_contract_number="N0042125C1001",
        contract_number="N0042125C1001", modification_number="P00003",
        pricing_type="cost-plus-fixed-fee", completion_date="Aug. 2027",
        contracting_activity="Naval Air Systems Command, Patuxent River, Maryland",
        work_description="Continued program management, engineering, financial and "
                         "logistics support for the F/A-18.",
        extraction_confidence=0.85,
        extraction_notes="Printed as a modification (P00003) that exercises an option; "
                         "recorded as option_exercise."),
    row("Textron Systems Corp.", "Hunt Valley", "Maryland", "NAVY", 15_472_272,
        contract_number="N0001926F1170", base_contract_number="N0001926G1007",
        pricing_type="firm-fixed-price", completion_date="May 2028",
        contracting_activity="Naval Air Systems Command, Patuxent River, Maryland",
        work_description="Pre-operational and operational support for sea-based unmanned "
                         "aircraft maritime ISR services.",
        extraction_confidence=0.8,
        extraction_notes="Order placed against basic ordering agreement N0001926G1007."),
    row("The Boeing Co.", "St. Louis", "Missouri", "NAVY", 12_680_717,
        action_type="modification", base_contract_number="N0001918C1012",
        contract_number="N0001918C1012", modification_number="P00094",
        completion_date="November 2029",
        contracting_activity="Naval Air Systems Command, Patuxent River, Maryland",
        work_description="Non-recurring engineering for RF Blanker Unit and Encrypted "
                         "Mass Storage System on MQ-25.",
        extraction_confidence=0.75,
        extraction_notes="Adds scope AND exercises an option; recorded as modification."),
    row("Meggitt Defense Systems Inc.", "Irvine", "California", "NAVY", 9_373_421,
        contract_number="N0016426FL049", pricing_type="firm-fixed-price",
        completion_date="September 2028", place_of_performance="Irvine, California",
        contracting_activity="Naval Surface Warfare Center, Crane Division, Crane, Indiana",
        work_description="Liquid Air Palletized System Kits and spares for the Navy P-8A "
                         "Tactical Airborne Sensor Mission System.",
        extraction_notes="Option-inclusive cumulative value of $20,509,871 is contingent "
                         "on options and is not recorded as a cumulative face value."),

    row("Northrop Grumman Systems Corp.", "Riverdale", "Utah", "AIR FORCE", 86_069_000,
        contract_number="FA8214-26-F-B017", base_contract_number="FA8214-21-D-0002",
        completion_date="September 2031",
        contracting_activity="Air Force Nuclear Weapons Center, Hill AFB, Utah",
        work_description="Program management, maintenance and sustaining engineering for "
                         "the remote visual assessment program.",
        extraction_confidence=0.8,
        extraction_notes="Task order under parent IDIQ FA8214-21-D-0002."),
    row("Aleut Construction LLC,*", "Reston", "Virginia", "AIR FORCE", 52_525_333,
        action_type="modification", base_contract_number="FA8501-25-C-0005",
        contract_number="FA8501-25-C-0005", modification_number="P00001",
        cumulative_face_value_usd=56_116_956, completion_date="March 15, 2028",
        place_of_performance="Robins Air Force Base, Warner Robins, Georgia",
        contracting_activity="Air Force Materiel Command Operational Contracting, "
                             "Robins AFB, Warner Robins, Georgia",
        work_description="Narrow body paint booth."),
    row("CAE Inc.", "Arlington", "Texas", "AIR FORCE", 42_103_873,
        contract_number="FA862126CB003",
        pricing_type="fixed-price, cost-plus-award-fee, cost reimbursable, and "
                     "fixed-price plus incentive",
        completion_date="April 30, 2029",
        contracting_activity="Air Force Life Cycle Management Center, Wright-Patterson "
                             "Air Force Base, Ohio",
        work_description="Royal Moroccan Air Force F-16 Block 72 training system and "
                         "contractor logistics support."),

    row("Raytheon Co.", "McKinney", "Texas", "OTHER", 9_747_240,
        contract_number="H9240826FE010", base_contract_number="H9240824D4343",
        is_idiq=True, completion_date="Sept. 14, 2027",
        place_of_performance="McKinney, Texas",
        contracting_activity="U.S. Special Operations Command, MacDill Air Force Base, "
                             "Florida",
        work_description="Silent Knight Radar Antennae Gimbal-4 program.",
        extraction_confidence=0.8,
        extraction_notes="header 'U.S. SPECIAL OPERATIONS COMMAND'; task order under "
                         "parent IDIQ H9240824D4343"),

    row("Kit Masters Inc.,*", "Perham", "Minnesota", "DEFENSE LOGISTICS AGENCY", 9_235_200,
        contract_number="SPRDL1-26-C-0111", pricing_type="firm-fixed-price",
        completion_date="June 19, 2028",
        contracting_activity="Defense Logistics Agency Weapons Support, Warren, Michigan",
        work_description="Engine fan clutches.",
        extraction_notes="Using military service is Army; announced under the DLA header."),
]

# --------------------------------------------------------------------------------
# 4585867 -- Contracts for Aug. 28, 2026 (partial: the entries the cases touch)
# --------------------------------------------------------------------------------

AWARDS_4585867 = [
    row("Symetrics Industries LLC, doing business as Extant Aerospace", "Melbourne",
        "Florida", "AIR FORCE", 147_310_777, contract_number="FA8523-26-D-0001",
        is_idiq=True, bids_solicited=None, bids_received=2,
        completion_date="Aug. 27, 2032",
        contracting_activity="Air Force Life Cycle Management Center, Robins Air Force "
                             "Base, Georgia",
        work_description="ALE-47 countermeasure dispenser system production."),
    row("Telos Corp.", "Ashburn", "Virginia", "AIR FORCE", 37_777_500,
        contract_number="FA7037-26-D-0003", is_idiq=True,
        bids_solicited=None, bids_received=30, completion_date="Jan. 31, 2031",
        place_of_performance="San Antonio, Texas",
        contracting_activity="Air Combat Command, Acquisition Management and Integration "
                             "Center, Joint Base San Antonio-Lackland, Texas",
        work_description="Cybersecurity and risk management support for Air Combat "
                         "Command."),
    row("Kwaan Tech LLC", "Chantilly", "Virginia", "AIR FORCE", 29_999_937,
        contract_number="FA489026C0012", pricing_type="firm-fixed-price",
        completion_date="Aug. 31, 2029",
        place_of_performance="Tinker Air Force Base, Oklahoma",
        contracting_activity="Headquarters Air Combat Command Acquisition Management and "
                             "Integration Center, Hampton, Virginia",
        work_description="E-3 contract aircrew training and courseware development."),
    row("BK Manufacturing Co. Inc.,*", "Arab", "Alabama", "AIR FORCE", 28_000_000,
        contract_number="FA8556-26-D-B001", is_idiq=True,
        pricing_type="firm-fixed-price, requirements, indefinite-delivery/"
                     "indefinite-quantity",
        completion_date="Aug. 27, 2031", place_of_performance="Arab, Alabama",
        contracting_activity="Air Force Life Cycle Management Center, Robins Air Force "
                             "Base, Georgia",
        work_description="Wings and fins for AIM-120X and CATM-120X missiles."),
    row("Oneida Engineering Solutions LLC", "Milwaukee", "Wisconsin", "AIR FORCE",
        20_239_787, contract_number="FA8903-26-C-0007", pricing_type="firm-fixed-price",
        completion_date="Aug. 27, 2031",
        contracting_activity="772d Enterprise Sourcing Squadron, Joint Base San Antonio "
                             "Lackland, Texas",
        work_description="PFAS response action analysis and point-of-use treatment for "
                         "off-base drinking water wells."),
    row("Guidehouse Inc.", "McLean", "Virginia", "OTHER", 11_793_452,
        contract_number="HR001126FE052", pricing_type="cost-plus-fixed-fee",
        bids_solicited=None, bids_received=1, completion_date="October 2027",
        place_of_performance="Arlington, Virginia",
        contracting_activity="Defense Advanced Research Projects Agency, Arlington, "
                             "Virginia",
        work_description="Advisory, assistance and systems engineering support for Multi X "
                         "Office programs.",
        extraction_notes="header 'DEFENSE ADVANCED RESEARCH PROJECTS AGENCY'"),
]

GOOD = {
    "4586879": {
        "awards": AWARDS_4586879,
        "document_notes": "Five ALL-CAPS sections; two multi-award pools (7 Army, 10 Navy) "
                          "sharing one ceiling each; 14 asterisked small-business entries "
                          "plus the '*Small business' legend, which is a footnote.",
    },
    "4585867": {
        "awards": AWARDS_4585867,
        "document_notes": "Partial stub: the Air Force and DARPA entries the golden cases "
                          "touch. The CORRECTION line for Lockheed Martin Corp. "
                          "(FA860426DB008) is deliberately absent -- it announces no new "
                          "award.",
    },
}


# --------------------------------------------------------------------------------
# bad-rule transformations -- each one is exactly what its candidate rule prescribes
# --------------------------------------------------------------------------------

def divide_pool_ceiling(payload: dict) -> dict:
    """R-BAD-001: 'the ceiling is divided evenly among the awardees'."""
    out = copy.deepcopy(payload)
    pools: dict[tuple, int] = {}
    for a in out["awards"]:
        if a.get("is_multi_award"):
            pools[(a["amount_usd"], a["service_branch"])] = \
                pools.get((a["amount_usd"], a["service_branch"]), 0) + 1
    for a in out["awards"]:
        if a.get("is_multi_award"):
            n = pools[(a["amount_usd"], a["service_branch"])]
            a["amount_usd"] = a["amount_usd"] // n
    return out


def cumulative_as_amount(payload: dict) -> dict:
    """R-BAD-002: 'record the cumulative face value as the amount of the action'."""
    out = copy.deepcopy(payload)
    for a in out["awards"]:
        if a.get("cumulative_face_value_usd"):
            a["amount_usd"] = a["cumulative_face_value_usd"]
    return out


def collapse_pool_to_one_row(payload: dict) -> dict:
    """R-BAD-003: 'a shared-ceiling paragraph is one contract, so emit one row'."""
    out = copy.deepcopy(payload)
    kept, seen = [], set()
    for a in out["awards"]:
        if a.get("is_multi_award"):
            key = (a["amount_usd"], a["service_branch"])
            if key in seen:
                continue
            seen.add(key)
        kept.append(a)
    out["awards"] = kept
    return out


BAD_RULES = {
    "bad_rule_divide_pool": divide_pool_ceiling,
    "bad_rule_cumulative_as_amount": cumulative_as_amount,
    "bad_rule_collapse_pool": collapse_pool_to_one_row,
}


def write_all() -> None:
    good_dir = HERE / "good"
    good_dir.mkdir(parents=True, exist_ok=True)
    for aid, payload in GOOD.items():
        (good_dir / f"{aid}.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8", newline="\n")
        print(f"good/{aid}.json  {len(payload['awards'])} awards")

    for name, fn in BAD_RULES.items():
        d = HERE / name
        d.mkdir(parents=True, exist_ok=True)
        for aid, payload in GOOD.items():
            bad = fn(payload)
            (d / f"{aid}.json").write_text(
                json.dumps(bad, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8", newline="\n")
        print(f"{name}/  derived from good by {fn.__name__}()")


if __name__ == "__main__":
    write_all()
