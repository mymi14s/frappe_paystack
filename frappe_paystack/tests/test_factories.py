"""A factory cleanup removes only a record the factory itself inserted."""

import frappe

from frappe_paystack.tests.factories import (
    CustomerFactory,
    GatewaySettingFactory,
    ItemFactory,
    PortalCustomerFactory,
    cleanup_doc,
)
from frappe_paystack.tests.session_setup import before_tests
from frappe_paystack.tests.test_base import TEST_COMPANY, PaystackTestCase

ERROR_LOG = "Error Log"
GATEWAY_SETTING = "Paystack Gateway Setting"
ITEM_PRICE = "Item Price"

# The price list a selling price hangs off.
SELLING_PRICE_LIST = "Standard Selling"

# Names inserted directly, so the factory finds them.
STANDING_CUSTOMER = "_Test Paystack Standing Customer"
STANDING_ITEM = "_Test Paystack Standing Item"
STANDING_GATEWAY = "_Test Paystack Standing Gateway"
STANDING_ADDRESS_TITLE = "_Test Paystack Standing Address"

STANDING_RATE = 1234.0


class FactoryGuardTestCase(PaystackTestCase):
    """Helpers for planting a record the factory finds."""

    def plant_customer(self, name: str = STANDING_CUSTOMER) -> str:
        """Insert a Customer outside the factory and register its cleanup."""
        if not frappe.db.exists("Customer", name):
            customer = frappe.get_doc(
                {
                    "doctype": "Customer",
                    "customer_name": name,
                    "customer_group": "Individual",
                    "territory": "All Territories",
                    "customer_type": "Individual",
                }
            )
            customer.flags.ignore_permissions = True
            customer.flags.ignore_mandatory = True
            customer.insert()

        self.addCleanup(cleanup_doc, "Customer", name)
        return name

    def plant_item(self, name: str = STANDING_ITEM) -> str:
        """Insert an Item outside the factory and register its cleanup."""
        if not frappe.db.exists("Item", name):
            item = frappe.get_doc(
                {
                    "doctype": "Item",
                    "item_code": name,
                    "item_name": name,
                    "item_group": "All Item Groups",
                    "stock_uom": "Nos",
                    "is_stock_item": 0,
                }
            )
            item.flags.ignore_permissions = True
            item.flags.ignore_mandatory = True
            item.insert()

        self.addCleanup(cleanup_doc, "Item", name)
        return name

    def plant_price(self, item_code: str) -> str:
        """Give a planted item a price, the way a live site would."""
        price = frappe.get_doc(
            {
                "doctype": ITEM_PRICE,
                "item_code": item_code,
                "price_list": "Standard Selling",
                "price_list_rate": STANDING_RATE,
            }
        )
        price.flags.ignore_permissions = True
        price.insert()
        self.addCleanup(cleanup_doc, ITEM_PRICE, price.name)
        return price.name


class TestCustomerFactoryLeavesFoundRecords(FactoryGuardTestCase):
    """CustomerFactory.cleanup only removes a customer create() inserted."""

    def test_a_customer_the_factory_found_is_left_alone(self) -> None:
        """A customer create() found is left in place by cleanup."""
        self.plant_customer()

        self.assertEqual(CustomerFactory.create(STANDING_CUSTOMER), STANDING_CUSTOMER)
        CustomerFactory.cleanup(STANDING_CUSTOMER)

        self.assertTrue(frappe.db.exists("Customer", STANDING_CUSTOMER))

    def test_a_customer_the_factory_inserted_is_removed(self) -> None:
        """A customer create() inserted is removed by cleanup."""
        name = "_Test Paystack Disposable Customer"
        cleanup_doc("Customer", name)

        CustomerFactory.create(name)
        self.assertTrue(frappe.db.exists("Customer", name))

        CustomerFactory.cleanup(name)
        self.assertFalse(frappe.db.exists("Customer", name))


