#!/usr/bin/env python3
"""Check the exact staged Git snapshot before publishing this public project.

Only source, documentation and the sanitized public course catalogue belong in
Git. Reports, browser profiles and customer data never do. Findings contain only
file names, line numbers and categories; sensitive values are never printed.
This is a guardrail, not a replacement for reviewing every staged change.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

TOP_LEVEL = frozenset({
    ".gitignore", ".gitattributes", ".editorconfig", ".pre-commit-config.yaml",
    "README.md", "README.en.md", "LICENSE", "NOTICE", "SECURITY.md",
    "CONTRIBUTING.md", "AGENTS.md", "pyproject.toml", "asetukset.toml",
    "kaynnista.sh", "uv.lock", "requirements.txt", "requirements-dev.txt",
})
PUBLIC_COURSE_FIELDS = frozenset({
    "reservation_event_group_id", "name", "is_course", "service_id", "location", "location_id",
    "location_address", "reservation_group_id", "reservation_group_name", "start", "end",
    "capacity", "free", "queue_capacity", "queue", "free_queue", "is_reservable",
    "public_reservation_start", "weekdays", "event_number", "fare_products", "price", "bundle_price",
})
PUBLIC_PRODUCT_FIELDS = frozenset({"fare_product_id", "name", "price", "bundle_price", "original_price"})
PUBLIC_WEEKDAY_FIELDS = frozenset({"days", "times", "weekdays"})
RUNTIME_COMPONENTS = frozenset({
    ".state", ".venv", "venv", "__pycache__", "build", "dist", "testiraportit",
    "reports", "artifacts", "test-results", "playwright-report", "profiles", "profile",
    "browser-data", "secrets", ".aws", ".ssh", ".codex", ".agents",
})
PRIVATE_SUFFIXES = frozenset({
    ".log", ".sqlite", ".sqlite3", ".db", ".har", ".trace", ".zip", ".gz",
    ".png", ".jpg", ".jpeg", ".webp", ".mp4", ".pem", ".key", ".p12", ".pfx",
})
EMAIL = re.compile(r"(?<![\w.])[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})(?![\w.])", re.IGNORECASE)
SSN = re.compile(r"(?<!\d)(\d{2})(\d{2})(\d{2})[+\-ABCDEFUVWXY]\d{3}[0-9A-Y](?!\w)", re.IGNORECASE)
SIGNATURES = (
    ("private key", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})\b")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("JWT token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}", re.IGNORECASE)),
    ("Finnish phone number", re.compile(r"\+358[\s().-]*(?:\d[\s().-]*){6,12}(?!\d)")),
    ("Finnish bank account", re.compile(r"\bFI(?:\s*\d){16}\b")),
    ("cookie header", re.compile(r"(?:^|[\"'])Cookie:\s*[A-Za-z0-9_-]+=[^\s\"']{6,}", re.IGNORECASE)),
)
SECRET_ASSIGNMENT = re.compile(
    r"[\"']?(?:password|passwd|access_token|refresh_token|api_key|apikey|client_secret|secret_key)"
    r"[\"']?\s*[:=]\s*[\"']([^\"'\r\n]{8,})[\"']", re.IGNORECASE,
)
CARD_NUMBER = re.compile(r"(?<![\w])(?:\d[ -]?){12,18}\d(?![\w])")
TEMPLATE_IDS = frozenset({
    "template_eventgroup", "template__eventgroup_details", "template__eventgroup_buy",
    "template__participant_select", "template__eventgroup_reservations", "template_cart",
    "template__cartitem", "template_cartwidget",
})
TEMPLATE_CLASSES = frozenset({
    "agreement_notice__wrapper", "alerts", "btn", "btn-block", "btn-success", "buy", "btn-warning", "queue",
    "btn-danger", "btn-mini", "remove", "btn-primary", "btn-small", "ok", "btn-tiny", "buyCart", "continueShopping",
    "closeButton", "btn-group", "close", "control-group", "control-label", "controls", "dl-horizontal", "pull-right",
    "priceItems", "eventgroup", "eventgroup-body", "eventgroup-labels", "form", "icon-calendar", "icon-home", "icon-money",
    "icon-shopping-cart", "icon-time", "linked-participants", "margin-box", "modal-body", "cart-modal", "modal-footer",
    "modal-header", "price", "priceDiv", "priceContainer", "radio", "table", "table-striped", "texts", "title",
})
TEMPLATE_SLOTS = frozenset({
    "adult-name", "availability", "balance-label", "balance-section", "child-label", "child-name", "child-row", "child-rows",
    "count", "dates", "details", "empty-message", "extra-information-heading", "location", "net", "net-label", "participant",
    "participant-heading", "participants", "payment-button", "payment-methods", "price", "price-heading", "product-heading",
    "reservation-row", "reservations", "schedule", "status", "status-heading", "terms-label", "title", "total", "total-label",
    "vat", "vat-heading", "vat-label",
})


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    category: str


class _InertTemplates(HTMLParser):
    """Reject captured values or active content in the reviewed DOM fixture."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.templates: list[str] = []
        self.provenance = False

    def handle_starttag(self, tag, attrs):
        tags = {"template", "a", "b", "br", "button", "dd", "div", "dl", "dt", "fieldset", "form",
                "h3", "h4", "i", "input", "label", "legend", "p", "span", "table", "tbody", "td", "th", "thead", "tr"}
        if tag not in tags or len(attrs) != len(dict(attrs)):
            raise ValueError
        attributes, attrs = attrs, dict(attrs)
        if tag == "template":
            if self.stack or set(attrs) != {"id"} or attrs["id"] not in TEMPLATE_IDS:
                raise ValueError
            self.templates.append(attrs["id"])
        elif not self.stack:
            raise ValueError
        ids = TEMPLATE_IDS | {"cart", "cart-aria-live", "cartItems", "cart_texts", "extraInformation",
                              "linked-accounts", "linkedAccount", "usageTermsCheckbox", "useBalanceCheckbox"}
        values = {"id": ids, "data-slot": TEMPLATE_SLOTS, "tabindex": {"0"}, "type": {"button", "checkbox", "radio"},
                  "aria-hidden": {"true"}, "role": {"alert", "heading", "toolbar"}, "href": {"#"},
                  "for": {"inputParticipant"}, "data-name": {""}, "name": {"participant"}, "value": {""}, "aria-level": {"2"}}
        for name, value in attributes:
            if name == "class":
                if not value or not set(value.split()).issubset(TEMPLATE_CLASSES):
                    raise ValueError
            elif name not in values or value not in values[name]:
                raise ValueError
        if tag not in {"br", "input"}:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            raise ValueError

    def handle_data(self, data):
        if data.strip():
            raise ValueError

    def handle_comment(self, data):
        prefix = " Generated by scripts/extract_shop_templates.py\n"
        if self.provenance or self.stack or not data.startswith(prefix):
            raise ValueError
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result

        metadata = json.loads(data[len(prefix):], object_pairs_hook=unique_object)
        if (not isinstance(metadata, dict) or set(metadata) != {"url", "page_sha256", "templates"}
                or metadata["url"] != "https://kauppa.urheiluhallit.fi/webshop/reservations2/"
                or not isinstance(metadata["templates"], dict) or set(metadata["templates"]) != TEMPLATE_IDS):
            raise ValueError
        for digest in [metadata["page_sha256"], *metadata["templates"].values()]:
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError
        self.provenance = True

    def handle_decl(self, decl):
        raise ValueError

    def unknown_decl(self, data):
        raise ValueError

    def handle_pi(self, data):
        raise ValueError


