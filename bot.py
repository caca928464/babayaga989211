# bot.py
# Dépendances: aiogram>=3.6, python-dotenv, SQLAlchemy>=2.0, aiosqlite
# .env: BOT_TOKEN=..., ADMIN_REVIEW_CHAT_ID=..., DATABASE_URL=sqlite+aiosqlite:///bot.db

import os
import asyncio
from datetime import datetime
from typing import Optional, Literal

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile, WebAppInfo
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties

from sqlalchemy import (
    String, Integer, DateTime, Enum, select, UniqueConstraint
)
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from dotenv import load_dotenv
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_REVIEW_CHAT_ID = int(os.getenv("ADMIN_REVIEW_CHAT_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///bot.db")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN manquant")

# ---------- DB ----------
class Base(DeclarativeBase): ...

OfferType = Literal["BEGINNER30", "PRO200"]

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    stake_pseudo: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    twitter_handle: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class Request(Base):
    __tablename__ = "requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(Integer, index=True)
    offer_type: Mapped[str] = mapped_column(Enum("BEGINNER30","PRO200", name="offer_type"))
    deposit_amount: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(Enum("PENDING","CLOSED", name="req_status"), default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("tg_id", "offer_type", name="uq_one_per_offer"),)

engine = create_async_engine(DATABASE_URL, echo=False)
Session: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# ---------- FSM ----------
class Flow(StatesGroup):
    choose_offer = State()
    ask_has_account = State()
    ask_stake_pseudo = State()
    ask_deposit_done = State()
    ask_deposit_amount = State()     # PRO200
    ask_twitter = State()            # PRO200
    idle = State()
    # Edition d'infos
    edit_stake = State()
    edit_amount = State()
    edit_twitter = State()

# ---------- UI ----------
def main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="🎁 Accéder aux bonus", callback_data="menu_bonus")
    kb.button(text="❓ J'ai besoin d'aide", web_app=WebAppInfo(url="https://sora-europe.store/STAKE/aide.html"))
    kb.button(text="⭐ Découvrir les avantages de Stake", web_app=WebAppInfo(url="https://sora-europe.store/STAKE/index_partenaire.html"))
    kb.adjust(1)
    return kb.as_markup()

def bonus_choice_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="Débutant : 30€ offerts 🎁", callback_data="offer_BEGINNER30")
    kb.button(text="Aguerri : Dépôt triplé (200€ min) 💥", callback_data="offer_PRO200")
    kb.adjust(1)
    return kb.as_markup()