class TestItemFactoryLeavesFoundRecords(FactoryGuardTestCase):
    """ItemFactory.cleanup only removes an item create() inserted."""

    def test_an_item_the_factory_found_is_left_alone(self) -> None:
        """An item create() found is left in place by cleanup."""
        self.plant_item()

        self.assertEqual(ItemFactory.create(STANDING_ITEM), STANDING_ITEM)
        ItemFactory.cleanup(STANDING_ITEM)

        self.assertTrue(frappe.db.exists("Item", STANDING_ITEM))

    def test_a_price_on_a_found_item_survives_cleanup(self) -> None:
        """A price on a found item survives the factory cleanup."""
        self.plant_item()
        price = self.plant_price(STANDING_ITEM)

        ItemFactory.create(STANDING_ITEM)
        ItemFactory.cleanup(STANDING_ITEM)

        self.assertTrue(frappe.db.exists(ITEM_PRICE, price))
        self.assertEqual(frappe.db.get_value(ITEM_PRICE, price, "price_list_rate"), STANDING_RATE)

    def test_an_item_the_factory_inserted_is_removed(self) -> None:
        """An item create() inserted is removed by cleanup."""
        name = "_Test Paystack Disposable Item"
        cleanup_doc("Item", name)

        ItemFactory.create(name)
        self.assertTrue(frappe.db.exists("Item", name))

        ItemFactory.cleanup(name)
        self.assertFalse(frappe.db.exists("Item", name))


class TestGatewaySettingFactoryLeavesFoundRecords(FactoryGuardTestCase):
    """GatewaySettingFactory.cleanup only removes a gateway it inserted."""

    def test_a_gateway_the_factory_found_is_left_alone(self) -> None:
        """A gateway create() found is left in place by cleanup."""
        if not frappe.db.exists("Paystack Gateway Setting", STANDING_GATEWAY):
            setting = frappe.get_doc(
                {
                    "doctype": "Paystack Gateway Setting",
                    "gateway": STANDING_GATEWAY,
                    "company": TEST_COMPANY,
                    "test_mode": 1,
                    "secret_key": "sk_test_standing",
                    "public_key": "pk_test_standing",
                    "suspense_account": self.suspense_account(),
                    "mode_of_payment": "Paystack",
                    "currency": frappe.db.get_value("Company", TEST_COMPANY, "default_currency"),
                    "enabled": 0,
                }
            )
            setting.flags.ignore_permissions = True
            setting.flags.ignore_links = True
            setting.insert()

        self.addCleanup(cleanup_doc, "Paystack Gateway Setting", STANDING_GATEWAY)

        GatewaySettingFactory.create(gateway=STANDING_GATEWAY, enabled=False)
        GatewaySettingFactory.cleanup(STANDING_GATEWAY)

        self.assertTrue(frappe.db.exists("Paystack Gateway Setting", STANDING_GATEWAY))


class TestGatewaySettingFactorySurvivesARollback(FactoryGuardTestCase):
    """What the factory inserted is not remembered past a rollback of it."""

    def plant_gateway(self, name: str = STANDING_GATEWAY) -> str:
        """Insert a gateway outside the factory, durably, and register its cleanup."""
        if not frappe.db.exists(GATEWAY_SETTING, name):
            setting = frappe.get_doc(
                {
                    "doctype": GATEWAY_SETTING,
                    "gateway": name,
                    "company": TEST_COMPANY,
                    "test_mode": 1,
                    "secret_key": "sk_test_standing",
                    "public_key": "pk_test_standing",
                    "suspense_account": self.suspense_account(),
                    "mode_of_payment": "Paystack",
                    "currency": frappe.db.get_value("Company", TEST_COMPANY, "default_currency"),
                    "enabled": 0,
                }
            )
            setting.flags.ignore_permissions = True
            setting.flags.ignore_links = True
            setting.insert()

        frappe.db.commit()
        self.addCleanup(cleanup_doc, GATEWAY_SETTING, name)
        return name

    def test_a_gateway_restored_by_a_rollback_is_left_alone(self) -> None:
        """A gateway the rollback put back survives a later cleanup."""
        standing = self.plant_gateway()

        frappe.delete_doc(GATEWAY_SETTING, standing, force=True, ignore_permissions=True)
        GatewaySettingFactory.create(gateway=standing, enabled=False)
        frappe.db.rollback()

        GatewaySettingFactory.cleanup(standing)

        self.assertTrue(frappe.db.exists(GATEWAY_SETTING, standing))


