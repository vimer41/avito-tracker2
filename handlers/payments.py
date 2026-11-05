import asyncio
import html
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Message, PreCheckoutQuery

import billing
import config
import database
import legal_docs
import ui

logger = logging.getLogger(__name__)
router = Router()


@router.message(Command("terms"))
@router.callback_query(F.data == "payment_terms")
async def payment_terms(event):
    message = event.message if isinstance(event, CallbackQuery) else event
    text = (
        "<b>Условия подписки и оплаты</b>\n"
        f"Редакция от {legal_docs.DOCS_DATE} · Код проверки: <code>{legal_docs.VERIFICATION_CODE}</code>\n\n"
        + "".join(
            f"• <b>{config.plan_title(plan)}:</b> {config.rub_price(plan)} ₽ или {config.stars_price(plan)} ⭐ / 30 дней, "
            f"{config.rub_price(plan, 3)} ₽ или {config.stars_price(plan, 3)} ⭐ / 90 дней; "
            f"радаров — до {config.plan_radars(plan)}, проверка раз в {config.plan_interval(plan)} с.\n"
            for plan in config.PAID_PLANS
        ) + "\n"
        "<b>Правила предоставления услуги:</b>\n"
        "• Услуга является нематериальной цифровой услугой по программному мониторингу открытых объявлений.\n"
        "• Разовая оплата за 30 или 90 дней доступа, <b>без автосписаний</b>. Подписка включается сразу после подтверждения транзакции.\n"
        "• Неиспользованный оплаченный срок сохраняется: оплаченные дни суммируются к текущей дате окончания.\n"
        f"• Максимальная сумма одной оплаты: не более {legal_docs.LIMIT_TEXT}.\n"
        "• Скорость и доступность объявлений зависят от площадки Авито, сетевых ограничений и нагрузки сервера.\n\n"
        "<b>Возврат средств и поддержка:</b>\n"
        "• Заявления на возврат средств рассматриваются в течение <b>24 часов</b>.\n"
        f"• Для оформления запроса используйте команду <b>/paysupport</b> или email: <b>{legal_docs.SUPPORT_EMAIL}</b>.\n"
        "• При подтверждении возврата доступ деактивируется, средства возвращаются тем же способом, которым была совершена оплата.\n\n"
        "Совершая оплату, вы подтверждаете согласие с настоящими Условиями и Пользовательским соглашением."
    )
    await ui.show(event, f"{ui.e('card')} {text}", ui.kb(
        [ui.btn("Соглашение", "docs", callback_data="doc_agreement"),
         ui.btn("Конфиденциальность", "lock", callback_data="doc_privacy")],
        [ui.btn("Тарифы", "tariffs", callback_data="open_subscription_menu"),
         ui.btn("Вопрос по оплате", "support", callback_data="payment_support")],
        [ui.home_btn()],
    ))


@router.callback_query(F.data.startswith(("demo_tariff:", "pay_request:")))
async def legacy_checkout(call: CallbackQuery):
    # Old messages must not resurrect card/crypto payments or demo activation.
    from handlers.common import cb_select_tariff
    await cb_select_tariff(call)


