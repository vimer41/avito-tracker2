import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _environment
from tests._dispatcher import DispatcherFixture
import aiosqlite

import billing
import config
import database
import referrals

OWNER = 81001
BUYER = 81002
OLD_USER = 81003


async def sql(statement, parameters=()):
    async with aiosqlite.connect(database.DB_PATH) as db:
        await db.execute(statement, parameters)
        await db.commit()


class ReferralBase(DispatcherFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="avito-ref-")
        self.patches = [
            patch.object(database, "DB_PATH", Path(self.temp_dir.name) / "ref.db"),
            patch.object(config, "STARS_PAYMENTS_ENABLED", True),
            patch.object(config, "PLATEGA_ENABLED", True),
            patch.object(config, "REF_STAR_RATE", 1.2),
            patch.object(config, "REF_PERCENT", 15),
        ]
        for item in self.patches:
            item.start()
        await database.init_db()
        await database.get_or_create_user(config.ADMIN_ID, "admin", "Admin")
        await database.get_or_create_user(OWNER, "partner", "Partner")
        await database.get_or_create_user(OLD_USER, "old", "Old")
        self.link = await referrals.create_link(OWNER, config.ADMIN_ID, 30)
        self.setup_dispatcher(names=("payments", "admin", "referrals", "support", "common", "searches"))

    async def asyncTearDown(self):
        await self.close_dispatcher()
        for item in reversed(self.patches):
            item.stop()
        self.temp_dir.cleanup()

    def start_text(self, link=None):
        return f"/start ref_{(link or self.link)['code']}"

    def texts(self, chat_id=None):
        return [m.text for m in self.session.calls if type(m).__name__ in ("SendMessage", "EditMessageText")
                and (chat_id is None or int(m.chat_id) == chat_id)]

    async def bind_buyer(self):
        bound = await referrals.bind_from_start(BUYER, "buyer", "Buyer", self.start_text())
        self.assertIsNotNone(bound)
        await database.activate_trial(BUYER)

    async def pay_stars(self, plan="standart", charge="charge-1"):
        order = await billing.create_order(BUYER, plan)
        await billing.approve_checkout(f"q-{order['id']}", BUYER, order["payload"], "XTR", order["total_amount"])
        return await billing.complete_payment(BUYER, order["payload"], "XTR", order["total_amount"], charge)

    async def pay_rub(self, tx="tx-1"):
        order = await billing.create_rub_order(BUYER, "standart", 1, "sbp")
        await billing.attach_rub_payment(order["id"], tx, "https://pay.example/x", time.time() + 900, order["amount"] * 1.085)
        return await billing.complete_rub_order(order["id"], tx, order["amount"] * 1.085)


class TestBinding(ReferralBase):
    async def test_new_user_is_bound_once(self):
        bound = await referrals.bind_from_start(BUYER, "buyer", "Buyer", self.start_text())
        self.assertEqual(bound["id"], self.link["id"])
        user = await database.get_user(BUYER)
        self.assertEqual((user["referred_by"], user["ref_link_id"]), (OWNER, self.link["id"]))
        self.assertIsNone(await referrals.bind_from_start(BUYER, "buyer", "Buyer", self.start_text()))

    async def test_existing_user_self_admin_and_bad_codes_are_not_bound(self):
        self.assertIsNone(await referrals.bind_from_start(OLD_USER, "old", "Old", self.start_text()))
        self.assertIsNone((await database.get_user(OLD_USER))["referred_by"])
        self.assertIsNone(await referrals.bind_from_start(OWNER, "p", "P", self.start_text()))
        self.assertIsNone(await referrals.bind_from_start(config.ADMIN_ID, "a", "A", self.start_text()))
        self.assertIsNone(await referrals.bind_from_start(BUYER, "b", "B", "/start ref_nope1234"))
        self.assertIsNone(await referrals.bind_from_start(BUYER, "b", "B", "/start"))
        self.assertIsNone(await database.get_user(BUYER))

    async def test_expired_or_switched_off_link_does_not_bind(self):
        await sql("UPDATE ref_links SET expires_at = datetime('now', '-1 minute')")
        self.assertIsNone(await referrals.bind_from_start(BUYER, "b", "B", self.start_text()))
        await referrals.extend_link(self.link["id"], 30)
        await referrals.revoke_link(self.link["id"], config.ADMIN_ID)
        self.assertIsNone(await referrals.bind_from_start(BUYER, "b", "B", self.start_text()))

    async def test_one_link_per_owner_and_none_for_admins(self):
        with self.assertRaises(referrals.ReferralError):
            await referrals.create_link(OWNER, config.ADMIN_ID, 30)
        with self.assertRaises(referrals.ReferralError):
            await referrals.create_link(config.ADMIN_ID, config.ADMIN_ID, 30)

    async def test_start_command_binds_and_tells_the_owner(self):
        await self.dispatch_message(user_id=BUYER, text=self.start_text())
        self.assertEqual((await database.get_user(BUYER))["referred_by"], OWNER)
        self.assertTrue(any("По вашей ссылке пришёл новый пользователь" in t for t in self.texts(OWNER)))


