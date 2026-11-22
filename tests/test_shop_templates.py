"""Regression tests for offline extraction from independent anonymous HTML."""

from hashlib import sha256

import pytest

from scripts.extract_shop_templates import ExtractionError, extract_templates


def source_page(**replacements: str) -> bytes:
    templates = {
        "template_eventgroup": """
            <header class="source-title-band"><h4 class="title"><%=model.get('name')%></h4></header>
            <div class="eventgroup-body">
                <%~_.template(jQuery("#template__eventgroup_details").html(), data)%>
                <%~_.template(jQuery("#template__eventgroup_reservations").html(), data)%>
                <div class="priceDiv priceContainer">
                    <p><%~_.template(jQuery("#template__eventgroup_labels").html(), data)%></p>
                    <p class="price"><%=model.price%></p>
                    <form><button class="buy">Buy</button><button class="queue">Queue</button></form>
                </div>
            </div>
        """,
        "template__eventgroup_details": """
            <p tabindex="0"><i class="icon-home"></i><%-model.attributes.location%>
                <br class="location-break"><i class="icon-calendar"></i><%=date_start%>
                <br class="schedule-break">
                <% if (once) { %><i class="icon-time"></i><%=time[0]%>
                <% } else if (weekday) { %><i class="icon-time"></i><%=otherTime[0]%>
                <% } else { %><i class="icon-time"></i><%=variedTime[0]%><% } %>
            </p>
        """,
        "template__eventgroup_buy": """
            <div class="modal-header"><button class="close">Close</button><h3><%=model.name%></h3></div>
            <div class="modal-body">
                <div class="alerts source-buy-alert" role="alert"></div>
                <div class="priceDiv">
                    <p class="eventgroup-labels"><%~_.template(jQuery("#template__eventgroup_labels").html(), data)%></p>
                    <p class="price"></p>
                </div>
                <%~_.template(jQuery("#template__eventgroup_details").html(), data)%>
                <%~_.template(jQuery("#template__participant_select").html(), data)%>
            </div>
            <div class="modal-footer"><button class="closeButton">Cancel</button><a class="ok" href="#">OK</a></div>
        """,
        "template__participant_select": """
            <form id="extraInformation" onsubmit="return false;">
                <h4><%=Enkora.tt('Extra information')%></h4>
                <fieldset><legend><label class="control-label" for="inputParticipant">Participant</label></legend>
                <label class="radio">
                    <input type="radio" name="participant" value="<%=eg.account_id%>" data-name="<%=eg.name%>">
                    <%=eg.name%>
                </label></fieldset>
                <label><input id="linked-accounts" type="radio" /></label>
                <div id="linkedAccount" style="display:none">
                    <div class="source-child-wrapper"><label class="radio">
                        <input name="participant" type="radio" data-name="Real name" value="1234567">Real name
                    </label></div>
                    <div id="childAccountForm"><button class="addAccount">Create child</button></div>
                </div>
            </form>
        """,
        "template__eventgroup_reservations": """
            <table><thead><tr><th>Status</th><th>Participant</th></tr></thead>
                <tbody><tr><td><b><%=status%></b></td><td><%=name%></td></tr></tbody></table>
        """,
        "template_cart": """
            <div class="modal-header"><button class="close">Close</button><h3>Shopping cart</h3></div>
            <div class="modal-body cart-modal">
                <p class="source-empty-cart">Your cart is empty</p>
                <table id="cartItems"><thead><tr><th>Product</th><th>VAT</th><th>Price</th><th></th></tr></thead><tbody></tbody></table>
                <dl class="priceItems"><dt>Net</dt><dd></dd><dt>VAT</dt><dd></dd><dt>Total</dt><dd></dd></dl>
                <div class="margin-box <% if (giftCard) { %>loadBalanceFPInTheCart<% } %>">
                    <label><input id="useBalanceCheckbox" type="checkbox">Balance</label></div>
                <label><input id="usageTermsCheckbox" type="checkbox" value="true">Terms</label>
                <div role="alert" class="alerts source-cart-alert"></div>
            </div>
            </div>
            <div class="modal-footer">
                <button class="continueShopping">Continue</button>
                <div class="btn-group" role="toolbar"><button class="buyCart">Pay</button></div>
            </div>
        """,
        "template__cartitem": """
            <td><div><%=model.title%></div></td><td><%=model.vat%></td><td><%=model.price%></td>
            <td><button class="remove">Delete</button></td>
        """,
        "template_cartwidget": """
            <div id="cart-aria-live"></div><table>
            <tr><td id="cart_texts">
            <span class="icon-shopping-cart"></span><%=collection.totalProducts()%>
            <span class="icon-money"></span><%=collection.totalPrice()%>
            </td><td><button class="buy">Buy</button><button class="empty">Empty</button></td></tr>
            </table>
        """,
    }
    templates.update(replacements)
    scripts = (
        f'<script type="text/html" id="{name}">{body}</script>'
        for name, body in templates.items() if body
    )
    return ("<html><body><div id='cart'></div>" + "".join(scripts)
            + '<script>const privateData = "exclude me";</script></body></html>').encode()