@router.callback_query(F.data.startswith("stars_buy:"))
async def buy_stars(call: CallbackQuery, bot: Bot, state: FSMContext):
    try:
        # stars_buy:<plan>:<months>:<price>; buttons from before periods have no months and mean one month.
        parts = call.data.split(":")
        if len(parts) == 3:
            parts.insert(2, "1")
        _, plan, months_raw, displayed_price = parts
        months = int(months_raw)
        if months not in config.PERIOD_MONTHS:
            raise billing.PaymentValidationError("Такого срока нет. Откройте тариф заново.")
        if int(displayed_price) != config.stars_price(plan, months):
            raise billing.PaymentValidationError("Цена изменилась. Откройте тариф заново и подтвердите новые условия.")
        if not call.message or call.message.chat.type != "private" or call.message.chat.id != call.from_user.id:
            raise billing.PaymentValidationError("Оплата доступна только в личном чате с ботом.")
        order = await billing.create_order(call.from_user.id, plan, months)
    except ValueError as exc:
        await call.answer(str(exc), show_alert=True)
        return
    await state.clear()
    await call.answer("Открываю счёт Telegram Stars")
    try:
        period = config.period_title(months)
        plan_title = "AVITO RADAR · ТЕСТ · 1 день" if plan == "test" else f"AVITO RADAR · {config.plan_title(plan)} · {period}"
        plan_desc = "Тестовая проверка оплаты Telegram Stars (1 день)." if plan == "test" else (
            f"Радаров — до {config.plan_radars(plan)}, проверка раз в {config.plan_interval(plan)} с. "
            "Разовая подписка, без автосписания. Условия: /terms. Поддержка и возврат: /paysupport."
        )
        label = "Тестовый доступ (1 день)" if plan == "test" else f"Подписка на {order['days']} дней ({period})"
        await bot.send_invoice(
            chat_id=call.from_user.id,
            title=plan_title,
            description=plan_desc,
            payload=order["payload"], provider_token="", currency="XTR",
            prices=[LabeledPrice(label=label, amount=order["total_amount"])],
            start_parameter="tariffs", protect_content=True,
        )
    except Exception as exc:
        logger.warning("Stars invoice delivery failed: order=%s error=%s", order["id"], type(exc).__name__)
        await call.message.answer("Не удалось отправить счёт. Деньги не списаны; попробуйте снова или откройте /paysupport.")


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, bot: Bot):
    try:
        await asyncio.wait_for(billing.approve_checkout(
            query.id, query.from_user.id, query.invoice_payload, query.currency, query.total_amount,
        ), timeout=4)
    except billing.PaymentValidationError as exc:
        await bot.answer_pre_checkout_query(pre_checkout_query_id=query.id, ok=False, error_message=str(exc))
    except Exception as exc:
        logger.error("Stars precheckout unavailable: %s", type(exc).__name__)
        await bot.answer_pre_checkout_query(pre_checkout_query_id=query.id, ok=False,
                                           error_message="Не удалось проверить счёт. Списание отменено; попробуйте позже.")
    else:
        await bot.answer_pre_checkout_query(pre_checkout_query_id=query.id, ok=True)


async def notify_admins(bot: Bot, text: str, user_id: int):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Пользователь / платежи", callback_data=f"admin_user:{user_id}:0")],
        [InlineKeyboardButton(text="✍️ Ответить", callback_data=f"support_reply:{user_id}")],
    ])
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, reply_markup=kb, parse_mode="HTML")
        except Exception as exc:
            logger.warning("Payment admin notification failed: %s", type(exc).__name__)


@router.message(F.successful_payment)
async def successful_payment(message: Message, bot: Bot):
    payment = message.successful_payment
    try:
        order = await billing.complete_payment(
            message.from_user.id, payment.invoice_payload, payment.currency, payment.total_amount,
            payment.telegram_payment_charge_id, payment.provider_payment_charge_id,
        )
    except billing.PaymentValidationError as exc:
        logger.error("Stars confirmed payment requires review: user=%s reason=%s", message.from_user.id, exc)
        await message.answer("Telegram подтвердил оплату, но автоматическая сверка не прошла. Поддержка уведомлена; откройте /paysupport.")
        await notify_admins(bot, f"⚠️ <b>Нужна сверка подтверждённого платежа</b>\nID: <code>{message.from_user.id}</code>\n"
                            f"Charge: <code>{html.escape(payment.telegram_payment_charge_id)}</code>\n{html.escape(str(exc))}", message.from_user.id)
        return
    # The transaction is committed before any notification; re-delivery cannot add days twice.
    if not order["applied"]:
        await message.answer("Этот платёж уже обработан. Текущая подписка доступна в /profile; вопросы — /paysupport.")
        return
    notes = ""
    if (order["allowed_before"] and order["tariff_before"] != order["tariff_after"]
            and database.subscription_seconds_left({"subscription_until": order["subscription_before"]})):
        notes += "\nОстаток прежнего тарифа пересчитан по цене нового."
    paused = await database.count_limit_paused(order["user_id"])
    if paused:
        notes += f"\nНе вошли в лимит тарифа и стоят на паузе радаров: {paused}. Какие запускать, можно выбрать в «Мои радары»."
    await message.answer(
        f"{ui.e('ok')} <b>Оплата {order['total_amount']} ⭐ прошла</b>\n\n"
        f"Тариф <b>«{config.plan_title(order['tariff_plan'])}»</b>, радаров до {await database.get_user_max_radars(order['user_id'])}.\n"
        f"+{order['days']} дней, действует до {database.format_subscription_date(order['subscription_after'])}.\n"
        f"Заказ <code>{order['id']}</code>, без автосписания. Вопросы: /paysupport.{notes}",
        parse_mode="HTML", reply_markup=ui.kb(
            [ui.btn("Добавить радар", "radar", callback_data="add_search_prompt", style="primary")],
            [ui.btn("Мои радары", "radars", callback_data="list_searches"),
             ui.btn("Профиль", "profile", callback_data="refresh_profile")],
        ),
    )
    logger.info("Stars payment fulfilled: order=%s user=%s plan=%s amount=%s", order["id"], order["user_id"], order["tariff_plan"], order["total_amount"])
    try:
        await notify_admins(bot, await payment_notice(message, order), order["user_id"])
    except Exception:
        logger.warning("Payment notice for admins failed", exc_info=True)


