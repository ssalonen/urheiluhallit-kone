"""Extract inert simulator templates from saved anonymous shop HTML."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from hashlib import sha256
from html import escape
from html.parser import HTMLParser
from pathlib import Path

SOURCE_URL = "https://kauppa.urheiluhallit.fi/webshop/reservations2/"
_EXPRESSION = re.compile(r"<%([=~\-]?)[\s\S]*?%>")
_EXTRA_INFO = re.compile(r"""<%=\s*Enkora\.tt\((['"])Extra information\1\)\s*%>""")
_MARKER = "data-extract-slot"
_VALUE = "EXTRACT_TEMPLATE_VALUE"
_INCLUDES = {
    "template__eventgroup_details": ("div", "details"),
    "template__eventgroup_labels": ("span", "availability"),
    "template__eventgroup_reservations": ("div", "reservations"),
    "template__participant_select": ("div", "participants"),
}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_FORBIDDEN = {"applet", "base", "embed", "frame", "frameset", "iframe", "link", "math", "meta",
              "object", "script", "style", "svg", "template"}
_SAFE_TAGS = {"a", "article", "b", "br", "button", "dd", "div", "dl", "dt", "fieldset", "form", "h3", "h4",
              "header", "i", "input", "label", "legend", "p", "section", "span", "strong", "table",
              "tbody", "td", "th", "thead", "tr"}
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*")

class ExtractionError(ValueError):
    """The source cannot establish the reviewed template structure."""


@dataclass(eq=False)
class Element:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Element] = field(default_factory=list)

    def find(self, tag: str = "", *, cls: str = "", **attributes: str) -> list[Element]:
        result = []
        for node in self.children:
            if ((not tag or node.tag == tag) and (not cls or cls in node.attrs.get("class", "").split())
                    and all(node.attrs.get(key) == value for key, value in attributes.items())):
                result.append(node)
            result.extend(node.find(tag, cls=cls, **attributes))
        return result

    def one(self, tag: str = "", *, cls: str = "", **attributes: str) -> Element:
        matches = self.find(tag, cls=cls, **attributes)
        if len(matches) != 1:
            raise ExtractionError(f"Lähteessä on {len(matches)} vastinetta rakenteelle {(tag, cls, attributes)}.")
        return matches[0]

    def slot(self, name: str) -> Element:
        self.attrs["data-slot"] = name
        return self

    def append_slot(self, name: str) -> None:
        self.children.append(Element("span").slot(name))

    def slot_after(self, node: Element, name: str) -> None:
        parent, = [element for element in (self, *self.find()) if node in element.children]
        parent.children.insert(parent.children.index(node) + 1, Element("span").slot(name))


class _PublicPage(HTMLParser):
    """Capture only named upstream template bodies, never the page runtime."""

    def __init__(self, source: str) -> None:
        super().__init__()
        self.templates: dict[str, str] = {}
        self.cart: Element | None = None
        self._active: str | None = None
        self.feed(source)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "div" and values.get("id") == "cart":
            if self.cart is not None:
                raise ExtractionError("Lähteessä on useita ostoskoripaneeleja.")
            self.cart = Element(tag, _safe_attrs(tag, values))
        if tag == "script" and values.get("type") == "text/html" and (name := values.get("id")) in _PROJECTORS:
            assert name is not None
            if name in self.templates or values.get("src"):
                raise ExtractionError(f"Lähdemallin {name} määritelmä ei ole yksiselitteinen.")
            self.templates[name] = ""
            self._active = name

    def handle_data(self, data: str) -> None:
        if self._active is not None:
            self.templates[self._active] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._active = None


def _safe_attrs(tag: str, attrs: dict[str, str | None]) -> dict[str, str]:
    result = {}
    for name in ("id", "role", "name", "for"):
        value = attrs.get(name)
        if value and _SAFE_TOKEN.fullmatch(value) and _VALUE not in value:
            result[name] = value
    classes = " ".join(value for value in (attrs.get("class") or "").split()
                       if _SAFE_TOKEN.fullmatch(value) and _VALUE not in value)
    if classes:
        result["class"] = classes
    for name, options in {
        "tabindex": {"0", "-1"}, "aria-hidden": {"true", "false"}, "aria-level": {"1", "2", "3"},
        "type": {"radio", "checkbox", "button"}, "href": {"#"},
    }.items():
        value = attrs.get(name)
        if value is not None and value in options:
            result[name] = value
    if tag == "button":
        result["type"] = "button"
    return result


def _mask_expression(match: re.Match[str]) -> str:
    if not match[1]:
        return ""
    expression = match[0]
    if _EXTRA_INFO.fullmatch(expression):
        return f'<span {_MARKER}="extra-information-heading"></span>'
    for name, (tag, slot) in _INCLUDES.items():
        if f'"#{name}"' in expression or f"'#{name}'" in expression:
            return f'<{tag} {_MARKER}="{slot}"></{tag}>'
    return _VALUE


class _TemplateMarkup(HTMLParser):
    def __init__(self, source: str, *, tolerate_cart_closing_tag: bool = False) -> None:
        super().__init__()
        self.root = Element("")
        self._stack = [self.root]
        self._tolerate_cart_closing_tag = tolerate_cart_closing_tag
        if _MARKER in source.lower() or _VALUE in source:
            raise ExtractionError("Lähde sisältää poimijalle varatun merkinnän.")
        self.feed(_EXPRESSION.sub(_mask_expression, source))
        self.close()
        if len(self._stack) != 1:
            raise ExtractionError("Lähdemallin HTML-rakenne jäi auki.")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _FORBIDDEN:
            raise ExtractionError(f"Lähdemallissa on kielletty elementti {tag}.")
        values = dict(attrs)
        child = Element(tag, _safe_attrs(tag, values))
        if slot := values.get(_MARKER):
            child.slot(slot)
        self._stack[-1].children.append(child)
        if tag not in _VOID:
            self._stack.append(child)

    def handle_endtag(self, tag: str) -> None:
        # The published cart contains one orphan closing div after the body.
        # The browser's fragment parser ignores it.
        if (self._tolerate_cart_closing_tag and len(self._stack) == 1 and tag == "div"
                and self.root.children and "cart-modal" in self.root.children[-1].attrs.get("class", "").split()):
            self._tolerate_cart_closing_tag = False
            return
        if len(self._stack) == 1 or tag != self._stack[-1].tag:
            raise ExtractionError(f"Lähdemallin HTML-rakenne ei täsmää kohdassa {tag}.")
        self._stack.pop()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _VOID:
            raise ExtractionError(f"Lähdemallin {tag}-elementti sulkeutuu epäselvästi.")
        self.handle_starttag(tag, attrs)


def _project(root: Element, *leaves: Element) -> None:
    """Keep the reviewed elements and all their original ancestors."""
    keep = set(leaves)

    def preserve(node: Element) -> bool:
        node.children = [child for child in node.children if preserve(child)]
        return bool(node.children or node in keep or "data-slot" in node.attrs)

    preserve(root)


def _alternatives(elements: list[Element], count: int = 2) -> Element:
    if len(elements) not in {1, count}:
        raise ExtractionError("Lähdemallin vaihtoehtoinen haara muuttui.")
    return elements[0]


def _card(root: Element) -> list[Element]:
    title = _alternatives(root.find("h4", cls="title")).slot("title")
    body = root.one(cls="eventgroup-body")
    price = body.one(cls="priceContainer")
    details = body.one(**{"data-slot": "details"})
    reservations = body.one(**{"data-slot": "reservations"})
    availability = price.one(**{"data-slot": "availability"})
    _project(root, title, details, reservations, availability, price.one(cls="price").slot("price"),
             price.one("button", cls="buy"), price.one("button", cls="queue"))
    return [Element("div", {"class": "eventgroup"}, root.children)]


def _details(root: Element) -> list[Element]:
    info = root.one("p")
    location = info.one("i", cls="icon-home")
    dates = info.one("i", cls="icon-calendar")
    schedule = _alternatives(info.find("i", cls="icon-time"), count=3)
    ordered = info.find()
    breaks = []
    for previous, following in ((location, dates), (dates, schedule)):
        candidates = [node for node in ordered[ordered.index(previous) + 1:ordered.index(following)]
                      if node.tag == "br"]
        if not candidates:
            raise ExtractionError("Kurssitietojen rivinvaihto puuttuu.")
        breaks.append(candidates[-1])
    _project(root, location, dates, schedule, *breaks)
    for icon, slot in ((location, "location"), (dates, "dates"), (schedule, "schedule")):
        info.slot_after(icon, slot)
    return root.children


def _participant_modal(root: Element) -> list[Element]:
    header = root.one(cls="modal-header")
    body = root.one(cls="modal-body")
    footer = root.one(cls="modal-footer")
    _project(root, header.one("h3").slot("title"), header.one("button", cls="close"),
             body.one("div", cls="alerts", role="alert"),
             body.one(cls="price").slot("price"), body.one(**{"data-slot": "availability"}),
             body.one(**{"data-slot": "details"}),
             body.one(**{"data-slot": "participants"}), footer.one("button", cls="closeButton"), footer.one("a", cls="ok"))
    return root.children


def _participants(root: Element) -> list[Element]:
    form = root.one("form", id="extraInformation")
    children = form.one("div", id="linkedAccount").slot("child-rows")
    child = children.one("input", type="radio", name="participant")
    adult, = [node for node in form.find("input", type="radio", name="participant") if node is not child]
    linked = form.one("input", id="linked-accounts", type="radio")
    if "name" in linked.attrs:
        raise ExtractionError("Lapsilistan avauskenttä ei saa kuulua osallistujavalintaan.")
    adult_label, = [node for node in form.find("label") if adult in node.find("input")]
    linked_label, = [node for node in form.find("label") if linked in node.find("input")]
    fieldset, = [node for node in form.find("fieldset") if adult in node.find("input")]
    fieldset.one("legend").one("label", cls="control-label").slot("participant-heading")
    extra, = [node for node in form.find("h4") if node.find(**{"data-slot": "extra-information-heading"})]
    extra.children.clear()
    extra.slot("extra-information-heading")
    child_label = children.one("label", cls="radio")
    child_row, = [node for node in children.children if node is child_label or child_label in node.find("label")]
    child_row.slot("child-row")
    adult_label.append_slot("adult-name")
    linked_label.append_slot("child-label")
    child_label.append_slot("child-name")
    for node in (adult, child):
        node.attrs.update({"value": "", "data-name": ""})
    _project(root, adult, linked, child)
    return [form]


def _reservations(root: Element) -> list[Element]:
    table = root.one("table")
    headings = table.one("thead").find("th")
    row = table.one("tbody").one("tr").slot("reservation-row")
    cells = row.find("td")
    statuses = [cell for cell in cells if cell.find("b")]
    status = _alternatives(statuses)
    if len(headings) < 2 or len(cells) <= len(statuses) or cells[:len(statuses)] != statuses:
        raise ExtractionError("Varaustaulukon tila- tai osallistujasarake muuttui.")
    _project(root, headings[0].slot("status-heading"), headings[1].slot("participant-heading"),
             status.one("b").slot("status"), cells[len(statuses)].slot("participant"))
    return [table]


def _cart(root: Element) -> list[Element]:
    header = root.one(cls="modal-header")
    body = root.one(cls="cart-modal")
    footer = root.one(cls="modal-footer")
    empty, = [node for node in body.children if node.tag == "p"]
    empty.slot("empty-message")
    table = body.one("table", id="cartItems")
    summary = body.one("dl", cls="priceItems")
    balance = body.one("input", id="useBalanceCheckbox", type="checkbox")
    balance_sections = [node for node in body.find(cls="margin-box") if balance in node.find("input")]
    if len(balance_sections) != 1:
        raise ExtractionError("Ostoskorin saldo-osio muuttui.")
    balance_section = balance_sections[0].slot("balance-section")
    balance_section.attrs["class"] = " ".join(cls for cls in balance_section.attrs["class"].split()
                                              if cls != "loadBalanceFPInTheCart")
    methods = footer.one(cls="btn-group", role="toolbar").slot("payment-methods")
    methods.one("button", cls="buyCart").slot("payment-button")
    terms = body.one("input", id="usageTermsCheckbox", type="checkbox")
    headings = table.one("thead").find("th")
    labels, amounts = summary.find("dt"), summary.find("dd")
    if (len(headings), len(labels), len(amounts)) != (4, 3, 3):
        raise ExtractionError("Ostoskorin otsikot tai hintayhteenveto muuttuivat.")
    for slot, label, amount in zip(("net", "vat", "total"), labels, amounts, strict=True):
        label.slot(f"{slot}-label")
        amount.slot(slot)
    for slot, heading in zip(("product-heading", "vat-heading", "price-heading"), headings):
        heading.slot(slot)
    for checkbox, slot in ((balance, "balance-label"), (terms, "terms-label")):
        label, = [node for node in body.find("label") if checkbox in node.find("input")]
        label.append_slot(slot)
    _project(root, header.one("h3").slot("title"), header.one("button", cls="close"), *headings,
             body.one("div", cls="alerts", role="alert"),
             table.one("tbody"), balance, terms, footer.one("button", cls="continueShopping"), methods)
    return root.children


def _cart_item(root: Element) -> list[Element]:
    cells = root.find("td")
    if len(cells) != 4:
        raise ExtractionError("Ostoskoririvin sarakkeet muuttuivat.")
    titles = [node for node in cells[0].children if node.tag == "div"]
    if not titles:
        raise ExtractionError("Ostoskoririvin tuotteen nimi puuttuu.")
    titles[0].slot("title")
    cells[1].slot("vat")
    cells[2].slot("price")
    _project(root, cells[3].one("button", cls="remove"))
    return [Element("tr", children=root.children)]


def _cart_widget(root: Element) -> list[Element]:
    text = root.one("td", id="cart_texts")
    count, price = text.one(cls="icon-shopping-cart"), text.one(cls="icon-money")
    button = root.one("button", cls="buy")
    _project(root, count, price, button, root.one("div", id="cart-aria-live"))
    for icon, slot in ((count, "count"), (price, "total")):
        text.slot_after(icon, slot)
    return root.children


_PROJECTORS = {
    "template_eventgroup": _card,
    "template__eventgroup_details": _details,
    "template__eventgroup_buy": _participant_modal,
    "template__participant_select": _participants,
    "template__eventgroup_reservations": _reservations,
    "template_cart": _cart,
    "template__cartitem": _cart_item,
    "template_cartwidget": _cart_widget,
}


def _serialize(element: Element, depth: int = 1) -> str:
    if element.tag not in _SAFE_TAGS:
        raise ExtractionError(f"Lähde sisältää tarkistamattoman elementin {element.tag}.")
    attrs = "".join(f' {name}="{escape(value, quote=True)}"' for name, value in sorted(element.attrs.items()))
    opening = "  " * depth + f"<{element.tag}{attrs}>"
    if element.tag in _VOID:
        return opening
    closing = f"</{element.tag}>"
    if not element.children:
        return opening + closing
    children = "\n".join(_serialize(child, depth + 1) for child in element.children)
    return f"{opening}\n{children}\n{'  ' * depth}{closing}"


def extract_templates(raw: bytes) -> str:
    page = _PublicPage(raw.decode("utf-8"))
    missing = _PROJECTORS.keys() - page.templates.keys()
    if missing or page.cart is None:
        names = ", ".join(sorted(missing | ({"#cart"} if page.cart is None else set())))
        raise ExtractionError(f"Julkiselta sivulta puuttuu lähderakenne {names}.")
    blocks = []
    for name, projector in _PROJECTORS.items():
        try:
            markup = _TemplateMarkup(page.templates[name], tolerate_cart_closing_tag=name == "template_cart")
            nodes = projector(markup.root)
            if name == "template_cartwidget":
                page.cart.children = nodes
                nodes = [page.cart]
            rendered = "\n".join(_serialize(node) for node in nodes)
        except ValueError as exc:
            raise ExtractionError(f"{name}: {exc}") from exc
        blocks.append(f'<template id="{name}">\n{rendered}\n</template>')
    provenance = {
        "url": SOURCE_URL,
        "page_sha256": sha256(raw).hexdigest(),
        "templates": {name: sha256(body.encode("utf-8")).hexdigest() for name, body in page.templates.items()},
    }
    header = "<!-- Generated by scripts/extract_shop_templates.py\n" + json.dumps(provenance, indent=2) + "\n-->"
    return "\n\n".join([header, *blocks]) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Tallennettu anonyymi julkinen HTML-sivu")
    parser.add_argument("--output", type=Path, required=True, help="Poimittujen HTML-mallien tiedosto")
    args = parser.parse_args()
    try:
        result = extract_templates(args.source.read_bytes())
        args.output.write_text(result, encoding="utf-8")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"HTML-mallien poiminta epäonnistui. {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