class TestPortalCustomerFactoryLeavesFoundRecords(FactoryGuardTestCase):
    """PortalCustomerFactory.cleanup matches on more than the address title."""

    def test_an_address_the_factory_did_not_raise_is_left_alone(self) -> None:
        """An address sharing the title of another party survives cleanup."""
        customer = self.plant_customer()

        address = frappe.get_doc(
            {
                "doctype": "Address",
                "address_title": STANDING_ADDRESS_TITLE,
                "address_type": "Billing",
                "address_line1": "1 Standing Street",
                "city": "Lagos",
                "country": frappe.db.get_single_value("System Settings", "country") or "Nigeria",
                "links": [{"link_doctype": "Customer", "link_name": customer}],
            }
        )
        address.flags.ignore_permissions = True
        address.insert()
        self.addCleanup(cleanup_doc, "Address", address.name)

        PortalCustomerFactory.cleanup(
            "_Test Paystack Absent Customer",
            "absent.portal@example.com",
            STANDING_ADDRESS_TITLE,
        )

        self.assertTrue(frappe.db.exists("Address", address.name))


class TestErrorLogsDoNotOutliveTheTest(PaystackTestCase):
    """tabError Log is MyISAM, so an alert survives the rollback."""

    def test_an_alert_raised_by_a_test_is_swept(self) -> None:
        """cleanup_test_data removes an Error Log a test committed."""
        alert = frappe.get_doc(
            {
                "doctype": ERROR_LOG,
                "method": "Paystack factory guard probe",
                "error": "raised by a factory guard test",
            }
        )
        alert.flags.ignore_permissions = True
        alert.insert()
        frappe.db.commit()

        self.assertIn(ERROR_LOG, self.transient_doctypes)

        self.cleanup_test_data()

        self.assertFalse(frappe.db.exists(ERROR_LOG, alert.name))


class TestBeforeTestsHookLeavesItemPricesAlone(PaystackTestCase):
    """The app's own before_tests hook keeps erpnext's out of the session."""

    def test_the_hook_resolves_to_the_app_s_own_setup(self) -> None:
        """frappe_paystack registers its own before_tests."""
        self.assertEqual(
            frappe.get_hooks("before_tests", app_name="frappe_paystack"),
            ["frappe_paystack.tests.session_setup.before_tests"],
        )

    def test_the_hook_is_not_erpnext_s(self) -> None:
        """No resolved hook reaches erpnext's Item Price delete."""
        self.assertNotIn(
            "erpnext.setup.utils.before_tests",
            frappe.get_hooks("before_tests", app_name="frappe_paystack"),
        )

    def test_running_it_keeps_the_prices_the_site_already_carries(self) -> None:
        """before_tests() leaves an existing Item Price standing."""
        item = ItemFactory.create(STANDING_ITEM)
        self.addCleanup(ItemFactory.cleanup, item)

        price = frappe.get_doc(
            {
                "doctype": ITEM_PRICE,
                "item_code": item,
                "price_list": SELLING_PRICE_LIST,
                "price_list_rate": 250,
            }
        )
        price.flags.ignore_permissions = True
        price.insert()
        frappe.db.commit()

        before_tests()

        self.assertTrue(frappe.db.exists(ITEM_PRICE, price.name))