async def payment_notice(message: Message, order: dict) -> str:
    """One message per payment: who, what, how much, first purchase or a renewal."""
    user = message.from_user
    name = html.escape(user.full_name or "Без имени")
    tag = f" @{html.escape(user.username)}" if user.username else ""
    paid = await database.count_paid_orders(order["user_id"])
    kind = "первая оплата" if paid <= 1 else f"продление, {paid}-я оплата"
    return (
        f"💰 <b>Оплата {order['total_amount']} ⭐</b>\n\n"
        f"{name}{tag} · <code>{order['user_id']}</code>\n"
        f"Тариф «{html.escape(config.plan_title(order['tariff_plan']))}» на {order['days']} дн., "
        f"до {database.format_subscription_date(order['subscription_after'])}\n"
        f"{kind.capitalize()} · заказ <code>{order['id']}</code>"
    )


@router.message(Command("paysupport"))
@router.callback_query(F.data == "payment_support")
async def payment_support(event, state: FSMContext):
    from handlers.support import SupportStates
    user = event.from_user
    orders = await billing.list_orders(user.id)
    text = f"{ui.e('support')} <b>Оплата и возврат</b>\n\n{ui.e('mail')} {legal_docs.SUPPORT_EMAIL}\n\n"
    if orders:
        text += "<b>Последние платежи:</b>\n" + "\n".join(
            f"• #{o['id']} · {config.plan_title(o['tariff_plan'])} · {o['total_amount']} ⭐ · "
            f"{'возвращён' if o['status'] == 'refunded' else 'возврат обрабатывается' if o['status'] == 'refunding' else 'оплачен'}"
            for o in orders
        ) + "\n\n"
    text += "Опишите проблему или запрос возврата следующим сообщением, укажите номер заказа, если знаете. Ответим прямо здесь."
    await state.set_state(SupportStates.waiting_for_user_message)
    await state.update_data(payment_support=True)
    message = event.message if isinstance(event, CallbackQuery) else event
    await message.answer(text, parse_mode="HTML", reply_markup=ui.kb(
        [ui.btn("Отмена", "cancel", callback_data="support_cancel")],
    ))
    if isinstance(event, CallbackQuery):
        await event.answer()


@router.callback_query(F.data.startswith("admin_payments:"))
async def admin_payments(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    try:
        user_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("Некорректный пользователь", show_alert=True)
        return
    orders = await billing.list_orders(user_id)
    lines = [f"⭐ <b>ПЛАТЕЖИ ПОЛЬЗОВАТЕЛЯ {user_id}</b>"]
    buttons = []
    for order in orders:
        lines.append(f"#{order['id']} · {config.plan_title(order['tariff_plan'])} · {order['total_amount']} ⭐ · {order['status']}"
                     + (" · ⚠️ сверить подписку вручную" if order["refund_needs_review"] else ""))
        if order["status"] in ("paid", "refunding"):
            buttons.append([InlineKeyboardButton(text=f"↩️ Вернуть #{order['id']} ({order['total_amount']} ⭐)", callback_data=f"admin_refund_prompt:{order['id']}")])
    if not orders:
        lines.append("Подтверждённых платежей пока нет.")
    buttons.append([InlineKeyboardButton(text="« Карточка пользователя", callback_data=f"admin_user:{user_id}:0")])
    await call.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("admin_refund_prompt:"))
