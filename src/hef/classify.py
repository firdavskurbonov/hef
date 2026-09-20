"""Layered SHA / SRHR classification.

Why layered rather than a model
-------------------------------
The strongest signal in this data is the country's own account code: it is
structured, assigned by the finance system, and present on every record -
including Country C's 27 records with no description at all. So the account mapping
runs first and free text is only consulted when the code cannot answer.

Each layer is weaker than the one before and records a lower confidence.
Every result carries the method, the rule id and a one-line explanation, so
an analyst can see why a record landed where it did and challenge it. A rule
table can be audited line by line; a score out of a model cannot.

Layer 1  account_mapping   country account code -> SHA + SRHR   grade 0.40-0.95
Layer 2  keyword           bilingual regex over the description  grade 0.70
Layer 3  fuzzy             rapidfuzz against SHA labels          grade 0.55
Layer 4  none              UNCLASSIFIED -> review queue          grade 0.0

Confidence is an evidence grade, not a probability. It says how strong the
kind of evidence was - the country's own account code is strongest, free
text weaker, nothing at all is a guess - and it drives routing: any result
below `settings.review_below` (0.70) goes to a human, on top of the explicit
flags for untrusted text, code/text conflicts and input-type codes.

Untrusted text
--------------
Descriptions are data, never instructions. The supplied Country B extract
carries four records with injected LLM instructions in the description field.
This classifier is deterministic so they cannot take effect, but any record
matching an injection pattern still has its text layer disabled and is routed
to human review - both because a forged "prior audit confirmed" note can
mislead a human, and because the text layer is exactly where an LLM would later
be plugged in.
"""
from __future__ import annotations

import re

from rapidfuzz import fuzz, process

from . import db
from .harmonise import fold

OUTCOME_CLASSIFIED = "CLASSIFIED"
OUTCOME_CAPITAL = "CAPITAL"
OUTCOME_ADMIN = "ADMIN_INPUT"
OUTCOME_UNCLASSIFIED = "UNCLASSIFIED"