class TestRewards(ReferralBase):
    async def test_stars_payment_gives_percent_at_the_star_rate_once(self):
        await self.bind_buyer()
        order = await self.pay_stars()
        self.assertEqual(order["ref_reward"]["reward_kop"], round(449 * 1.2 * 15))  # 80,82 ₽
        again = await billing.complete_payment(BUYER, order["payload"], "XTR", order["total_amount"], "charge-1")
        self.assertFalse(again["applied"])
        s = await referrals.summary(OWNER)
        self.assertEqual((s["came"], s["buyers"], s["earned"], s["pending"], s["balance"]), (1, 1, 8082, 8082, 0))

    async def test_rub_payment_counts_the_price_without_the_buyer_fee(self):
        await self.bind_buyer()
        order = await self.pay_rub()
        self.assertEqual(order["ref_reward"]["reward_kop"], round(order["amount"] * 15))

    async def test_unbound_buyer_and_test_tariff_give_nothing(self):
        await database.get_or_create_user(BUYER, "b", "B")
        await database.activate_trial(BUYER)
        self.assertIsNone((await self.pay_stars())["ref_reward"])
        async with aiosqlite.connect(database.DB_PATH) as db:
            await sql("UPDATE users SET referred_by = ?, ref_link_id = ? WHERE user_id = ?", (OWNER, self.link["id"], BUYER))
            self.assertIsNone(await referrals.accrue_in_transaction(db, "rub", 999, BUYER, config.RUB_TEST_PLAN, 1, "RUB"))

    async def test_expired_link_stops_rewards_and_turning_it_on_again_resumes_them(self):
        await self.bind_buyer()
        await sql("UPDATE ref_links SET expires_at = datetime('now', '-1 day')")
        self.assertIsNone((await self.pay_stars(charge="c-1"))["ref_reward"])
        link = await referrals.extend_link(self.link["id"], 30)
        self.assertTrue(referrals.is_active(link))
        self.assertEqual(link["code"], self.link["code"])
        self.assertIsNotNone((await self.pay_stars(charge="c-2"))["ref_reward"])

    async def test_switched_off_link_gives_nothing(self):
        await self.bind_buyer()
        await referrals.revoke_link(self.link["id"], config.ADMIN_ID)
        self.assertIsNone((await self.pay_stars())["ref_reward"])

    async def test_hold_then_ready_and_payout(self):
        await self.bind_buyer()
        await self.pay_stars()
        await sql("UPDATE ref_rewards SET available_at = datetime('now', '-1 minute')")
        s = await referrals.summary(OWNER)
        self.assertEqual((s["pending"], s["ready"], s["balance"]), (0, 8082, 8082))
        await referrals.record_payout(OWNER, 8000, config.ADMIN_ID, "СБП")
        self.assertEqual((await referrals.summary(OWNER))["balance"], 82)

    async def test_stars_refund_cancels_and_a_paid_out_reward_leaves_a_debt(self):
        await self.bind_buyer()
        order = await self.pay_stars()
        await sql("UPDATE ref_rewards SET available_at = datetime('now', '-1 minute')")
        await referrals.record_payout(OWNER, 8082, config.ADMIN_ID)
        await billing.begin_refund(order["id"], config.ADMIN_ID)
        self.assertEqual((await referrals.summary(OWNER))["frozen"], 8082)
        await billing.complete_refund("charge-1")
        s = await referrals.summary(OWNER)
        self.assertEqual((s["earned"], s["balance"]), (0, -8082))
        self.assertEqual(s["recent"][0]["state"], referrals.STATE_CANCELED)

    async def test_rub_dispute_freezes_partial_refund_shrinks_chargeback_cancels(self):
        await self.bind_buyer()
        order = await self.pay_rub()
        full = order["ref_reward"]["reward_kop"]
        self.assertTrue(await billing.mark_rub_disputed(order["id"]))
        self.assertEqual((await referrals.summary(OWNER))["frozen"], full)
        self.assertTrue(await billing.set_rub_status(order["id"], "paid", ("disputed",)))
        self.assertEqual((await referrals.summary(OWNER))["pending"], full)
        await billing.note_rub_partial_refund(order["id"], order["paid_amount"] / 2)
        self.assertEqual((await referrals.summary(OWNER))["pending"], round(full / 2))
        await billing.revoke_rub_order(order["id"])
        self.assertEqual((await referrals.summary(OWNER))["earned"], 0)


