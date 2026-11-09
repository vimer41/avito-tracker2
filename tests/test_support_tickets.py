import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tests import _environment  # noqa: F401
from aiogram.types import Chat, Message, Update, User

import config
import database
from handlers import support
from tests._dispatcher import DispatcherFixture

USER = 123456
STRANGER = 654321
ADMIN, ADMIN2 = config.ADMIN_IDS[0], config.ADMIN_IDS[1]


class TicketCase(DispatcherFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="avito-tickets-")
        self.db_patch = patch.object(database, "DB_PATH", Path(self.directory.name) / "test.db")
        self.db_patch.start()
        await database.init_db()
        for uid in (USER, STRANGER):
            await database.get_or_create_user(uid, f"u{uid}", "Fixture")
            await database.set_user_allowed(uid, 1)
        self.setup_dispatcher()

    async def asyncTearDown(self):
        await self.close_dispatcher()
        await database.close_db()
        self.db_patch.stop()
        self.directory.cleanup()

    async def say(self, text, user_id=USER, reply_to=None):
        self.update_id += 1
        replied = None
        if reply_to is not None:
            replied = Message(message_id=reply_to, date=datetime.now(timezone.utc), chat=Chat(id=user_id, type="private"),
                              from_user=User(id=self.bot.id, is_bot=True, first_name="Bot"), text="quoted")
        message = Message(message_id=10_000 + self.update_id, date=datetime.now(timezone.utc),
                          chat=Chat(id=user_id, type="private"),
                          from_user=User(id=user_id, is_bot=False, first_name="Fixture"),
                          text=text, reply_to_message=replied)
        await self.dp.feed_update(self.bot, Update(update_id=self.update_id, message=message))

    async def write(self, text, user_id=USER, button="support_start_ticket"):
        await self.dispatch_callback(button, user_id=user_id)
        await self.say(text, user_id)

    def sent_to(self, chat_id):
        return [m for m in self.methods("SendMessage") if m.chat_id == chat_id]

    def alerts(self):
        return [m.text for m in self.methods("AnswerCallbackQuery") if m.text]

    async def admin_answer(self, ticket_id, text, admin=ADMIN):
        await self.dispatch_callback(f"admin_tkr:{ticket_id}", user_id=admin)
        await self.say(text, admin)