class Classifier:
    """Rule engine for one classification run.

    Built once from the account mapping, rules.yml and the scheme labels, then
    `classify_record` is called per fact. Holds no state between records, so
    the order of records cannot affect a result.
    """

    def __init__(self, account_mapping: dict, rules: dict, sha_labels: dict, srhr_labels: dict):
        self.account_mapping = account_mapping
        self.settings = rules.get("settings", {}) or {}
        self.sha_rules = rules.get("sha_rules", []) or []
        self.srhr_rules = rules.get("srhr_rules", []) or []
        self.injection = [re.compile(p, re.IGNORECASE)
                          for p in (rules.get("injection_patterns") or [])]
        self.sha_labels = sha_labels      # code -> description
        self.srhr_labels = srhr_labels
        # folded label -> code, the candidate list for the fuzzy layer
        self._sha_choices = {fold(v): k for k, v in sha_labels.items()}

    # ------------------------------------------------------------- guards ----

    def injection_hits(self, text: str | None) -> list[str]:
        """The injection patterns from rules.yml that match, if any. Run on the
        raw description, before any normalisation."""
        if not text:
            return []
        return [p.pattern for p in self.injection if p.search(str(text))]

    # ------------------------------------------------------------- layers ----

    def _from_account_mapping(self, country_code: str, account_code: str | None) -> dict | None:
        """Layer 1: the code looked up in account_mapping.yml for this country.
        Returns {"sha": result, "srhr": result} or None when the code is unmapped.
        Confidence and needs_review come from the mapping entry itself."""
        if not account_code:
            return None
        entry = (self.account_mapping.get(country_code) or {}).get(str(account_code).strip())
        if not entry:
            return None
        return {
            "sha": {
                "code": entry.get("sha"),
                "outcome": entry.get("outcome", OUTCOME_CLASSIFIED),
                "confidence": float(entry.get("confidence", 0.9)),
                "needs_review": bool(entry.get("needs_review", False)),
                "method": "account_mapping",
                "rule_id": f"MAP:{country_code}:{account_code}",
                "explanation": (
                    f"Account {account_code} ({entry.get('label')}) is mapped to "
                    f"{entry.get('sha') or entry.get('outcome')}."
                    + (f" {entry['note']}" if entry.get("note") else "")
                ),
            },
            "srhr": {
                "code": entry.get("srhr"),
                "outcome": OUTCOME_CLASSIFIED if entry.get("srhr") else OUTCOME_UNCLASSIFIED,
                "confidence": float(entry.get("confidence", 0.9)),
                "needs_review": bool(entry.get("needs_review", False)),
                "method": "account_mapping",
                "rule_id": f"MAP:{country_code}:{account_code}",
                "explanation": f"Account {account_code} is mapped to {entry.get('srhr')}.",
            },
        }

    def _from_keywords(self, text: str, rules: list[dict], code_key: str) -> dict | None:
        """Layer 2: first rule in `rules` whose pattern matches the folded text.
        `code_key` is "sha" or "srhr" - the key the rule stores its code under."""
        folded = fold(text)
        if not folded:
            return None
        for rule in rules:
            if re.search(rule["pattern"], folded, re.IGNORECASE):
                return {
                    "code": rule.get(code_key),
                    "outcome": rule.get("outcome", OUTCOME_CLASSIFIED),
                    "confidence": float(rule.get(
                        "confidence", self.settings.get("default_confidence", 0.7))),
                    "needs_review": True,   # a text match is never accepted unseen
                    "method": "keyword",
                    "rule_id": rule["id"],
                    "explanation": (
                        f"Description matched rule {rule['id']} "
                        f"(/{rule['pattern']}/)."
                        + (f" {rule['note']}" if rule.get("note") else "")
                    ),
                }
        return None

    def _from_fuzzy(self, text: str) -> dict | None:
        """Layer 3: token-set similarity between the description and the SHA
        labels. Below `fuzzy_threshold` it answers nothing rather than guess."""
        folded = fold(text)
        if not folded or not self._sha_choices:
            return None
        threshold = float(self.settings.get("fuzzy_threshold", 88))
        match = process.extractOne(folded, list(self._sha_choices.keys()),
                                   scorer=fuzz.token_set_ratio)
        if not match or match[1] < threshold:
            return None
        label, score, _ = match
        return {
            "code": self._sha_choices[label],
            "outcome": OUTCOME_CLASSIFIED,
            "confidence": float(self.settings.get("fuzzy_confidence", 0.55)),
            "needs_review": True,
            "method": "fuzzy",
            "rule_id": f"FUZZY:{score:.0f}",
            "explanation": f"Description is {score:.0f}% similar to SHA label '{label}'.",
        }

    @staticmethod
    def _signal(res: dict | None) -> str | None:
        """What a result asserts: its code, or its outcome when there is no
        code (CAPITAL, ADMIN_INPUT). Comparing codes alone would miss the
        most important disagreement - construction booked to a drugs account."""
        if not res:
            return None
        return res.get("code") or res.get("outcome")

    def _corroborate(self, res: dict | None, text_res: dict | None) -> None:
        """Where the account mapping answered, check the text agrees. On a
        disagreement the mapping's answer stands but the record is flagged and
        the conflict is written into its explanation. Mutates `res` in place."""
        if not res or res.get("method") != "account_mapping":
            return
        mine, theirs = self._signal(res), self._signal(text_res)
        if theirs and mine and theirs != mine and theirs != OUTCOME_UNCLASSIFIED:
            res["needs_review"] = True
            res["explanation"] = (res.get("explanation") or "") + (
                f" Conflict: description suggests {theirs} "
                f"(rule {text_res['rule_id']}); account code retained.")

    # -------------------------------------------------------------- record ---

    def classify_record(self, country_code: str, account_code: str | None,
                        description: str | None) -> tuple[dict, dict, list[str]]:
        """Classify one record against both schemes.

        Order: injection check on the raw text; account mapping; keyword and
        fuzzy layers only for what the mapping left open; corroboration of the
        mapping against the text; UNCLASSIFIED for anything still unanswered;
        confidence cap for untrusted text; review routing by threshold.

        Returns (sha_result, srhr_result, injection_patterns_hit). Each result
        has code, outcome, confidence, needs_review, method, rule_id and
        explanation - the columns of the classification table.
        """
        hits = self.injection_hits(description)
        safe_text = None if hits else description

        matched = self._from_account_mapping(country_code, account_code)
        sha = dict(matched["sha"]) if matched else None
        srhr = dict(matched["srhr"]) if matched else None

        # Text layer only where the code could not answer, and only on text we
        # are willing to trust.
        if safe_text:
            if sha is None or (sha.get("code") is None and sha.get("outcome") == OUTCOME_CLASSIFIED):
                sha = self._from_keywords(safe_text, self.sha_rules, "sha") or sha
            if srhr is None or srhr.get("code") is None:
                srhr = self._from_keywords(safe_text, self.srhr_rules, "srhr") or srhr
            if sha is None:
                sha = self._from_fuzzy(safe_text)

            # Corroboration: where the code answered, ask the text anyway. If
            # they disagree - on a code, or on an outcome such as CAPITAL - the
            # code's answer is kept and the conflict is flagged for a human.
            if matched:
                self._corroborate(sha, self._from_keywords(safe_text, self.sha_rules, "sha"))
                self._corroborate(srhr, self._from_keywords(safe_text, self.srhr_rules, "srhr"))

        if sha is None:
            sha = {"code": None, "outcome": OUTCOME_UNCLASSIFIED, "confidence": 0.0,
                   "needs_review": True, "method": "none", "rule_id": None,
                   "explanation": "No account-code mapping and no rule matched the description."}
        if srhr is None:
            srhr = {"code": None, "outcome": OUTCOME_UNCLASSIFIED, "confidence": 0.0,
                    "needs_review": True, "method": "none", "rule_id": None,
                    "explanation": "No SRHR signal in the account code or description."}

        if hits:
            note = (" Free-text layer disabled: the description contains "
                    "instruction-like content and is treated as untrusted.")
            for res in (sha, srhr):
                res["needs_review"] = True
                res["confidence"] = min(res.get("confidence", 0.0), 0.40)
                res["explanation"] = (res.get("explanation") or "") + note

        # The grade is what routes: anything below the threshold goes to a
        # human, whatever produced it.
        threshold = float(self.settings.get("review_below", 0.70))
        for res in (sha, srhr):
            if res.get("confidence", 0.0) < threshold and not res.get("needs_review"):
                res["needs_review"] = True
                res["explanation"] = (res.get("explanation") or "") + (
                    f" Evidence grade {res['confidence']:.2f} is below the review "
                    f"threshold of {threshold:.2f}.")

        return sha, srhr, hits


