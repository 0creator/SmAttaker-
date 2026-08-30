"""
SmAttaker — Signals Handler
Display active trading signals in a clean, professional format.

v45.4.10: cards are rendered by backend/utils/signal_format.py — the
SAME builder the broadcast service uses (one source of truth):
  - PRO header with direction color + FULL crypto pair (FARTCOIN/USDT)
  - Entry / SL / TP with class-aware decimals + consistent (±%) signs
  - AI confidence line + slim ▰▱ professional progress bar
  - NO Model Quality / WR line — removed permanently (unverifiable data)
  - NO copy button (user rejected it) — the message text itself is what
    Telegram's long-press → Copy takes
  - Track Trade + Live Mini App buttons
"""
import logging
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import select

from backend.database import async_session_factory
from backend.models.user import User
from backend.models.signal import Signal, SignalStatus
from backend.models.trade import Trade

from backend.bot.handlers.menu import register_callback
from backend.bot.utils.safe_edit import safe_edit_message
from backend.utils.signal_format import (
    build_signal_card,
    fmt_price as _fmt_price,
    fmt_pct as _fmt_pct,
    fmt_candle_smart as _fmt_candle_time,
    STRATEGY_DISPLAY_NAME,
)


# ── v45.4.7: formatters (_fmt_price / _fmt_pct / _fmt_candle_time) are
# imported from backend.utils.signal_format — one implementation shared
# with the broadcast service, so the two can never drift apart again.