class TestTicketFlow(TicketCase):
    async def test_messages_join_one_ticket_until_it_is_closed(self):
        await self.write("Не приходят лоты")
        await self.write("И ещё вопрос")
        tickets, total = await database.list_user_tickets(USER)
        self.assertEqual(total, 1)
        texts = [m["text"] for m in await database.get_ticket_messages(tickets[0]["id"])]
        self.assertEqual(texts, ["Не приходят лоты", "И ещё вопрос"])
        notices = self.sent_to(ADMIN)
        self.assertIn("Новое обращение #1", notices[0].text)
        self.assertIn("новое сообщение", notices[1].text)
        self.assertIn("admin_tkr:1", [b.callback_data for row in notices[0].reply_markup.inline_keyboard for b in row])

    async def test_admin_answer_reaches_the_user_and_the_other_admin_is_told(self):
        await self.write("Помогите")
        await self.admin_answer(1, "Уже смотрим")
        answer = self.sent_to(USER)[-1]
        self.assertIn("Ответ по обращению #1", answer.text)
        self.assertIn("Уже смотрим", answer.text)
        self.assertTrue(any("ответил" in m.text for m in self.sent_to(ADMIN2)))
        self.assertEqual((await database.get_ticket(1))["status"], "answered")

    async def test_quote_replies_continue_the_ticket_on_both_sides(self):
        await self.write("Вопрос")
        # The offline session numbers a sent message by its position among the calls.
        notice_id = self.session.calls.index(self.sent_to(ADMIN)[0]) + 1
        self.assertEqual((await database.get_ticket_by_ref(ADMIN, notice_id))["id"], 1)
        await self.say("Ответ цитатой", ADMIN, reply_to=notice_id)
        answer = self.sent_to(USER)[-1]
        self.assertIn("Ответ по обращению #1", answer.text)
        await self.say("Спасибо, а ещё?", USER, reply_to=self.session.calls.index(answer) + 1)
        authors = [m["author"] for m in await database.get_ticket_messages(1)]
        self.assertEqual(authors, ["user", "admin", "user"])
        self.assertEqual((await database.get_ticket(1))["status"], "open")

    async def test_unrelated_replies_are_not_swallowed_by_support(self):
        await self.say("просто ответ", USER, reply_to=5)
        self.assertEqual((await database.list_user_tickets(USER))[1], 0)

    async def test_user_closes_and_reopens_within_the_window(self):
        await self.write("Вопрос")
        await self.dispatch_callback("tk_close:1")
        await self.dispatch_callback("tk_close_y:1")
        ticket = await database.get_ticket(1)
        self.assertEqual((ticket["status"], ticket["closed_by"]), ("closed", "user"))
        self.assertTrue(any("закрыл пользователь" in m.text for m in self.sent_to(ADMIN)))
        await self.dispatch_callback("tk_reopen:1")
        self.assertEqual((await database.get_ticket(1))["status"], "open")
        notes = [m["text"] for m in await database.get_ticket_messages(1) if m["author"] == "system"]
        self.assertEqual(notes, ["Закрыто пользователем", "Открыто снова пользователем"])

    async def test_after_the_window_a_reply_starts_a_linked_ticket(self):
        await self.write("Вопрос")
        await database.close_ticket(1, "user")
        old = time.time() - (config.SUPPORT_REOPEN_DAYS + 1) * 86400
        async with database.writer_connection() as db:
            await db.execute("UPDATE tickets SET closed_epoch = ? WHERE id = 1", (old,))
            await db.commit()
        await self.dispatch_callback("tk_reopen:1")
        self.assertTrue(any("Прошло больше" in a for a in self.alerts()))
        await self.write("Снова проблема", button="tk_write:1")
        ticket = await database.get_ticket(2)
        self.assertEqual((ticket["parent_id"], ticket["status"]), (1, "open"))
        self.assertEqual((await database.get_ticket(1))["status"], "closed")

    async def test_admin_closes_with_a_last_word(self):
        await self.write("Вопрос")
        await self.dispatch_callback("admin_tkc:1", user_id=ADMIN)
        await self.say("Готово, всё починили", ADMIN)
        ticket = await database.get_ticket(1)
        self.assertEqual((ticket["status"], ticket["closed_by"], ticket["closed_by_admin"]), ("closed", "admin", ADMIN))
        last = self.sent_to(USER)[-1]
        self.assertIn("закрыто поддержкой", last.text)
        self.assertIn("tk_reopen:1", [b.callback_data for row in last.reply_markup.inline_keyboard for b in row])

    async def test_admin_closes_without_a_message(self):
        await self.write("Вопрос")
        await self.dispatch_callback("admin_tkc:1", user_id=ADMIN)
        await self.dispatch_callback("admin_tkc_y:1", user_id=ADMIN)
        self.assertEqual((await database.get_ticket(1))["status"], "closed")
        self.assertIn("закрыто поддержкой", self.sent_to(USER)[-1].text)
        self.assertTrue(any("закрыл" in m.text for m in self.sent_to(ADMIN2)))

    async def test_message_to_a_user_who_never_wrote_starts_a_ticket(self):
        await self.dispatch_callback(f"support_reply:{USER}", user_id=ADMIN)
        await self.say("Здравствуйте, у вас кончается подписка", ADMIN)
        ticket = await database.get_live_ticket(USER)
        self.assertEqual(ticket["status"], "answered")
        self.assertIn("Ответ по обращению", self.sent_to(USER)[-1].text)

    async def test_stranger_cannot_open_someone_elses_ticket(self):
        await self.write("Личное")
        await self.dispatch_callback("tk:1:-1", user_id=STRANGER)
        await self.dispatch_callback("tk_close_y:1", user_id=STRANGER)
        self.assertEqual(self.alerts().count("Обращение не найдено"), 2)
        self.assertEqual((await database.get_ticket(1))["status"], "open")

    async def test_expired_user_still_sees_and_writes_tickets(self):
        await self.write("Вопрос")
        await database.set_user_allowed(USER, 0)
        await self.dispatch_callback("tk_my:0")
        self.assertIn("tk:1:-1", self.callback_values())
        await self.write("Ещё", button="tk_write:1")
        self.assertEqual(len(await database.get_ticket_messages(1)), 2)