class TestScreens(ReferralBase):
    async def test_owner_sees_link_and_money_in_the_profile(self):
        await self.bind_buyer()
        await self.pay_stars()
        await self.dispatch_callback("refresh_profile", OWNER)
        self.assertIn("ref_me", self.callback_values())
        await self.dispatch_callback("ref_me", OWNER)
        text = self.texts(OWNER)[-1]
        self.assertIn(f"start=ref_{self.link['code']}", text)
        self.assertIn("Пришло по ссылке:</b> 1", text)
        self.assertIn("80,82 ₽", text)
        self.assertIn(config.REF_PAYOUT_CONTACT, text)

    async def test_users_without_a_link_have_no_partner_button(self):
        await self.dispatch_callback("refresh_profile", OLD_USER)
        self.assertNotIn("ref_me", self.callback_values())

    async def test_admin_issues_extends_switches_off_and_records_payout(self):
        await database.get_or_create_user(BUYER, "b", "B")
        await self.dispatch_callback(f"ref_adm_user:{BUYER}", config.ADMIN_ID)
        self.assertIn(f"ref_adm_new:{BUYER}:30", self.callback_values())
        await self.dispatch_callback(f"ref_adm_new:{BUYER}:30", config.ADMIN_ID)
        link = await referrals.get_owner_link(BUYER)
        self.assertTrue(referrals.is_active(link))
        self.assertTrue(any("Вам открыта партнёрская программа" in t for t in self.texts(BUYER)))

        await self.dispatch_callback(f"ref_adm_offy:{link['id']}", config.ADMIN_ID)
        self.assertFalse(referrals.is_active(await referrals.get_link(link["id"])))
        self.assertIn(f"ref_adm_ext:{link['id']}", self.callback_values())
        await self.dispatch_callback(f"ref_adm_ext:{link['id']}:90", config.ADMIN_ID)
        self.assertTrue(referrals.is_active(await referrals.get_link(link["id"])))

        await self.dispatch_callback(f"ref_adm_pct:{link['id']}:20", config.ADMIN_ID)
        self.assertEqual((await referrals.get_link(link["id"]))["percent"], 20)

        await self.dispatch_callback(f"ref_adm_pay:{link['id']}", config.ADMIN_ID)
        await self.dispatch_message(user_id=config.ADMIN_ID, text="1 500,50 карта")
        self.assertEqual((await referrals.payouts(BUYER))[0]["amount_kop"], 150050)

        await self.dispatch_callback("ref_adm:0", config.ADMIN_ID)
        self.assertIn(f"ref_adm_link:{link['id']}", self.callback_values())
        await self.dispatch_callback(f"ref_adm_log:{link['id']}", config.ADMIN_ID)
        self.assertIn("1 500,50 ₽", self.texts(config.ADMIN_ID)[-1])

    async def test_admin_sets_star_rate(self):
        await self.dispatch_callback("ref_adm_rate", config.ADMIN_ID)
        await self.dispatch_message(user_id=config.ADMIN_ID, text="1,35")
        self.assertEqual(await referrals.star_rate(), 1.35)

    async def test_non_admin_cannot_issue(self):
        await self.dispatch_callback(f"ref_adm_new:{OLD_USER}:30", OLD_USER)
        self.assertIsNone(await referrals.get_owner_link(OLD_USER))


class TestHelpers(unittest.TestCase):
    def test_money_and_codes(self):
        self.assertEqual(referrals.fmt_rub(8082), "80,82 ₽")
        self.assertEqual(referrals.fmt_rub(150000), "1 500 ₽")
        self.assertEqual(referrals.fmt_rub(-500), "−5 ₽")
        self.assertEqual(referrals.parse_rub("1 500,5"), 150050)
        self.assertIsNone(referrals.parse_rub("abc"))
        self.assertEqual(referrals.parse_start_code("/start ref_AbC12345"), "abc12345")
        self.assertIsNone(referrals.parse_start_code("/start promo"))


if __name__ == "__main__":
    unittest.main()