async def signals_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /signals — show active signals."""
    user = update.effective_user
    if not user:
        return

    # Show a "typing" indicator so the user knows the bot is working
    try:
        await context.bot.send_chat_action(chat_id=user.id, action="typing")
    except Exception:
        pass

    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.telegram_id == user.id))
        db_user = result.scalar_one_or_none()
        if not db_user:
            await update.message.reply_text("👋 Please /start first to create your account.")
            return

        # Banned users can't view signals
        if db_user.is_banned:
            await update.message.reply_text("⛔ Your account has been banned.")
            return

        # Get active signals — MUST filter by expires_at too, otherwise
        # signals whose monitor tick lagged (e.g. price fetch failing
        # for a stuck symbol like BAC on Yahoo) haunt the user's
        # /signals view forever even after their lifetime is up.
        now_utc = datetime.now(timezone.utc)
        sig_result = await db.execute(
            select(Signal)
            .where(
                Signal.status == SignalStatus.ACTIVE,
                Signal.expires_at > now_utc,
            )
            .order_by(Signal.created_at.desc())
            .limit(10)
        )
        signals = sig_result.scalars().all()

    is_ar = db_user.language == "ar"
    t = lambda en, ar: ar if is_ar else en

    if not signals:
        no_signals_en = "New signals will appear here automatically.\n\nUse /login to open the web dashboard where signals auto-refresh every 60s."
        no_signals_ar = "الإشارات الجديدة ستظهر هنا تلقائياً.\n\nاستخدم /login لفتح لوحة الويب حيث تتحدث الإشارات كل 60 ثانية."
        await update.message.reply_text(
            f"📡 *{t('No Active Signals', 'لا توجد إشارات نشطة')}*\n\n"
            f"_{t(no_signals_en, no_signals_ar)}_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(t("🔙 Back", "🔙 رجوع"), callback_data="menu:main")
            ]]),
        )
        return

    # Send a clean header with just the count
    count_text = (
        f"📡 *{t('Active Signals', 'الإشارات النشطة')}* — {len(signals)}"
    )
    await update.message.reply_text(count_text, parse_mode="Markdown")

    # Send each signal as a clean card
    for signal in signals:
        await _send_signal_card(update, context, signal, db_user)


async def _send_signal_card(update, context, signal: Signal, db_user: User):
    """Format and send a single signal card — PRO format (v45.4.10).

    Text comes from backend.utils.signal_format.build_signal_card() —
    the same builder the live broadcast uses, so /signals cards and
    broadcast cards are pixel-identical. No copy button (user rejected
    it); the message text is natively copyable.
    """
    is_ar = db_user.language == "ar"
    t = lambda en, ar: ar if is_ar else en

    text = build_signal_card(signal, is_ar=is_ar)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                t("📥 Track Trade", "📥 تتبع الصفقة"),
                callback_data=f"trade:open:auto:{signal.id}",
            ),
            InlineKeyboardButton(
                t("📊 Live Mini App", "📊 تطبيق مباشر"),
                callback_data=f"signal:miniapp:{signal.id}",
            ),
        ],
    ])

    if hasattr(update, 'message') and update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


@register_callback("trade")
async def trade_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str):
    """Handle 'Track Trade' button — creates a paper/demo/real trade
    from a signal, then confirms to the user.

    ⚠️ v45.4.2 FIX: the old payload parsing was
        `action, acc_type, signal_id = payload.split(":", 2)`
    which raised ValueError (silent crash) if the payload had fewer than
    3 parts. With the callback_router's error handler now showing a
    useful "Something went wrong" alert, a payload split failure was
    one of the most common causes of "Track Trade button doesn't work".

    Now: defensive parsing with clear error feedback to the user.
    """
    query = update.callback_query
    # Always answer the query first so the button doesn't appear stuck
    # even if we encounter an error below. The user sees a brief "..."
    # loading indicator, then this answer clears it.
    await query.answer()

    user = update.effective_user
    if not user:
        return

    # ── Defensive payload parsing ──
    # Expected format: "open:auto:<signal_id>" (3 parts, maxsplit=2 →
    # returns ['open', 'auto', '<signal_id>']). But if anything is off
    # (malformed callback_data from an old bot version, partial click,
    # etc.), we want a clear error message, not a silent crash.
    #
    # NOTE: we can't use the `t()` translator here yet — it needs
    # db_user.language, which we haven't fetched yet at this point.
    # Use bilingual plain-text for any error before db_user is loaded.
    try:
        parts = payload.split(":", 2)
        if len(parts) < 3:
            logger.error(
                f"trade_callback: malformed payload received: {payload!r} "
                f"(expected 'open:auto:<signal_id>', got {len(parts)} parts)"
            )
            await safe_edit_message(query,
                "⚠️ *Invalid button payload.*\n\n"
                "The button callback data is malformed — this can happen if you're "
                "using an old bot message. Please send /signals to get a fresh signal "
                "card with working buttons.\n\n"
                "⚠️ *بيانات الزر غير صالحة.*\n\n"
                "بيانات الزر المضغوط تالفة — هذا قد يحدث عند استخدام رسالة قديمة. "
                "الرجاء إرسال /signals للحصول على بطاقة إشارة جديدة بأزرار عاملة.",
                parse_mode="Markdown",
            )
            return
        action, acc_type, signal_id = parts[0], parts[1], parts[2]
    except Exception as e:
        logger.error(f"trade_callback: payload parse exception: {e}", exc_info=True)
        await safe_edit_message(query,
            "⚠️ Could not parse this button's action.\n"
            "⚠️ تعذّر تحليل إجراء هذا الزر.",
        )
        return

    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.telegram_id == user.id))
        db_user = result.scalar_one_or_none()
        if not db_user:
            # Use plain English/Arabic fallback since we don't have a
            # language hint yet (db_user is None).
            await safe_edit_message(
                query,
                "Please /start first to create your account.\n"
                "الرجاء إرسال /start أولاً لإنشاء حسابك.",
            )
            return

    is_ar = db_user.language == "ar"
    t = lambda en, ar: ar if is_ar else en

    # "auto" = use the user's default_account_type (paper/demo/real)
    if acc_type == "auto":
        acc_type = db_user.default_account_type or "paper"

    if action == "open":
        # Create a trade
        async with async_session_factory() as db:
            # Fetch the signal
            sig_res = await db.execute(select(Signal).where(Signal.id == signal_id))
            signal = sig_res.scalar_one_or_none()
            if not signal:
                await safe_edit_message(query, t("Signal not found.", "الإشارة غير موجودة."))
                return

            # ── v57 FIX: fresh entry price at CONFIRMATION time ──────
            # This used to just copy signal.entry_price — the price at
            # the moment the signal was GENERATED (up to ~1h + however
            # long the user took to notice the Telegram message and tap
            # this button). Every late tap silently recorded a trade at
            # a price the market had already moved away from — exactly
            # the "bad entry" the user is describing. Now: fetch the
            # REAL current price at the moment of confirmation, and use
            # THAT as the trade's entry_price. entry_time was already
            # `datetime.now()` (correct) — the bug was entry_PRICE not
            # matching entry_TIME.
            from backend.services.price_feed import fetch_live_price
            live_price = await fetch_live_price(signal.symbol, signal.asset_class)
            entry_price = live_price if live_price is not None else signal.entry_price
            price_is_stale = live_price is None
            drift_note = ""

            # ── Safety check: has the market already moved too far? ──
            # If the fresh price is already at or beyond the stop-loss
            # level, or has drifted far outside the signal's intended
            # entry zone, opening "as if" this were still a fresh signal
            # would record a trade that's already lost or has a badly
            # skewed risk/reward — worse than missing it entirely. Block
            # and tell the user plainly, instead of quietly creating a
            # doomed trade. This is the honest version of "never miss
            # the trade": protect the user from a bad one, don't fake a
            # good one.
            if not price_is_stale:
                sl = float(signal.stop_loss)
                is_long = signal.direction == "long"
                sl_already_hit = (entry_price <= sl) if is_long else (entry_price >= sl)
                if sl_already_hit:
                    await safe_edit_message(query, t(
                        f"⛔ *Signal Expired*\n\n{signal.symbol} has already moved past its "
                        f"stop-loss level since this signal was generated (now: {_fmt_price(entry_price)}, "
                        f"SL: {_fmt_price(sl)}). Opening now would start the trade already at a loss — "
                        f"skipping it instead.",
                        f"⛔ *الإشارة منتهية*\n\n{signal.symbol} تجاوز مستوى وقف الخسارة منذ توليد "
                        f"الإشارة (الآن: {_fmt_price(entry_price)}، الوقف: {_fmt_price(sl)}). فتح الصفقة "
                        f"الآن يعني بدء بخسارة مباشرة — تم تخطيها بدلاً من ذلك.",
                    ), parse_mode="Markdown")
                    return

                # Drift warning (not a block): price moved noticeably but
                # not fatally — tell the user so they enter with open eyes.
                drift_pct = abs(entry_price - signal.entry_price) / signal.entry_price * 100 if signal.entry_price else 0
                if drift_pct >= 0.5:
                    drift_note = t(
                        f"\n⚠️ _Price has moved {drift_pct:.2f}% since this signal was generated — "
                        f"entering at the current price, not the original one._",
                        f"\n⚠️ _تحرك السعر {drift_pct:.2f}% منذ توليد الإشارة — الدخول بالسعر الحالي، مش الأصلي._",
                    )

            # Determine position size based on account type
            # PAPER: virtual $10,000 with 2% risk per trade (proper sizing)
            # DEMO: same logic as paper but separate from paper balance
            # REAL: default $100 until user configures risk settings; the
            #       trade_executor reads real risk settings + real balance
            #       from the connected exchange when it's actually invoked.
            if acc_type == "paper":
                paper_balance = float(db_user.paper_balance or 10000.0)
                risk_pct = 0.02  # 2% risk per trade (conservative)
                risk_amount_usd = paper_balance * risk_pct
                stop_dist_pct = abs(signal.stop_loss_pct / 100.0) if signal.stop_loss_pct else 0.02
                if stop_dist_pct <= 0:
                    stop_dist_pct = 0.02
                position_size_usd = min(risk_amount_usd / stop_dist_pct, paper_balance * 0.5)
            elif acc_type == "demo":
                # Demo: virtual $10,000 with 2% risk (same sizing logic as
                # paper, but tracked separately as "demo" trades).
                demo_balance = 10000.0
                risk_pct = 0.02
                risk_amount_usd = demo_balance * risk_pct
                stop_dist_pct = abs(signal.stop_loss_pct / 100.0) if signal.stop_loss_pct else 0.02
                if stop_dist_pct <= 0:
                    stop_dist_pct = 0.02
                position_size_usd = min(risk_amount_usd / stop_dist_pct, demo_balance * 0.5)
            else:  # real
                position_size_usd = 100.0  # default real size (real sizing happens in trade_executor)

            # Create the trade
            trade = Trade(
                user_id=db_user.id,
                signal_id=signal.id,
                account_type=acc_type,
                symbol=signal.symbol,
                exchange=signal.exchange,
                strategy=signal.strategy_type,
                asset_class=signal.asset_class,
                direction=signal.direction,
                entry_price=entry_price,
                entry_time=datetime.now(timezone.utc),
                stop_loss=signal.stop_loss,
                stop_loss_pct=signal.stop_loss_pct,
                take_profit_levels=signal.take_profit_levels,
                position_size=position_size_usd / entry_price if entry_price else 0,
                position_size_usd=position_size_usd,
                status="active",
            )
            db.add(trade)
            await db.commit()
            await db.refresh(trade)

            # ── v57 FIX: actually EXECUTE real trades ──────────────
            # Before this, "real" account trades were only ever recorded
            # in the database — trade_executor.execute_trade_for_user()
            # (the function that places a genuine order via CCXT/MetaApi)
            # was never called from anywhere. A user selecting "Real"
            # believed they had a live position on their exchange; they
            # did not. This closes that gap: a real order is placed HERE,
            # and the result (including the ACTUAL exchange fill price,
            # when available) is reconciled back onto the trade record.
            execution_note = ""
            if acc_type == "real":
                from backend.services.trade_executor import execute_trade_for_user
                exec_result = await execute_trade_for_user(
                    user_id=str(db_user.id), signal_id=str(signal.id), account_type="real",
                )
                if not exec_result.get("success"):
                    trade.status = "cancelled"
                    await db.commit()
                    await safe_edit_message(query, t(
                        f"⛔ *Real Order Failed*\n\n{signal.symbol} — the live order was rejected: "
                        f"{exec_result.get('error', 'unknown error')}\n\nNo position was opened; "
                        f"nothing was recorded.",
                        f"⛔ *فشل تنفيذ الأمر الحقيقي*\n\n{signal.symbol} — الأمر الحي رُفض: "
                        f"{exec_result.get('error', 'خطأ غير معروف')}\n\nلم تُفتح أي صفقة، ولم يُسجَّل شيء.",
                    ), parse_mode="Markdown")
                    return

                # ⚠️ MT5's connector has a "stub mode" that returns
                # success=True with bridge_required=True when MetaApi
                # credentials aren't configured for this connection — it
                # recorded the order shape but placed NOTHING on a real
                # account. Claiming "live order placed" here would be
                # exactly the false confidence this whole fix exists to
                # remove. Tell the user the truth instead.
                if exec_result.get("bridge_required"):
                    execution_note = t(
                        "\n⚠️ _No live bridge configured for this connection — this trade is "
                        "TRACKED ONLY, not executed on a real account. Connect via MetaApi "
                        "(Settings → Connections) for real execution._",
                        "\n⚠️ _لا يوجد جسر تنفيذ حي مفعّل لهذا الاتصال — هذه الصفقة للتتبع فقط، "
                        "غير منفّذة على حساب حقيقي. اربط عبر MetaApi (الإعدادات ← الاتصالات) "
                        "للتنفيذ الحقيقي._",
                    )
                    order_obj = {}
                else:
                    order_obj = exec_result.get("order") if isinstance(exec_result.get("order"), dict) else {}
                    execution_note = t(
                        "\n✅ _Live order placed on your connected exchange._",
                        "\n✅ _تم تنفيذ أمر حقيقي على منصتك المتصلة._",
                    )
                # CCXT market orders report the real fill under `average`
                # — `price` is the LIMIT price and is None for market
                # orders, so checking `price` first would silently keep
                # the stale entry_price on every real trade. MT5's own
                # response uses `price` directly for the real fill, so
                # the fallback chain still resolves correctly there.
                fill_price = order_obj.get("average") or order_obj.get("price")
                if not fill_price:
                    filled = order_obj.get("filled") or 0
                    cost = order_obj.get("cost") or 0
                    if filled and cost:
                        fill_price = cost / filled

                # trade_executor computes its OWN risk-based position size
                # from the user's real balance + risk settings (the "100.0
                # placeholder" above was only ever a display/DB stand-in
                # until execution). Sync the real value back so the trade
                # record and the confirmation message both show what was
                # actually sized on the exchange, not the placeholder.
                real_size_usd = exec_result.get("position_size_usd")
                if real_size_usd:
                    position_size_usd = float(real_size_usd)
                    trade.position_size_usd = position_size_usd

                if fill_price:
                    trade.entry_price = float(fill_price)
                    trade.position_size = position_size_usd / float(fill_price) if float(fill_price) else trade.position_size
                    await db.commit()
                elif real_size_usd:
                    await db.commit()

        acc_labels = {
            "paper": t("PAPER", "ورقي"),
            "demo": t("DEMO", "تجريبي"),
            "real": t("REAL", "حقيقي"),
        }
        acc_label = acc_labels.get(acc_type, acc_type.upper())

        await safe_edit_message(query,
            t(
                f"✅ *Trade Tracked* ({acc_label})\n\n"
                f"{signal.symbol} · {signal.direction.upper()}\n"
                f"Entry: {_fmt_price(trade.entry_price)}\n"
                f"Size: ${position_size_usd:.2f}\n\n"
                f"_I'll notify you when SL or TP is hit, or after 8 hours._"
                f"{drift_note}{execution_note}",
                f"✅ *تم تتبع الصفقة* ({acc_label})\n\n"
                f"{signal.symbol} · {signal.direction.upper()}\n"
                f"الدخول: {_fmt_price(trade.entry_price)}\n"
                f"الحجم: ${position_size_usd:.2f}\n\n"
                f"_سأخطرك عند ضرب الوقف أو الهدف، أو بعد 8 ساعات._"
                f"{drift_note}{execution_note}"
            ),
            parse_mode="Markdown"
        )


@register_callback("signal")
async def signal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str):
    """Handle signal-related button callbacks.

    Supports:
      - 'detail:<signal_id>'   → show detailed signal info inline
      - 'miniapp:<signal_id>' → open the Telegram Mini App (chart + AI)
      - 'dismiss:<signal_id>'  → delete the message
    """
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.telegram_id == user.id))
        db_user = result.scalar_one_or_none()
        if not db_user:
            return

    is_ar = db_user.language == "ar"
    t = lambda en, ar: ar if is_ar else en

    # Defensive parsing — payload format varies by action
    try:
        parts = payload.split(":", 1)
        action = parts[0] if parts else ""
        signal_id = parts[1] if len(parts) > 1 else ""
    except Exception as e:
        logger.error(f"signal_callback: payload parse failed: {e}", exc_info=True)
        await safe_edit_message(query, t(
            "⚠️ Invalid button payload.",
            "⚠️ بيانات الزر غير صالحة.",
        ))
        return

    if action == "dismiss":
        await query.delete_message()
    elif action == "miniapp":
        # ── Open the Telegram Mini App (Live chart + AI + trade setup) ──
        # We use a URL button instead of a callback for this — the
        # Mini App needs to open a web app URL, which can't be done
        # from a callback handler. So we send a NEW message with a
        # URL button that opens the Mini App.
        if not signal_id:
            await safe_edit_message(query, t(
                "⚠️ Signal ID missing.",
                "⚠️ معرّف الإشارة مفقود.",
            ))
            return
        # Build the Mini App URL — the backend's /miniapp route renders
        # the TradingView chart + AI gauges + trade setup.
        from backend.config import settings
        miniapp_url = (
            f"{settings.RENDER_EXTERNAL_URL.rstrip('/')}/miniapp"
            f"?signal_id={signal_id}&user_id={db_user.id}"
        )
        # Send a NEW message (not edit) with a URL button — Telegram
        # only allows web_app buttons on inline keyboards via URL buttons.
        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            await query.message.reply_text(
                t(
                    "📊 *Live Signal Mini App*\n\nTap below to open the interactive chart, AI gauges, and trade setup.",
                    "📊 *تطبيق الإشارة المباشر*\n\nاضغط لفتح الشارت التفاعلي ومؤشرات الذكاء الاصطناعي وتفاصيل الصفقة.",
                ),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                    t("🚀 Open Mini App", "🚀 افتح التطبيق"),
                    url=miniapp_url,
                )]]),
            )
        except Exception as e:
            logger.error(f"Failed to send Mini App link: {e}", exc_info=True)
            await safe_edit_message(query, t(
                "⚠️ Could not open the Mini App. Please try again.",
                "⚠️ تعذّر فتح التطبيق. الرجاء المحاولة مرة أخرى.",
            ))
    elif action == "detail":
        async with async_session_factory() as db:
            sig_res = await db.execute(select(Signal).where(Signal.id == signal_id))
            signal = sig_res.scalar_one_or_none()
        if signal:
            # Detailed view — v45.4.9: honest clean design mirroring the
            # broadcast card: slim ▰▱ progress bar, NO model-quality/WR
            # line (removed permanently — unverifiable data), full pair.
            confidence = signal.confidence_score or 0
            rr = signal.risk_reward_ratio
            candle_time_str = _fmt_candle_time(signal.entry_time)

            # Technical snapshot
            tech = signal.technical_snapshot or {}
            rsi_val = tech.get("rsi")
            atr_pct_val = tech.get("atr_pct")

            from backend.utils.signal_format import (
                _conviction, _display_symbol, confidence_bar, fmt_price as _cls_price,
            )
            conf_emoji, conf_label = _conviction(confidence, is_ar)
            cls_for_price = (signal.asset_class or "").lower()
            display_sym = _display_symbol(signal.symbol or "—", cls_for_price)

            text = (
                f"📊 *{display_sym}* — {signal.direction.upper()}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"*{t('Trade Setup', 'إعداد الصفقة')}*\n"
                f"• {t('Entry', 'الدخول')}: {_cls_price(signal.entry_price, cls_for_price)}\n"
                f"• {t('Stop Loss', 'وقف الخسارة')}: {_cls_price(signal.stop_loss, cls_for_price)}\n"
                f"• {t('Take Profit', 'الهدف')}: {_cls_price(signal.take_profit_levels[0].get('price') if signal.take_profit_levels else None, cls_for_price)}\n"
                f"• {t('R:R', 'عائد/مخاطرة')}: 1:{rr:.1f}\n"
                f"• {t('Candle Time', 'وقت الشمعة')}: {candle_time_str}\n\n"
                f"*{t('ML Insight', 'رؤية الذكاء الاصطناعي')}*\n"
                f"• {t('AI Confidence', 'ثقة الذكاء الاصطناعي')}: *{confidence:.1f}%* {conf_emoji} {conf_label}\n"
                f"{confidence_bar(confidence)}\n"
            )
            text += (
                f"• {t('Asset Class', 'الفئة')}: {signal.asset_class.title()}\n"
                f"• {t('Strategy', 'الاستراتيجية')}: {STRATEGY_DISPLAY_NAME}\n"
            )

            # Technical section (only if we have data)
            if rsi_val is not None or atr_pct_val is not None:
                text += f"\n*{t('Technicals', 'المؤشرات الفنية')}*\n"
                if rsi_val is not None:
                    text += f"• RSI: {rsi_val:.1f}\n"
                if atr_pct_val is not None:
                    text += f"• ATR%: {atr_pct_val:.4f}\n"

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        t("📊 Live Mini App", "📊 تطبيق مباشر"),
                        callback_data=f"signal:miniapp:{signal.id}",
                    ),
                    InlineKeyboardButton(t("🔙 Back", "🔙 رجوع"), callback_data="menu:main"),
                ],
            ])
            await safe_edit_message(query, text, parse_mode="Markdown", reply_markup=keyboard)