class TestTicketCard(TicketCase):
    async def test_card_shows_the_last_page_and_pages_back(self):
        ticket, _mid, _new = await database.add_user_ticket_message(USER, "Fixture", "", "сообщение 1")
        for n in range(2, 13):
            await database.add_user_ticket_message(USER, "Fixture", "", f"сообщение {n}")
        await self.dispatch_callback(f"tk:{ticket['id']}:-1")
        screen = self.methods("EditMessageText")[-1]
        self.assertIn("стр. 3/3", screen.text)
        self.assertIn("сообщение 12", screen.text)
        self.assertNotIn("сообщение 10", screen.text)
        self.assertIn(f"tk:{ticket['id']}:1", self.callback_values())
        await self.dispatch_callback(f"tk:{ticket['id']}:0")
        first = self.methods("EditMessageText")[-1].text
        self.assertIn("сообщение 1\n", first.replace("</blockquote>", "\n"))
        self.assertNotIn("сообщение 6", first)
        self.assertNotIn(f"tk:{ticket['id']}:-1", self.callback_values())

    async def test_long_message_is_cut_and_sent_whole_on_request(self):
        ticket, _mid, _new = await database.add_user_ticket_message(USER, "Fixture", "", "а" * 1000)
        await self.dispatch_callback(f"tk:{ticket['id']}:-1")
        self.assertNotIn("а" * 400, self.methods("EditMessageText")[-1].text)
        self.assertIn(f"tk_full:{ticket['id']}:0", self.callback_values())
        await self.dispatch_callback(f"tk_full:{ticket['id']}:0")
        self.assertIn("а" * 1000, self.sent_to(USER)[-1].text)

    async def test_admin_card_has_admin_buttons_and_export(self):
        await self.write("Вопрос")
        await self.dispatch_callback("tk:1:-1", user_id=ADMIN)
        values = self.callback_values()
        self.assertIn("admin_tkr:1", values)
        self.assertIn("admin_tkx:1", values)
        self.assertNotIn("tk_close:1", values)
        await self.dispatch_callback("admin_tkx:1", user_id=ADMIN)
        self.assertEqual(self.methods("SendDocument")[-1].document.filename, "ticket_1.txt")

    async def test_admin_lists_and_home_button(self):
        await self.write("Вопрос")
        await self.dispatch_callback("admin_tks", user_id=ADMIN)
        self.assertIn("admin_tkl:open:0", self.callback_values())
        await self.dispatch_callback("admin_tkl:open:0", user_id=ADMIN)
        self.assertIn("tk:1:-1", self.callback_values())
        await self.dispatch_callback("admin_refresh", user_id=ADMIN)
        self.assertIn("admin_tks", self.callback_values())

    async def test_non_admin_cannot_open_the_admin_panel(self):
        await self.dispatch_callback("admin_tkl:open:0", user_id=USER)
        self.assertIn("У вас нет прав администратора", self.alerts())


class TestTicketStorage(TicketCase):
    async def test_deleting_the_user_keeps_the_history(self):
        await self.write("Верните деньги")
        await self.admin_answer(1, "Вернули")
        with patch("logger_setup.sanitize_logs_for_user"):
            result = await database.purge_user_by_pattern(str(USER))
        self.assertTrue(result["success"])
        ticket = await database.get_ticket(1)
        self.assertIsNotNone(ticket["user_deleted_epoch"])
        self.assertEqual(ticket["status"], "closed")
        self.assertEqual([m["text"] for m in await database.get_ticket_messages(1)][:2], ["Верните деньги", "Вернули"])
        self.assertEqual((await database.list_user_tickets(USER))[1], 0)
        self.assertEqual((await database.list_user_tickets(USER, include_deleted=True))[1], 1)
        await self.dispatch_callback("tk:1:-1", user_id=ADMIN)
        self.assertIn("аккаунт удалён", self.methods("EditMessageText")[-1].text)

    async def test_silent_answered_ticket_closes_itself(self):
        await self.write("Вопрос")
        await self.admin_answer(1, "Ответ")
        late = time.time() + config.SUPPORT_AUTOCLOSE_HOURS * 3600 + 60
        with patch.object(database.time, "time", return_value=late):
            closed = await database.close_stale_tickets()
        self.assertEqual([t["id"] for t in closed], [1])
        self.assertEqual((await database.get_ticket(1))["closed_by"], "auto")
        await support.notify_user_closed(self.bot, closed[0], by_admin=False)
        self.assertIn("ответа не было", self.sent_to(USER)[-1].text)

    async def test_waiting_ticket_never_closes_itself(self):
        await self.write("Вопрос")
        late = time.time() + 30 * 86400
        self.assertEqual(await database.close_stale_tickets(late), [])

    async def test_old_outbox_rows_become_tickets_waiting_for_delivery(self):
        async with database.writer_connection() as db:
            await db.execute(
                "INSERT INTO support_outbox (user_id, full_name, username, text, is_payment, created_epoch)"
                " VALUES (?, 'Old', 'old', 'старое обращение', 1, ?)", (USER, time.time()),
            )
            await db.commit()
        await database.init_db()
        pending = await database.get_undelivered_ticket_messages()
        self.assertEqual([(p["text"], p["topic"]) for p in pending], [("старое обращение", "payment")])
        self.assertEqual(await support.retry_undelivered_tickets(self.bot), 1)

    async def test_stats_measure_the_wait_for_an_answer(self):
        start = time.time() - 10
        ticket, _mid, _new = await database.add_user_ticket_message(USER, "F", "", "?")
        async with database.writer_connection() as db:
            await db.execute("UPDATE ticket_messages SET created_epoch = ? WHERE ticket_id = ?", (start, ticket["id"]))
            await db.commit()
        await database.add_admin_ticket_message(ticket["id"], ADMIN, "A", "!")
        stats = await database.get_ticket_stats(start - 60)
        self.assertEqual((stats["opened"], stats["answered"], stats["waiting"]), (1, 1, 0))
        self.assertGreaterEqual(stats["median_wait"], 9)


if __name__ == "__main__":
    unittest.main()