def _template_findings(path: str, content: str) -> list[Finding]:
    parser = _InertTemplates()
    try:
        parser.feed(content)
        parser.close()
        if parser.stack or not parser.provenance or len(parser.templates) != len(TEMPLATE_IDS) or set(parser.templates) != TEMPLATE_IDS:
            raise ValueError
    except (ValueError, TypeError, KeyError):
        return [Finding(path, parser.getpos()[0], "template contains unreviewed structure or captured values")]
    return []


def allowed_path(path: str) -> bool:
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or ".." in candidate.parts or "\\" in path:
        return False
    if any(part in RUNTIME_COMPONENTS or part.endswith(".egg-info") for part in candidate.parts):
        return False
    if candidate.suffix.lower() in PRIVATE_SUFFIXES or candidate.name.startswith(".env"):
        return False
    if path in TOP_LEVEL or path == ".githooks/pre-commit":
        return True
    parts = candidate.parts
    if len(parts) == 2 and parts[0] == "uhkone" and candidate.suffix == ".py":
        return True
    if path in {"uhkone/testshop.html", "uhkone/testshop.js", "uhkone/testshop.css", "uhkone/testshop_templates.html",
                "uhkone/data/courses.json", "docs/esimerkkiajo.html"}:
        return True
    if len(parts) == 2 and parts[0] == "tests":
        return candidate.suffix == ".py" and (candidate.name.startswith("test_") or candidate.name in {"__init__.py", "conftest.py"})
    if len(parts) == 2 and parts[0] == "scripts" and candidate.suffix == ".py":
        return True
    if len(parts) == 2 and parts[0] == "docs" and candidate.suffix == ".md":
        return True
    if path in {
        "docs/superpowers/plans/2026-11-15-reserve-without-payment.md",
        "docs/superpowers/specs/2026-11-15-reserve-without-payment-design.md",
        "docs/superpowers/plans/2026-11-22-correctness-audit.md",
        "docs/superpowers/specs/2026-11-22-correctness-audit-design.md",
        "docs/superpowers/plans/2026-11-22-production-html.md",
        "docs/superpowers/specs/2026-11-22-production-html-design.md",
    }:
        return True
    if path == ".github/dependabot.yml":
        return True
    return len(parts) == 3 and parts[:2] == (".github", "workflows") and candidate.suffix in {".yml", ".yaml"}


def _example_domain(domain: str) -> bool:
    domain = domain.lower()
    return any(domain == suffix or domain.endswith("." + suffix) for suffix in (
        "example.com", "example.net", "example.org", "invalid", "test", "localhost", "example",
    ))


