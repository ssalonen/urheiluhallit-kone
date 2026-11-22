"""Synthetic inputs exercise the checker. The manual smoke fetches the real shop."""

import pytest

from uhkone.shop_contract import (
    ContractError,
    check_catalogue,
    check_shop_html,
    check_shop_javascript,
)

COURSE = {
    "reservation_event_group_id": 123, "name": "Esimerkkikurssi", "location": "Esimerkkihalli",
    "service_id": 9, "location_id": 2, "reservation_group_id": 6,
    "start": "2030-01-10 10:00:00", "end": "2030-02-10 10:30:00",
    "public_reservation_start": "2030-01-01 10:00:00", "is_reservable": 1, "free": 2, "free_queue": 0,
    "weekdays": [{"weekdays": [3], "times": "10:00 - 10:30"}],
    "fare_products": [{"fare_product_id": 321, "price": 7600}],
}

SHOP_HTML = """\
<!doctype html>
<script src="/webshop/javascript/jsmodule/reservations2/next-version"></script>
<script type="text/html" id="template_eventgroup">
  <h4 class="title">Course</h4><button class="primary buy">Buy</button><button class="queue">Queue</button>
</script>
<script type="text/html" id="template_eventgroup_modal">
  <div class="modal-header"><h3>Course</h3></div><button class="buy">Buy</button><button class="queue">Queue</button>
</script>
<script type="text/html" id="template__participant_select">
  <input type="radio" id="linked-accounts">
  <div id="linkedAccount">
    <% if (children) { %><input name="participant" type="radio" value="<%= child.id %>" data-name="<%= child.name %>"><% } %>
  </div>
</script>
<script type="text/html" id="template__eventgroup_buy">
  <div class="modal-footer"><a class="primary ok">Add</a></div>
</script>
<script type="text/html" id="template_cart">
  <input id="usageTermsCheckbox" type="checkbox"><input id="useBalanceCheckbox" type="checkbox">
  <button class="buyWithBalance" payment-type-id="<%= balance.id %>">Balance</button>
  <div class="btn-group-dropdown-top-left"><button class="dropdown-toggle">Select</button></div>
  <a class="buyCart" payment-type-id="<%= payment.id %>">Pay</a>
</script>
"""

SHOP_JS = """\
R2.session = new R2.Session();
R2.user = new R2.User();
R2.eventgroups = new R2.EventGroups();
R2.cart = new R2.Cart();
const session = {isAuthenticated: function () {return true;}};
R2.vent.on('login:open', openLogin);
R2.vent.on('cart:open', openCart);
Enkora.getApiUrl('reservations2/apieventgroups');
Enkora.getApiUrl('reservations2/apicart');
Enkora.getApiUrl('reservations2/apiusereventgroups');
Enkora.ajax('call/cart/externalpaymentstart', args);
"""


def test_catalogue_requires_keys_even_when_adapter_can_supply_a_default():
    row = COURSE.copy()
    del row["service_id"]
    with pytest.raises(ContractError, match="service_id"):
        check_catalogue([row])


def test_catalogue_rejects_unparseable_course_even_when_all_keys_are_present():
    with pytest.raises(ContractError, match="tunnus"):
        check_catalogue([COURSE | {"reservation_event_group_id": "not-an-id"}])


def test_catalogue_rejects_missing_product_identifier():
    with pytest.raises(ContractError, match="fare_product_id"):
        check_catalogue([COURSE | {"fare_products": [{"price": 7600}]}])


def test_catalogue_rejects_empty_and_duplicate_rows():
    with pytest.raises(ContractError, match="tyhjä"):
        check_catalogue([])
    with pytest.raises(ContractError, match="useita kertoja"):
        check_catalogue([COURSE, COURSE])


@pytest.mark.parametrize(("observed", "renamed"), [
    ('id="linkedAccount"', 'id="linked-accounts-list"'),
    ('class="buyCart"', 'class="payCart"'),
])
def test_shop_html_rejects_selector_changes_even_with_old_markup_in_a_comment(observed, renamed):
    changed = SHOP_HTML.replace(observed, renamed) + f"<!-- <input {observed}> -->"
    with pytest.raises(ContractError, match=observed.split('"')[1]):
        check_shop_html(changed)


def test_shop_html_requires_child_input_inside_linked_accounts():
    changed = SHOP_HTML.replace('<div id="linkedAccount">', '<div id="linkedAccount"></div>')
    with pytest.raises(ContractError, match="linkedAccount"):
        check_shop_html(changed)


@pytest.mark.parametrize("origin", ["https://unrelated.example", "//unrelated.example"])
def test_shop_html_rejects_foreign_script_origin(origin):
    with pytest.raises(ContractError, match="alkuperä"):
        check_shop_html(SHOP_HTML.replace('src="/webshop/', f'src="{origin}/webshop/'))


def test_shop_javascript_rejects_renamed_r2_cart_assignment():
    with pytest.raises(ContractError, match=r"R2\.cart"):
        check_shop_javascript(SHOP_JS.replace("R2.cart =", "R2.basket ="))


def test_public_contract_accepts_supported_course_templates_and_javascript():
    assert check_catalogue([COURSE]) == 1
    assert check_shop_html(SHOP_HTML) == (
        "https://kauppa.urheiluhallit.fi/webshop/javascript/jsmodule/reservations2/next-version"
    )
    check_shop_javascript(SHOP_JS)


@pytest.mark.parametrize("change", [
    {"service_id": None}, {"location_id": None}, {"reservation_group_id": None},
    {"fare_products": [{"fare_product_id": None, "price": 7600}]},
    {"fare_products": [{"fare_product_id": 321, "price": "bad"}]},
    {"weekdays": [{"weekdays": [7], "times": "10:00-10:30"}]},
    {"weekdays": [{"weekdays": [3], "times": "25:00-26:00"}]},
    {"weekdays": []}, {"fare_products": []},
])
def test_unusable_booking_values_fail_the_catalogue_contract(change):
    with pytest.raises(ContractError):
        check_catalogue([COURSE | change])


def test_signed_overbooking_count_is_separate_from_a_representative_booking_row():
    full = COURSE | {"free_queue": -2, "reservation_event_group_id": 124}
    assert check_catalogue([COURSE, full]) == 1
    with pytest.raises(ContractError):
        check_catalogue([full])