def test_inert_projection_preserves_changes_to_selected_source_paths():
    original = source_page()
    changed = original.replace(b'<header class="source-title-band">', b'<section class="new-band">', 1)
    changed = changed.replace(b'</h4></header>', b'</h4></section>', 1)
    result = extract_templates(changed)
    assert '<section class="new-band">' in result
    assert '<h4 class="title"' in result
    assert "source-title-band" in extract_templates(original)
    assert "source-title-band" not in result
    assert "source-child-wrapper" in result
    assert 'data-slot="child-row"' in result
    assert 'data-slot="balance-section"' in result
    assert 'data-slot="payment-methods"' in result
    assert '<button class="buyCart" data-slot="payment-button" type="button">' in result
    assert '<td>\n      <div data-slot="title"></div>' in result
    assert sha256(changed).hexdigest() in result
    raw_template = changed.split(b'id="template_eventgroup">')[1].split(b"</script>")[0]
    assert sha256(raw_template).hexdigest() in result
    assert result == extract_templates(changed)


def test_keeps_source_attention_and_label_elements_without_branch_values():
    result = extract_templates(source_page())
    for marker in ("source-buy-alert", "source-cart-alert", "source-empty-cart",
                   "location-break", "schedule-break"):
        assert marker in result
    assert '<h4 data-slot="extra-information-heading"></h4>' in result
    assert 'data-slot="child-label"' in result
    assert 'data-slot="participant-heading"' in result
    assert "loadBalanceFPInTheCart" not in result


def test_unsafe_payload_and_nonlocal_links_are_absent():
    raw = source_page().replace(
        b'<a class="ok" href="#">', b'<a class="ok" href="javascript:alert(1)" onclick="alert(2)" style="color:red">'
    )
    result = extract_templates(raw)
    assert 'class="ok"' in result
    for forbidden in ("<script", "<%", "onclick", "onsubmit", "javascript:", "style=", "privateData",
                      "Real name", "1234567", "childAccountForm", 'class="empty"'):
        assert forbidden not in result


def test_missing_participant_modal_availability_fails_extraction():
    raw = source_page().replace(
        b'<p class="eventgroup-labels"><%~_.template(jQuery("#template__eventgroup_labels").html(), data)%></p>',
        b"",
    )
    with pytest.raises(ExtractionError, match="template__eventgroup_buy:.*availability"):
        extract_templates(raw)


@pytest.mark.parametrize("raw", [
    pytest.param(source_page(template_cart=""), id="missing-template"),
    pytest.param(source_page().replace(b'class="priceDiv priceContainer"', b'class="priceDiv"'), id="missing-anchor"),
    pytest.param(source_page().replace(b'<h4 class="title">',
                 b'<iframe src="https://foreign.invalid/"></iframe><h4 class="title">'), id="iframe"),
    pytest.param(source_page().replace(b'<h4 class="title">',
                 b'<script>alert(1)</script><h4 class="title">'), id="script"),
    pytest.param(source_page().replace(b'<h4 class="title">',
                 b'<h4 class="title" DATA-EXTRACT-SLOT="other">'), id="reserved-marker"),
    pytest.param(source_page().replace(b'<h4 class="title">',
                 b'</div><h4 class="title">'), id="unreviewed-orphan-tag"),
])
def test_missing_or_executable_source_fails_without_fallback(raw):
    with pytest.raises(ExtractionError):
        extract_templates(raw)