def _realistic_ssn(match: re.Match[str]) -> bool:
    day, month, year = (int(value) for value in match.groups())
    try:
        date(2000 + year, month, day)
        return True
    except ValueError:
        return False


def _luhn_card(value: str) -> bool:
    digits = [int(char) for char in value if char.isdigit()]
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    total = 0
    for offset, digit in enumerate(reversed(digits)):
        if offset % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _seed_findings(path: str, content: str) -> list[Finding]:
    try:
        document = json.loads(content)
        if not isinstance(document, dict) or set(document) != {
            "version", "source_url", "fetched_at", "data_sha256", "courses",
        }:
            raise ValueError
        if document["source_url"] != "https://kauppa.urheiluhallit.fi/webshop/reservations2/apieventgroups?service_id=9":
            raise ValueError
        courses = document["courses"]
        if not isinstance(courses, list) or not courses:
            raise ValueError
        for course in courses:
            if not isinstance(course, dict) or not set(course).issubset(PUBLIC_COURSE_FIELDS):
                raise ValueError
            for key, value in course.items():
                if key not in {"fare_products", "weekdays"} and not isinstance(value, (str, int, float, bool, type(None))):
                    raise ValueError
            for product in course.get("fare_products", []):
                if not isinstance(product, dict) or not set(product).issubset(PUBLIC_PRODUCT_FIELDS):
                    raise ValueError
                if any(not isinstance(value, (str, int, float, bool, type(None))) for value in product.values()):
                    raise ValueError
            for weekday in course.get("weekdays", []):
                if not isinstance(weekday, dict) or not set(weekday).issubset(PUBLIC_WEEKDAY_FIELDS):
                    raise ValueError
                if any(not isinstance(value, str) for key, value in weekday.items() if key != "weekdays"):
                    raise ValueError
                if "weekdays" in weekday and (not isinstance(weekday["weekdays"], list)
                        or any(type(day) is not int for day in weekday["weekdays"])):
                    raise ValueError
    except (ValueError, TypeError, KeyError):
        return [Finding(path, 1, "catalogue contains fields outside the public course allowlist")]
    return []


def check_blob(path: str, data: bytes) -> list[Finding]:
    if not allowed_path(path):
        return [Finding(path, 1, "file is outside the public source allowlist")]
    if len(data) > 4 * 1024 * 1024 or b"\x00" in data:
        return [Finding(path, 1, "binary or unexpectedly large file")]
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        return [Finding(path, 1, "file is not UTF-8 text")]
    findings = _seed_findings(path, content) if path == "uhkone/data/courses.json" else []
    if path == "uhkone/testshop_templates.html":
        findings.extend(_template_findings(path, content))
    for number, line in enumerate(content.splitlines(), 1):
        for category, pattern in SIGNATURES:
            if pattern.search(line):
                findings.append(Finding(path, number, category))
        if any(not _example_domain(match.group(1)) for match in EMAIL.finditer(line)):
            findings.append(Finding(path, number, "non-example email address"))
        if path != "uv.lock" and any(_realistic_ssn(match) for match in SSN.finditer(line)):
            findings.append(Finding(path, number, "possible Finnish personal identity code"))
        if any(_luhn_card(match.group()) for match in CARD_NUMBER.finditer(line)):
            findings.append(Finding(path, number, "possible payment card number"))
        for match in SECRET_ASSIGNMENT.finditer(line):
            value = match.group(1).lower()
            test_sentinel = path.startswith("tests/test_") and value in {"secret token", "do not output"}
            if not test_sentinel and not value.startswith(("example", "dummy", "placeholder", "your_", "<", "test_")):
                findings.append(Finding(path, number, "non-placeholder credential assignment"))
    return findings


def staged_blobs(root: Path) -> list[tuple[str, bytes]]:
    listing = subprocess.run(
        ["git", "ls-files", "--stage", "-z"], cwd=root, check=True, stdout=subprocess.PIPE,
    ).stdout
    output = []
    for entry in listing.split(b"\x00"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, object_id, stage = metadata.split()
        path = raw_path.decode("utf-8")
        if mode not in {b"100644", b"100755"} or stage != b"0":
            raise ValueError(f"{path}: unsupported staged file mode or merge conflict")
        data = subprocess.run(
            ["git", "cat-file", "blob", object_id.decode("ascii")], cwd=root,
            check=True, stdout=subprocess.PIPE,
        ).stdout
        output.append((path, data))
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Git working tree (default: current directory)")
    args = parser.parse_args(argv)
    try:
        blobs = staged_blobs(args.root)
        if not blobs:
            raise ValueError("No staged files to check.")
        findings = [finding for path, data in blobs for finding in check_blob(path, data)]
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        # Git stderr may include paths, but blob contents are never sent to Git.
        print(f"Public-tree check could not run: {exc}", file=sys.stderr)
        return 2
    if findings:
        for finding in findings:
            print(f"{finding.path}:{finding.line}: {finding.category}", file=sys.stderr)
        print("Public-tree check failed. Remove private data from the index before committing.", file=sys.stderr)
        return 1
    print(f"Public-tree check passed: {len(blobs)} staged source files. Review staged changes before committing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