async def refund_prompt(call: CallbackQuery):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    try:
        order = await billing.get_order(order_id=int(call.data.split(":")[1]))
    except (ValueError, IndexError):
        order = None
    if not order or not order["telegram_payment_charge_id"] or order["status"] == "refunded":
        await call.answer("Платёж не найден или уже возвращён", show_alert=True)
        return
    await call.message.edit_text(
        f"↩️ Вернуть <b>{order['total_amount']} ⭐</b> по заказу #{order['id']} пользователю {order['user_id']}?\n\n"
        "Оплаченное этим платежом продление будет отменено. Если после него были другие платежи или ручные изменения, "
        "система не перезапишет их: потребуется сверить срок подписки вручную.", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить возврат Stars", callback_data=f"admin_refund:{order['id']}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_payments:{order['user_id']}")],
        ]),
    )
    await call.answer()


@router.callback_query(F.data.startswith("admin_refund:"))
async def refund_payment(call: CallbackQuery, bot: Bot):
    if not config.is_admin(call.from_user.id):
        await call.answer("У вас нет прав администратора", show_alert=True)
        return
    try:
        order = await billing.begin_refund(int(call.data.split(":")[1]), call.from_user.id)
    except (ValueError, IndexError) as exc:
        await call.answer(str(exc), show_alert=True)
        return
    if not order["request"]:
        await call.answer("Платёж уже возвращён", show_alert=True)
        return
    await call.answer("Выполняю возврат через Telegram")
    try:
        if not await bot.refund_star_payment(user_id=order["user_id"], telegram_payment_charge_id=order["telegram_payment_charge_id"]):
            await billing.cancel_refund(order["id"])
            await call.message.answer("Telegram не подтвердил возврат. Подписка не изменена.")
            return
    except TelegramBadRequest as exc:
        if "CHARGE_ALREADY_REFUNDED" not in str(exc).upper():
            await billing.cancel_refund(order["id"])
            await call.message.answer("Telegram отклонил возврат. Проверьте платёж и баланс Stars; подписка не изменена.")
            return
    except Exception as exc:
        logger.warning("Stars refund ambiguous: order=%s error=%s", order["id"], type(exc).__name__)
        await call.message.answer("Ответ Telegram не получен. Подписка пока не изменена; повторите сверку через 5 минут.")
        return
    result = await billing.complete_refund(order["telegram_payment_charge_id"])
    await database.log_admin_action(call.from_user.id, order["user_id"], "refund", f"заказ #{order['id']}, {order['total_amount']} ⭐")
    await call.message.answer(f"✅ Возврат {order['total_amount']} ⭐ подтверждён."
                              + (" ⚠️ После платежа подписка менялась: сверить срок вручную в карточке пользователя." if result["refund_needs_review"] else " Продление по этому платежу отменено."))
    try:
        await bot.send_message(order["user_id"], f"↩️ Заказ #{order['id']}: Telegram подтвердил возврат {order['total_amount']} ⭐. Вопросы: /paysupport.")
    except Exception:
        logger.warning("Refund user notification failed: order=%s", order["id"])


@router.message(F.refunded_payment)
async def refunded_payment(message: Message, bot: Bot):
    payment = message.refunded_payment
    order = await billing.get_order(charge_id=payment.telegram_payment_charge_id)
    if not order or payment.currency != "XTR" or payment.total_amount != order["total_amount"] or payment.invoice_payload != order["payload"]:
        await notify_admins(bot, "⚠️ Возврат Telegram не совпадает с записью платежа. Нужна ручная сверка.", message.from_user.id)
        return
    result = await billing.complete_refund(payment.telegram_payment_charge_id)
    if result["applied"] and result["refund_needs_review"]:
        await notify_admins(bot, f"⚠️ Заказ #{order['id']} возвращён; подписка менялась после оплаты. Сверьте срок вручную.", order["user_id"])