# ------------------------------------------------------------------- driver ---

def classify_all(conn, account_mapping: dict, rules: dict, country_code: str | None = None) -> dict:
    """Classify every fact, or only one country's when country_code is given
    (an account mapping edit or a re-delivered extract touches one country)."""
    sha_labels = {r["sha_code"]: r["sha_description"]
                  for r in conn.execute("SELECT sha_code, sha_description FROM dim_sha")}
    srhr_labels = {r["srhr_code"]: r["srhr_description"]
                   for r in conn.execute("SELECT srhr_code, srhr_description FROM dim_srhr")}
    clf = Classifier(account_mapping, rules, sha_labels, srhr_labels)

    where = " WHERE f.country_code = ?" if country_code else ""
    params = (country_code,) if country_code else ()

    # Re-running supersedes previous results rather than deleting them.
    conn.execute(
        "UPDATE classification SET is_current=0 WHERE is_current=1 AND expenditure_id IN"
        " (SELECT f.expenditure_id FROM fact_expenditure f" + where + ")", params)
    # Injection findings are re-derived below; drop the stale ones for this scope.
    conn.execute(
        "DELETE FROM dq_issue WHERE rule_code='UNTRUSTED_TEXT_IN_DESCRIPTION' AND expenditure_id IN"
        " (SELECT f.expenditure_id FROM fact_expenditure f" + where + ")", params)

    rows = conn.execute(
        "SELECT f.expenditure_id, f.country_code, f.description, f.description_raw,"
        "       a.account_code"
        "  FROM fact_expenditure f"
        "  LEFT JOIN dim_account a ON a.account_sk = f.account_sk" + where, params
    ).fetchall()

    stats = {"records": 0, "sha_classified": 0, "sha_unclassified": 0,
             "capital": 0, "admin_input": 0, "needs_review": 0, "injection_flagged": 0}

    for row in rows:
        # Injection detection runs on the RAW text: normalisation must not be
        # able to hide a payload.
        raw_desc = row["description_raw"] or row["description"]
        sha, srhr, hits = clf.classify_record(
            row["country_code"], row["account_code"], raw_desc)
        stats["records"] += 1

        if hits:
            stats["injection_flagged"] += 1
            db.add_issue(
                conn, rule_code="UNTRUSTED_TEXT_IN_DESCRIPTION", severity="ERROR",
                country_code=row["country_code"], expenditure_id=row["expenditure_id"],
                field="description", raw_value=raw_desc,
                message=("Description contains instruction-like content aimed at an "
                         "automated classifier (patterns: " + "; ".join(hits[:3]) +
                         "). Text layer disabled; record quarantined for human review."),
            )

        for scheme, res in (("SHA", sha), ("SRHR", srhr)):
            conn.execute(
                "INSERT INTO classification (expenditure_id, scheme, code, outcome, method,"
                " rule_id, confidence, needs_review, review_status, classified_at, is_current,"
                " explanation) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["expenditure_id"], scheme, res.get("code"), res.get("outcome"),
                 res.get("method"), res.get("rule_id"), res.get("confidence"),
                 1 if res.get("needs_review") else 0,
                 "PENDING" if res.get("needs_review") else "NOT_REQUIRED", db.utc_now(), 1,
                 res.get("explanation")),
            )

        if sha["outcome"] == OUTCOME_CLASSIFIED and sha.get("code"):
            stats["sha_classified"] += 1
        elif sha["outcome"] == OUTCOME_CAPITAL:
            stats["capital"] += 1
        elif sha["outcome"] == OUTCOME_ADMIN:
            stats["admin_input"] += 1
        else:
            stats["sha_unclassified"] += 1
        if sha.get("needs_review") or srhr.get("needs_review"):
            stats["needs_review"] += 1

    conn.commit()
    return stats