def yes_no_kb(prefix: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Oui", callback_data=f"{prefix}:yes")
    kb.button(text="❌ Non", callback_data=f"{prefix}:no")
    kb.adjust(1)
    return kb.as_markup()

def resume_or_help_kb(offer_code: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Reprendre la procédure", callback_data=f"resume_deposit_{offer_code}")
    kb.button(text="❓ J'ai besoin d'aide pour dépôt", web_app=WebAppInfo(url="https://sora-europe.store//STAKE/aide.html?item=depot"))
    kb.adjust(1)
    return kb.as_markup()

def after_submit_kb(allow_other: bool, other_offer: Optional[str]):
    kb = InlineKeyboardBuilder()
    if allow_other and other_offer:
        label = "Profiter du bonus spécial 🎁" if other_offer=="BEGINNER30" else "Profiter du bonus 200% 🎁"
        kb.button(text=label, callback_data=f"offer_{other_offer}")
    kb.button(text="📝 Modifier mes informations", callback_data="edit_info")
    kb.adjust(1)
    return kb.as_markup()

# ---------- Helpers ----------
async def user_get_or_create(session: AsyncSession, msg_or_cb) -> User:
    tg_id = msg_or_cb.from_user.id
    username = getattr(msg_or_cb.from_user, "username", None)
    r = await session.execute(select(User).where(User.tg_id == tg_id))
    u = r.scalar_one_or_none()
    if not u:
        u = User(tg_id=tg_id, username=username or None)
        session.add(u)
        await session.commit()
    return u

async def has_request(session: AsyncSession, tg_id: int, offer: OfferType) -> bool:
    r = await session.execute(
        select(Request).where(Request.tg_id==tg_id, Request.offer_type==offer)
    )
    return r.scalar_one_or_none() is not None

async def create_or_get_request(session: AsyncSession, tg_id: int, offer: OfferType) -> Request:
    r = await session.execute(
        select(Request).where(Request.tg_id==tg_id, Request.offer_type==offer)
    )
    req = r.scalar_one_or_none()
    if not req:
        req = Request(tg_id=tg_id, offer_type=offer, status="PENDING")
        session.add(req)
        await session.commit()
    return req

def other_offer_name(offer: OfferType) -> OfferType:
    return "PRO200" if offer=="BEGINNER30" else "BEGINNER30"

async def send_review(
    bot: Bot,
    tg_username: Optional[str],
    tg_id: int,
    offer_type: str,
    stake_pseudo: Optional[str],
    deposit_amount: Optional[int] = None,
    twitter_handle: Optional[str] = None,
    updated: bool = False
):
    if ADMIN_REVIEW_CHAT_ID == 0:
        return
    if updated:
        lines = [
            f"📝 L'utilisateur @{tg_username or tg_id} a mis à jour ses détails.",
            "Nouveaux détails :"
        ]
    else:
        lines = [
            "🆕 Nouvelle candidature",
            f"• Offer: {'30€ offerts' if offer_type=='BEGINNER30' else 'Bonus 200%'}"
        ]
    lines.append(f"• TG: @{tg_username}" if tg_username else f"• TG id: {tg_id}")
    lines.append(f"• Pseudo Stake: {stake_pseudo or 'non communiqué'}")
    if offer_type == "PRO200":
        lines.append(f"• Dépôt déclaré: {deposit_amount or '?'} €")
        lines.append(f"• Twitter: {twitter_handle or 'non communiqué'}")
    await bot.send_message(ADMIN_REVIEW_CHAT_ID, "\n".join(lines))

async def has_done_other(tg_id: int, current_offer: OfferType) -> bool:
    other = other_offer_name(current_offer)
    async with Session() as session:
        r = await session.execute(select(Request).where(Request.tg_id==tg_id, Request.offer_type==other))
        return r.scalar_one_or_none() is not None

# ---------- Router ----------
rt = Router()

@rt.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    async with Session() as session:
        await user_get_or_create(session, msg)
    await msg.answer_photo(
        photo=FSInputFile("banner.jpg"),
        caption="",
        reply_markup=main_menu()
    )

@rt.callback_query(F.data=="menu_bonus")
async def cb_menu_bonus(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer(
        "🎰 Bienvenue sur le bot telegram de La Menace !\n\n"
        f"Salut  {cb.from_user.username or 'joueur'} !\n\n"
        "Prêt à tenter ta chance et à vivre l'expérience ultime du casino en ligne ? 💰🔥\n\n"
        "Avant de commencer, dis-moi quel type de joueur de casino tu es :\n\n"
        "Tu auras néanmoins la possibilité d'avoir accès aux 2 bonus.",
        reply_markup=bonus_choice_kb()
    )
    await state.set_state(Flow.choose_offer)
    await cb.answer()

@rt.callback_query(F.data.startswith("offer_"))
async def cb_offer(cb: CallbackQuery, state: FSMContext):
    offer: OfferType = cb.data.split("_",1)[1]  # BEGINNER30 | PRO200
    async with Session() as session:
        if await has_request(session, cb.from_user.id, offer):
            await cb.message.answer("Tu as déjà une demande en cours, attends que celle-ci soit traitée avant de faire une nouvelle demande ! 😎")
            await cb.answer()
            return
    await state.update_data(offer=offer)
    await cb.message.answer("As-tu déjà créé ton compte Stake ? 🎉", reply_markup=yes_no_kb("hasacc"))
    await state.set_state(Flow.ask_has_account)
    await cb.answer()

@rt.callback_query(F.data.startswith("hasacc:"))
async def cb_has_account(cb: CallbackQuery, state: FSMContext):
    _, choice = cb.data.split(":")
    data = await state.get_data()
    offer: OfferType = data["offer"]
    if choice == "no":
        await cb.message.answer(
            "Crée ton compte grâce au lien ci-dessous, puis clique sur le bouton pour reprendre la procédure ! 😎\n\n"
            "👉 <a href='https://stake.bet/?c=menacebet'>Crée ton compte !</a> 👈\n\n"
            "⚠️ Si le site ne fonctionne pas, utilise un VPN (Canada, Norvège).",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❓ Tutoriel VPN", web_app=WebAppInfo(url="https://sora-europe.store/STAKE/aide.html?item=vpn"))],
                [InlineKeyboardButton(text="✅ Reprendre la procédure", callback_data=f"offer_{offer}")]
            ]), parse_mode=ParseMode.HTML
        )
        await cb.answer()
        return
    # oui → on DEMANDE le pseudo + flèche immédiatement
    await cb.message.answer("Quel est ton pseudo Stake ? 😎")
    await cb.message.answer("⬇️⁣️⁣⁣⁣⁣")
    await state.set_state(Flow.ask_stake_pseudo)
    await cb.answer()

@rt.message(Flow.ask_stake_pseudo)
async def got_pseudo(msg: Message, state: FSMContext):
    pseudo = msg.text.strip()
    await state.update_data(stake_pseudo=pseudo)
    data = await state.get_data()
    offer: OfferType = data["offer"]
    if offer == "BEGINNER30":
        await msg.answer(
            "As-tu bien effectué ton dépôt de 20€ minimum ? "
            "(Tu reçois ensuite les 30€ en cash, sans aucune conditions, ce qui t'assure de gagner au minimum 10€, même si tu perds ton dépôt 😎)",
            reply_markup=yes_no_kb("dep20")
        )
        await state.set_state(Flow.ask_deposit_done)
    else:
        await msg.answer("Combien as-tu déposé (entre 200€ et 1000€) ? 😎")
        await state.set_state(Flow.ask_deposit_amount)

@rt.callback_query(F.data.startswith("dep20:"))
async def cb_dep20(cb: CallbackQuery, state: FSMContext, bot: Bot):
    _, choice = cb.data.split(":")
    data = await state.get_data()
    pseudo = data["stake_pseudo"]
    if choice == "no":
        offer: OfferType = data["offer"]
        await cb.message.answer(
            "Effectue ton dépôt, et utilise le bouton ci-dessous pour reprendre la procédure. 😎",
            reply_markup=resume_or_help_kb(offer)
        )
        await cb.answer()
        return

    # Dépôt effectué → enregistrer BEGINNER30
    async with Session() as session:
        u = await user_get_or_create(session, cb)
        u.stake_pseudo = pseudo
        await session.commit()
        await create_or_get_request(session, cb.from_user.id, "BEGINNER30")

    # 3 messages séparés
    await cb.message.answer("Tu as sélectionné l'offre : 30€ offerts.\nMerci ! ✅")
    await cb.message.answer("Nous avons bien enregistré toutes tes réponses. Nous te recontacterons dans un court délais pour de plus amples vérifications ou pour valider l'option précédemment choisis.")
    allow_other = not await has_done_other(cb.from_user.id, "BEGINNER30")
    await cb.message.answer(
        "Cordialement,\n\nL'équipe La Menace",
        reply_markup=after_submit_kb(allow_other, other_offer_name("BEGINNER30"))
    )

    # Envoi au groupe (valeurs déjà connues)
    await send_review(
        bot=bot,
        tg_username=cb.from_user.username,
        tg_id=cb.from_user.id,
        offer_type="BEGINNER30",
        stake_pseudo=pseudo
    )
    await state.set_state(Flow.idle)
    await cb.answer()

@rt.message(Flow.ask_deposit_amount)
async def got_amount(msg: Message, state: FSMContext):
    try:
        val = int(msg.text.strip())
    except ValueError:
        await msg.answer("Entre un nombre entre 200 et 1000.")
        return
    if val < 200 or val > 1000:
        await msg.answer("Entre un nombre entre 200 et 1000.")
        return
    await state.update_data(deposit_amount=val)
    await msg.answer("Quel est ton pseudo Twitter ? 😎")
    await state.set_state(Flow.ask_twitter)

@rt.message(Flow.ask_twitter)
async def got_twitter(msg: Message, state: FSMContext, bot: Bot):
    tw = msg.text.strip()
    data = await state.get_data()
    pseudo = data["stake_pseudo"]
    amount = data["deposit_amount"]

    async with Session() as session:
        # verrou 1 demande PRO200
        if await has_request(session, msg.from_user.id, "PRO200"):
            await msg.answer("Tu as déjà une demande en cours, attends que celle-ci soit traitée avant de faire une nouvelle demande ! 😎")
            await state.set_state(Flow.idle)
            return
        u = await user_get_or_create(session, msg)
        u.stake_pseudo = pseudo
        u.twitter_handle = tw
        await session.commit()
        req = await create_or_get_request(session, msg.from_user.id, "PRO200")
        req.deposit_amount = amount
        await session.commit()

    # 3 messages séparés
    await msg.answer("Tu as sélectionné l'offre : Bonus 200%.\nMerci ! ✅")
    await msg.answer("Nous avons bien enregistré toutes tes réponses. Nous te recontacterons dans un court délais pour de plus amples vérifications ou pour valider l'option précédemment choisis.")
    allow_other = not await has_done_other(msg.from_user.id, "PRO200")
    await msg.answer(
        "Cordialement,\n\nL'équipe La Menace",
        reply_markup=after_submit_kb(allow_other, other_offer_name("PRO200"))
    )

    # Envoi au groupe (valeurs en mémoire)
    await send_review(
        bot=bot,
        tg_username=msg.from_user.username,
        tg_id=msg.from_user.id,
        offer_type="PRO200",
        stake_pseudo=pseudo,
        deposit_amount=amount,
        twitter_handle=tw
    )
    await state.set_state(Flow.idle)

@rt.callback_query(F.data.startswith("resume_deposit_"))
async def cb_resume_deposit(cb: CallbackQuery, state: FSMContext):
    """Handler pour reprendre après dépôt non effectué - ne redemande PAS le pseudo"""
    data = await state.get_data()
    offer: OfferType = data.get("offer", "BEGINNER30")
    
    # On a déjà le pseudo dans state.data["stake_pseudo"]
    # On renvoie directement la question sur le dépôt
    if offer == "BEGINNER30":
        await cb.message.answer(
            "As-tu bien effectué ton dépôt de 20€ minimum ? "
            "(Tu reçois ensuite les 30€ en cash, sans aucune conditions, ce qui t'assure de gagner au minimum 10€, même si tu perds ton dépôt 😎)",
            reply_markup=yes_no_kb("dep20")
        )
        await state.set_state(Flow.ask_deposit_done)
    else:
        # PRO200 - même logique si nécessaire
        await cb.message.answer("Combien as-tu déposé (entre 200€ et 1000€) ? 😎")
        await state.set_state(Flow.ask_deposit_amount)
    
    await cb.answer()

@rt.callback_query(F.data=="resume_flow")
async def cb_resume(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    offer: OfferType = data.get("offer","BEGINNER30")
    if offer=="BEGINNER30":
        await cb.message.answer(
            "As-tu bien effectué ton dépôt de 20€ minimum ?",
            reply_markup=yes_no_kb("dep20")
        )
        await state.set_state(Flow.ask_deposit_done)
    else:
        await cb.message.answer("As-tu déjà créé ton compte Stake ? 🎉", reply_markup=yes_no_kb("hasacc"))
        await state.set_state(Flow.ask_has_account)
    await cb.answer()

# ----- Edit info -----
@rt.callback_query(F.data=="edit_info")
async def cb_edit(cb: CallbackQuery, state: FSMContext):
    async with Session() as session:
        has_pro = await has_request(session, cb.from_user.id, "PRO200")
    if has_pro:
        await state.update_data(edit_target="PRO200")
    else:
        await state.update_data(edit_target="BEGINNER30")
    await cb.message.answer("Envoie le nouveau pseudo Stake.")
    await cb.message.answer("⬇️⁣️⁣⁣⁣⁣")
    await state.set_state(Flow.edit_stake)
    await cb.answer()

@rt.message(Flow.edit_stake)
async def edit_stake(msg: Message, state: FSMContext):
    new_pseudo = msg.text.strip()
    await state.update_data(new_stake=new_pseudo)
    data = await state.get_data()
    target = data.get("edit_target","BEGINNER30")
    if target == "PRO200":
        await msg.answer("Entre le montant du dépôt (200 à 1000).")
        await state.set_state(Flow.edit_amount)
    else:
        async with Session() as session:
            u = await user_get_or_create(session, msg)
            u.stake_pseudo = new_pseudo
            await session.commit()
        await msg.answer("Modification effectuée avec succès ! ✅\n\nMerci ! 😎")
        # push groupe recap MAJ
        bot = msg.bot
        await send_review(
            bot, msg.from_user.username, msg.from_user.id,
            "BEGINNER30", new_pseudo, updated=True
        )
        await state.set_state(Flow.idle)

@rt.message(Flow.edit_amount)
async def edit_amount(msg: Message, state: FSMContext):
    try:
        val = int(msg.text.strip())
    except ValueError:
        await msg.answer("Entre un nombre entre 200 et 1000.")
        return
    if val < 200 or val > 1000:
        await msg.answer("Entre un nombre entre 200 et 1000.")
        return
    await state.update_data(new_amount=val)
    await msg.answer("Envoie ton pseudo Twitter.")
    await state.set_state(Flow.edit_twitter)

@rt.message(Flow.edit_twitter)
async def edit_twitter(msg: Message, state: FSMContext):
    tw = msg.text.strip()
    data = await state.get_data()
    new_pseudo = data["new_stake"]
    new_amount = data["new_amount"]

    async with Session() as session:
        u = await user_get_or_create(session, msg)
        u.stake_pseudo = new_pseudo
        u.twitter_handle = tw
        await session.commit()
        r = await session.execute(select(Request).where(Request.tg_id==msg.from_user.id, Request.offer_type=="PRO200"))
        req = r.scalar_one_or_none()
        if req:
            req.deposit_amount = new_amount
            await session.commit()

    await msg.answer("Modification effectuée avec succès ! ✅\n\nMerci ! 😎")
    # push groupe recap MAJ
    bot = msg.bot
    await send_review(
        bot, msg.from_user.username, msg.from_user.id,
        "PRO200", new_pseudo, new_amount, tw, updated=True
    )
    await state.set_state(Flow.idle)

# ---------- Entrée ----------
async def main():
    await init_db()
    dp = Dispatcher()
    dp.include_router(rt)
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())