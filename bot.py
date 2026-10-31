import os
import re
import sys
import json
import random
import zipfile
import asyncio
from io import BytesIO
from datetime import datetime
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BotCommand, BotCommandScopeDefault, BufferedInputFile
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from telethon import TelegramClient
from telethon.sessions import StringSession, SQLiteSession
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError,
    PhoneCodeExpiredError, FloodWaitError
)
from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationsRequest
import python_socks
from supabase import create_client, Client

# ==================== CONFIGURATION ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "9096733224:AAEQ36LOZoPZpO78kjH-1WRvG9KnWo_vqs0")
SUPABASE_URL = os.getenv("SUPABASE_URL", "YOUR_SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "YOUR_SUPABASE_KEY")
ADMIN_ID = 7266067201

# Official Telegram Desktop API
API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Active login sessions in RAM
active_logins = {}

# Realistic Desktop Device Pool for Anti-Freeze
DEVICE_POOL = [
    {"device": "ASUS ROG Strix G15", "sdk": "Windows 11 x64", "app": "4.16.8 x64"},
    {"device": "Dell XPS 15 9530", "sdk": "Windows 11 x64", "app": "5.1.2 x64"},
    {"device": "Lenovo ThinkPad X1 Carbon", "sdk": "Windows 10 x64", "app": "4.15.2 x64"},
    {"device": "HP Spectre x360", "sdk": "Windows 11 x64", "app": "5.2.3 x64"},
    {"device": "MSI Stealth 16 Studio", "sdk": "Windows 10 x64", "app": "4.16.4 x64"},
    {"device": "Acer Predator Helios 300", "sdk": "Windows 11 x64", "app": "5.0.1 x64"}
]

# ==================== FSM STATES ====================
class UserFlow(StatesGroup):
    waiting_otp = State()
    waiting_2fa = State()

class AdminFlow(StatesGroup):
    add_c_code = State()
    add_c_name = State()
    add_c_price = State()
    add_c_time = State()
    add_c_proxy = State()
    set_2fa_pass = State()
    broadcast_text = State()

# ==================== HELPER FUNCTIONS ====================
def clean_phone(raw: str) -> str:
    digits = re.sub(r'\D', '', raw)
    return f"+{digits}" if len(digits) >= 7 else ""

def parse_proxy(proxy_str: str):
    if not proxy_str or proxy_str.lower() == "none":
        return None
    clean = proxy_str.replace("socks5://", "").replace("http://", "").replace("@", ":")
    parts = clean.split(":")
    if len(parts) == 4:
        return (python_socks.ProxyType.SOCKS5, parts[0], int(parts[1]), True, parts[2], parts[3])
    elif len(parts) == 2:
        return (python_socks.ProxyType.SOCKS5, parts[0], int(parts[1]))
    return None

def get_settings():
    res = supabase.table("bot_settings").select("*").eq("id", 1).execute()
    return res.data[0] if res.data else {}

def match_country(phone: str):
    res = supabase.table("countries").select("*").eq("is_active", True).execute()
    if not res.data:
        return None
    # Sort by longest prefix first (e.g., +880 before +8)
    sorted_c = sorted(res.data, key=lambda x: len(x["country_code"]), reverse=True)
    for c in sorted_c:
        if phone.startswith(c["country_code"]):
            return c
    return None

async def check_single_device(session_str: str, dev_info: dict, proxy_str: str = "") -> tuple[bool, int]:
    """
    Returns (is_alive, total_devices_count)
    """
    client = TelegramClient(
        StringSession(session_str), API_ID, API_HASH,
        device_model=dev_info.get("device", "PC 64bit"),
        system_version=dev_info.get("sdk", "Windows 11 x64"),
        app_version=dev_info.get("app", "4.16.8 x64"),
        proxy=parse_proxy(proxy_str)
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return False, 0
        auths = await client(GetAuthorizationsRequest())
        count = len(auths.authorizations)
        await client.disconnect()
        return True, count
    except Exception:
        try:
            await client.disconnect()
        except Exception:
            pass
        return False, 0

# ==================== BACKGROUND CONFIRMATION TIMER ====================
async def schedule_confirmation_check(account_id: int, user_id: int, phone: str, wait_sec: int):
    await asyncio.sleep(wait_sec)
    res = supabase.table("accounts").select("*").eq("id", account_id).execute()
    if not res.data:
        return
    acc = res.data[0]
    if acc["status"] != "pending":
        return

    c_res = supabase.table("countries").select("*").eq("country_code", acc["country_code"]).execute()
    proxy_url = c_res.data[0]["proxy_url"] if c_res.data else ""

    dev_info = {
        "device": acc["device_model"],
        "sdk": acc["system_version"],
        "app": acc["app_version"]
    }
    is_alive, dev_count = await check_single_device(acc["session_string"], dev_info, proxy_url)

    if not is_alive:
        supabase.table("accounts").update({"status": "rejected"}).eq("id", account_id).execute()
        await bot.send_message(
            user_id,
            f"❌ <b>Account Rejected!</b>\nNumber: <code>{phone}</code>\nReason: Session was logged out or terminated before confirmation time.",
            parse_mode="HTML"
        )
        return

    if dev_count > 1:
        supabase.table("accounts").update({"status": "waiting_logout"}).eq("id", account_id).execute()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 I Logged Out / Check Again", callback_data=f"chklogout_{account_id}")],
            [InlineKeyboardButton(text="⚡ Force Logout Other Devices", callback_data=f"forcelogout_{account_id}")]
        ])
        await bot.send_message(
            user_id,
            f"⚠️ <b>Multiple Devices Logged In ({dev_count} devices)!</b>\n\n"
            f"📱 Number: <code>{phone}</code>\n"
            f"Confirmation time is over, but your device is still logged into this account.\n\n"
            f"👉 <b>Please log out from your phone/device now</b> and click the button below to receive your <b>${acc['price']}</b> reward!",
            reply_markup=kb,
            parse_mode="HTML"
        )
    else:
        await finalize_account_confirmation(acc)

async def finalize_account_confirmation(acc: dict):
    supabase.table("accounts").update({"status": "confirmed"}).eq("id", acc["id"]).execute()
    u_res = supabase.table("users").select("*").eq("user_id", acc["submitted_by"]).execute()
    if u_res.data:
        u = u_res.data[0]
        new_bal = float(u["balance"]) + float(acc["price"])
        new_conf = int(u["total_confirmed"]) + 1
        supabase.table("users").update({
            "balance": new_bal,
            "total_confirmed": new_conf
        }).eq("user_id", acc["submitted_by"]).execute()

    await bot.send_message(
        acc["submitted_by"],
        f"✅ <b>Account Confirmed!</b>\n\n"
        f"📱 Number: <code>{acc['phone']}</code>\n"
        f"💵 Reward Added: <b>+${acc['price']}</b>\n"
        f"🛡️ Status: Only Bot Logged In (Verified)",
        parse_mode="HTML"
    )

# ==================== LOGOUT CHECK BUTTON HANDLERS ====================
@dp.callback_query(F.data.startswith("chklogout_"))
async def cb_check_logout(call: CallbackQuery):
    acc_id = int(call.data.split("_")[1])
    res = supabase.table("accounts").select("*").eq("id", acc_id).execute()
    if not res.data:
        return await call.answer("Account not found!", show_alert=True)
    acc = res.data[0]
    if acc["status"] == "confirmed":
        return await call.message.edit_text(f"✅ Number <code>{acc['phone']}</code> is already confirmed!", parse_mode="HTML")

    await call.answer("🔍 Checking active devices, please wait...")
    dev_info = {"device": acc["device_model"], "sdk": acc["system_version"], "app": acc["app_version"]}
    is_alive, dev_count = await check_single_device(acc["session_string"], dev_info)

    if not is_alive:
        supabase.table("accounts").update({"status": "rejected"}).eq("id", acc_id).execute()
        return await call.message.edit_text(f"❌ <b>Session Dead!</b> You logged out the bot session instead of your device.", parse_mode="HTML")

    if dev_count > 1:
        return await call.answer(f"❌ Still {dev_count} devices logged in! Please log out from your phone first.", show_alert=True)

    await call.message.delete()
    await finalize_account_confirmation(acc)

@dp.callback_query(F.data.startswith("forcelogout_"))
async def cb_force_logout(call: CallbackQuery):
    acc_id = int(call.data.split("_")[1])
    res = supabase.table("accounts").select("*").eq("id", acc_id).execute()
    if not res.data:
        return
    acc = res.data[0]
    client = TelegramClient(
        StringSession(acc["session_string"]), API_ID, API_HASH,
        device_model=acc["device_model"], system_version=acc["system_version"], app_version=acc["app_version"]
    )
    try:
        await client.connect()
        await client(ResetAuthorizationsRequest())
        auths = await client(GetAuthorizationsRequest())
        count = len(auths.authorizations)
        await client.disconnect()
        if count == 1:
            await call.message.delete()
            await finalize_account_confirmation(acc)
        else:
            await call.answer("⚠️ Telegram blocks new sessions from kicking old devices for 24h. Please log out manually from your phone!", show_alert=True)
    except Exception:
        try:
            await client.disconnect()
        except Exception:
            pass
        await call.answer("⚠️ Telegram security requires manual logout from your phone before 24h. Please log out from your phone settings!", show_alert=True)

# ==================== USER MENU COMMANDS (/start, /cancel, /capacity, /balance, /help) ====================
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    supabase.table("users").upsert({
        "user_id": message.from_user.id,
        "username": message.from_user.username or "None"
    }, on_conflict="user_id").execute()

    await message.answer(
        f"Welcome <b>{message.from_user.full_name}</b> ✨\n\n"
        f"You can send me a phone number with country code.\n"
        f"e.g. <code>+57 300 123 4567</code> or <code>8801712345678</code>\n\n"
        f"🚀 <b>Ready to receive!</b>",
        parse_mode="HTML"
    )

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    uid = message.from_user.id
    if uid in active_logins:
        try:
            await active_logins[uid]["client"].disconnect()
        except Exception:
            pass
        del active_logins[uid]
    await state.clear()
    await message.answer("❌ <b>Process Canceled.</b>\nYou can now send a new number or use other commands.", parse_mode="HTML")

@dp.message(Command("capacity"))
async def cmd_capacity(message: Message):
    res = supabase.table("countries").select("*").eq("is_active", True).execute()
    if not res.data:
        return await message.answer("❌ No countries are currently open.")
    text = "📊 <b>Available Countries & Capacity Info:</b>\n\n"
    for c in res.data:
        text += (
            f"🌍 <b>{c['country_code']} {c['country_name']}</b>\n"
            f"<code>Price: ${c['price']} | Cap: {c['capacity']} | Time: {c['confirm_time_sec']}s</code>\n"
            f"──────────────────────\n"
        )
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    res = supabase.table("users").select("*").eq("user_id", message.from_user.id).execute()
    if not res.data:
        return await message.answer("Please send /start first.")
    u = res.data[0]
    await message.answer(
        f"👤 <b>User Information</b>\n"
        f"────────────────────\n"
        f"🏷️ <b>Name:</b> {message.from_user.full_name}\n"
        f"🆔 <b>User ID:</b> <code>{u['user_id']}</code>\n"
        f"💵 <b>Balance:</b> <b>${float(u['balance']):.2f}</b>\n"
        f"📥 <b>Submitted:</b> {u['total_submitted']}\n"
        f"✅ <b>Confirmed:</b> {u['total_confirmed']}\n"
        f"────────────────────\n"
        f"⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        parse_mode="HTML"
    )

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "ℹ️ <b>How to use this bot:</b>\n\n"
        "1️⃣ Send a phone number (with or without +).\n"
        "2️⃣ Enter the 5-digit verification code sent to your Telegram.\n"
        "3️⃣ Log out from your device and wait for the confirmation timer.\n"
        "4️⃣ Once confirmed, reward is added to your /balance automatically!\n\n"
        "To cancel any ongoing process, send /cancel.",
        parse_mode="HTML"
    )

# ==================== ADMIN PANEL (/admin) ====================
def admin_main_kb():
    cfg = get_settings()
    b_status = "🟢 ON" if cfg.get("bot_active", True) else "🔴 OFF"
    tfa_status = "🟢 ON" if cfg.get("enable_2fa", True) else "🔴 OFF"
    spam_status = "🔴 Reject" if cfg.get("reject_spam", False) else "🟢 Accept"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🌍 Country Settings", callback_data="adm_countries"),
            InlineKeyboardButton(text="📦 Accounts Export", callback_data="adm_export")
        ],
        [
            InlineKeyboardButton(text=f"🤖 Bot: {b_status}", callback_data="adm_toggle_bot"),
            InlineKeyboardButton(text=f"🔐 2FA: {tfa_status}", callback_data="adm_toggle_2fa")
        ],
        [
            InlineKeyboardButton(text=f"🔑 2FA Pass: {cfg.get('two_fa_pass')}", callback_data="adm_set_2fa"),
            InlineKeyboardButton(text=f"🚫 Spam: {spam_status}", callback_data="adm_toggle_spam")
        ],
        [
            InlineKeyboardButton(text="👥 Users & Stats", callback_data="adm_stats"),
            InlineKeyboardButton(text="📢 Broadcast", callback_data="adm_broadcast")
        ],
        [
            InlineKeyboardButton(text="🧹 Clear Cache", callback_data="adm_clear_cache"),
            InlineKeyboardButton(text="🔄 Restart Bot", callback_data="adm_restart")
        ]
    ])

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("🛠️ <b>Max-Level Admin Control Panel</b>\nSelect an option to manage:", reply_markup=admin_main_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "adm_back")
async def cb_adm_back(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await call.message.edit_text("🛠️ <b>Max-Level Admin Control Panel</b>\nSelect an option to manage:", reply_markup=admin_main_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "adm_toggle_bot")
async def cb_toggle_bot(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    cfg = get_settings()
    supabase.table("bot_settings").update({"bot_active": not cfg["bot_active"]}).eq("id", 1).execute()
    await call.message.edit_reply_markup(reply_markup=admin_main_kb())

@dp.callback_query(F.data == "adm_toggle_2fa")
async def cb_toggle_2fa(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    cfg = get_settings()
    supabase.table("bot_settings").update({"enable_2fa": not cfg["enable_2fa"]}).eq("id", 1).execute()
    await call.message.edit_reply_markup(reply_markup=admin_main_kb())

@dp.callback_query(F.data == "adm_toggle_spam")
async def cb_toggle_spam(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    cfg = get_settings()
    supabase.table("bot_settings").update({"reject_spam": not cfg["reject_spam"]}).eq("id", 1).execute()
    await call.message.edit_reply_markup(reply_markup=admin_main_kb())

@dp.callback_query(F.data == "adm_set_2fa")
async def cb_set_2fa(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID: return
    await state.set_state(AdminFlow.set_2fa_pass)
    await call.message.edit_text("🔑 Send the new custom <b>2FA Password</b> you want the bot to set on accounts:", parse_mode="HTML")

@dp.message(AdminFlow.set_2fa_pass)
async def save_2fa_pass(message: Message, state: FSMContext):
    supabase.table("bot_settings").update({"two_fa_pass": message.text.strip()}).eq("id", 1).execute()
    await state.clear()
    await message.answer(f"✅ 2FA Password updated to: <code>{message.text.strip()}</code>", reply_markup=admin_main_kb(), parse_mode="HTML")

# --- COUNTRY MANAGEMENT ---
@dp.callback_query(F.data == "adm_countries")
async def cb_adm_countries(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    res = supabase.table("countries").select("*").execute()
    buttons = [[InlineKeyboardButton(text="➕ Add / Update Country", callback_data="adm_add_country")]]
    for c in res.data:
        st = "✅" if c["is_active"] else "❌"
        buttons.append([
            InlineKeyboardButton(text=f"{st} {c['country_name']} ({c['country_code']}) - ${c['price']} | {c['confirm_time_sec']}s", callback_data=f"togc_{c['country_code']}"),
            InlineKeyboardButton(text="🗑️", callback_data=f"delc_{c['country_code']}")
        ])
    buttons.append([InlineKeyboardButton(text="🔙 Back", callback_data="adm_back")])
    await call.message.edit_text("🌍 <b>Country Settings</b>\nClick a country to toggle ON/OFF or add a new one:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

@dp.callback_query(F.data.startswith("togc_"))
async def cb_toggle_country(call: CallbackQuery):
    code = call.data.split("_")[1]
    c = supabase.table("countries").select("*").eq("country_code", code).execute().data[0]
    supabase.table("countries").update({"is_active": not c["is_active"]}).eq("country_code", code).execute()
    await cb_adm_countries(call)

@dp.callback_query(F.data.startswith("delc_"))
async def cb_del_country(call: CallbackQuery):
    code = call.data.split("_")[1]
    supabase.table("countries").delete().eq("country_code", code).execute()
    await cb_adm_countries(call)

@dp.callback_query(F.data == "adm_add_country")
async def cb_add_c_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFlow.add_c_code)
    await call.message.edit_text("Send Country Code (e.g. <code>+880</code> or <code>+57</code>):", parse_mode="HTML")

@dp.message(AdminFlow.add_c_code)
async def step_c_code(message: Message, state: FSMContext):
    code = message.text.strip()
    if not code.startswith("+"): code = "+" + code
    await state.update_data(code=code)
    await state.set_state(AdminFlow.add_c_name)
    await message.answer("Enter Country Name (e.g. <code>Bangladesh</code> or <code>Colombia</code>):", parse_mode="HTML")

@dp.message(AdminFlow.add_c_name)
async def step_c_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(AdminFlow.add_c_price)
    await message.answer("Enter Price per confirmed account in USD (e.g. <code>0.20</code>):", parse_mode="HTML")

@dp.message(AdminFlow.add_c_price)
async def step_c_price(message: Message, state: FSMContext):
    await state.update_data(price=float(message.text.strip()))
    await state.set_state(AdminFlow.add_c_time)
    await message.answer("Enter Confirmation Wait Time in seconds (e.g. <code>120</code> or <code>600</code>):", parse_mode="HTML")

@dp.message(AdminFlow.add_c_time)
async def step_c_time(message: Message, state: FSMContext):
    await state.update_data(time_sec=int(message.text.strip()))
    await state.set_state(AdminFlow.add_c_proxy)
    await message.answer("Enter SOCKS5 Proxy (<code>ip:port:user:pass</code>) or type <code>none</code> for direct connection:", parse_mode="HTML")

@dp.message(AdminFlow.add_c_proxy)
async def step_c_proxy(message: Message, state: FSMContext):
    data = await state.get_data()
    proxy = "" if message.text.strip().lower() == "none" else message.text.strip()
    supabase.table("countries").upsert({
        "country_code": data["code"],
        "country_name": data["name"],
        "price": data["price"],
        "confirm_time_sec": data["time_sec"],
        "proxy_url": proxy,
        "is_active": True
    }).execute()
    await state.clear()
    await message.answer(f"✅ Country <b>{data['name']} ({data['code']})</b> saved!", reply_markup=admin_main_kb(), parse_mode="HTML")

# --- 1-CLICK EXPORT (.session, .json, Both ZIP) ---
@dp.callback_query(F.data == "adm_export")
async def cb_adm_export(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    conf = supabase.table("accounts").select("id", count="exact").eq("status", "confirmed").execute().count or 0
    pend = supabase.table("accounts").select("id", count="exact").in_("status", ["pending", "waiting_logout"]).execute().count or 0
    dl = supabase.table("accounts").select("id", count="exact").eq("status", "downloaded").execute().count or 0

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📦 Download Confirmed (Both .session + .json) [{conf}]", callback_data="exp_both_confirmed")],
        [
            InlineKeyboardButton(text="📄 Only .session", callback_data="exp_session_confirmed"),
            InlineKeyboardButton(text="📋 Only .json", callback_data="exp_json_confirmed")
        ],
        [InlineKeyboardButton(text=f"♻️ Re-Download Previous Archive [{dl}]", callback_data="exp_both_downloaded")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="adm_back")]
    ])
    await call.message.edit_text(
        f"📦 <b>Account Session Exporter</b>\n\n"
        f"✅ <b>Ready Confirmed:</b> {conf}\n"
        f"⏳ <b>Pending / Waiting Logout:</b> {pend}\n"
        f"📥 <b>Total Downloaded:</b> {dl}\n\n"
        f"Select your preferred format below:",
        reply_markup=kb, parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("exp_"))
async def cb_run_export(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    _, fmt, target_status = call.data.split("_")
    res = supabase.table("accounts").select("*").eq("status", target_status).execute()
    if not res.data:
        return await call.answer("No accounts available in this category!", show_alert=True)

    await call.answer("⚙️ Generating ZIP archive, please wait...")
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        report = ["Phone | TG_UID | Submitted_By_UserID | Spam | 2FA | Device\n" + "="*70]
        for acc in res.data:
            clean_p = acc["phone"].replace("+", "")
            sub_id = acc["submitted_by"]
            prefix = f"User_{sub_id}/{clean_p}"

            if fmt in ["json", "both"]:
                j_content = {
                    "session_file": clean_p,
                    "phone": acc["phone"],
                    "user_id": acc["tg_user_id"],
                    "submitted_by_user_id": sub_id,
                    "app_id": API_ID,
                    "app_hash": API_HASH,
                    "sdk": acc["system_version"],
                    "app_version": acc["app_version"],
                    "device": acc["device_model"],
                    "lang_pack": "tdesktop",
                    "lang_code": "en",
                    "system_lang_code": "en-US",
                    "twoFA": acc["two_fa"],
                    "spam_status": acc["spam_status"]
                }
                zf.writestr(f"{prefix}.json", json.dumps(j_content, indent=4))

            if fmt in ["session", "both"]:
                tmp_file = f"tmp_{clean_p}.session"
                s_str = StringSession(acc["session_string"])
                sql_s = SQLiteSession(tmp_file)
                sql_s.set_dc(s_str.dc_id, s_str.server_address, s_str.port)
                sql_s.auth_key = s_str.auth_key
                sql_s.save()
                sql_s.close()
                with open(tmp_file, "rb") as f:
                    zf.writestr(f"{prefix}.session", f.read())
                os.remove(tmp_file)

            report.append(f"{acc['phone']} | {acc['tg_user_id']} | {sub_id} | {acc['spam_status']} | {acc['two_fa']} | {acc['device_model']}")
            if target_status == "confirmed":
                supabase.table("accounts").update({"status": "downloaded"}).eq("id", acc["id"]).execute()

        zf.writestr("User_Submitted_Report.txt", "\n".join(report))

    zip_buffer.seek(0)
    file_name = f"Accounts_{fmt}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    await call.message.answer_document(
        BufferedInputFile(zip_buffer.read(), filename=file_name),
        caption=f"✅ <b>Exported {len(res.data)} Accounts ({fmt.upper()})</b>\nIncludes Submitter User ID folders & report.",
        parse_mode="HTML"
    )
    await cb_adm_export(call)

# --- STATS, CACHE, BROADCAST & RESTART ---
@dp.callback_query(F.data == "adm_stats")
async def cb_adm_stats(call: CallbackQuery):
    users = supabase.table("users").select("*").execute().data
    text = f"👥 <b>Total Users ({len(users)}):</b>\n\n"
    for u in users[:25]:
        text += f"• <code>{u['user_id']}</code> (@{u['username']}) — Bal: <b>${float(u['balance']):.2f}</b> | Conf: {u['total_confirmed']}\n"
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="adm_back")]]), parse_mode="HTML")

@dp.callback_query(F.data == "adm_clear_cache")
async def cb_clear_cache(call: CallbackQuery):
    count = len(active_logins)
    for uid, item in list(active_logins.items()):
        try:
            await item["client"].disconnect()
        except Exception:
            pass
    active_logins.clear()
    await call.answer(f"🧹 Cleared {count} active RAM sessions!", show_alert=True)

@dp.callback_query(F.data == "adm_broadcast")
async def cb_broadcast(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFlow.broadcast_text)
    await call.message.edit_text("📢 Send the message to broadcast to all users:")

@dp.message(AdminFlow.broadcast_text)
async def run_broadcast(message: Message, state: FSMContext):
    await state.clear()
    users = supabase.table("users").select("user_id").execute().data
    sent = 0
    for u in users:
        try:
            await bot.send_message(u["user_id"], message.text)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await message.answer(f"✅ Broadcast sent to {sent} users!", reply_markup=admin_main_kb())

@dp.callback_query(F.data == "adm_restart")
async def cb_restart(call: CallbackQuery):
    await call.message.edit_text("🔄 Bot is restarting...")
    os.execv(sys.executable, ['python'] + sys.argv)

# ==================== USER PHONE & OTP RECEIVER ENGINE ====================
@dp.message(UserFlow.waiting_otp)
async def handle_otp_input(message: Message, state: FSMContext):
    uid = message.from_user.id
    if uid not in active_logins:
        await state.clear()
        return await message.answer("⚠️ Session expired! Please send your phone number again.")

    code = re.sub(r'\D', '', message.text.strip())
    if not code:
        return await message.answer("❌ Please enter a valid numeric OTP code (or /cancel):")

    session_info = active_logins[uid]
    client: TelegramClient = session_info["client"]
    phone = session_info["phone"]

    try:
        await client.sign_in(phone=phone, code=code)
        await complete_account_setup(message, state, uid)
    except SessionPasswordNeededError:
        await state.set_state(UserFlow.waiting_2fa)
        await message.answer("🔐 This account has 2FA enabled! Please send your current <b>2FA Password</b>:", parse_mode="HTML")
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        await message.answer("❌ Invalid or expired OTP code! Try again or send /cancel.")
    except Exception as e:
        await message.answer(f"❌ Error signing in: {e}")

@dp.message(UserFlow.waiting_2fa)
async def handle_existing_2fa(message: Message, state: FSMContext):
    uid = message.from_user.id
    if uid not in active_logins:
        await state.clear()
        return await message.answer("⚠️ Session expired! Send the phone number again.")
    client: TelegramClient = active_logins[uid]["client"]
    try:
        await client.sign_in(password=message.text.strip())
        await complete_account_setup(message, state, uid, old_2fa=message.text.strip())
    except Exception:
        await message.answer("❌ Wrong 2FA password! Try again or /cancel.")

async def complete_account_setup(message: Message, state: FSMContext, uid: int, old_2fa: str = None):
    info = active_logins.pop(uid)
    client: TelegramClient = info["client"]
    phone = info["phone"]
    country = info["country"]
    dev = info["device"]
    cfg = get_settings()

    status_msg = await message.answer("⏳ <b>Verifying & securing account... Please wait 5 seconds.</b>", parse_mode="HTML")

    try:
        me = await client.get_me()
        # 1. Human-like anti-freeze delay
        await asyncio.sleep(random.uniform(2.5, 4.0))

        # 2. Send branding note in Saved Messages
        await client.send_message("me", cfg.get("saved_msg_text", "Colombia Empire By Mahin"))

        # 3. Check @SpamBot
        spam_status = "Free"
        try:
            async with client.conversation("@SpamBot", timeout=8) as conv:
                await conv.send_message("/start")
                r = await conv.get_response()
                await conv.mark_read()
                txt = r.text.lower()
                if "no limits" in txt or "good news" in txt:
                    spam_status = "Free"
                else:
                    spam_status = "Limited"
        except Exception:
            spam_status = "Unknown"

        if spam_status == "Limited" and cfg.get("reject_spam", False):
            await client.log_out()
            await state.clear()
            return await status_msg.edit_text("❌ <b>Account Rejected!</b>\nReason: Account is Limited/Frozen on @SpamBot.", parse_mode="HTML")

        # 4. Set Custom 2FA if enabled
        final_2fa = old_2fa or ""
        if cfg.get("enable_2fa", True) and cfg.get("two_fa_pass"):
            new_pass = cfg["two_fa_pass"]
            try:
                await client.edit_2fa(current_password=old_2fa, new_password=new_pass, hint="")
                final_2fa = new_pass
            except Exception:
                pass

        sess_str = client.session.save()
        await client.disconnect()

        # Save to Supabase
        ins = supabase.table("accounts").insert({
            "phone": phone,
            "country_code": country["country_code"],
            "tg_user_id": me.id,
            "submitted_by": uid,
            "session_string": sess_str,
            "two_fa": final_2fa,
            "spam_status": spam_status,
            "device_model": dev["device"],
            "system_version": dev["sdk"],
            "app_version": dev["app"],
            "price": country["price"],
            "status": "pending"
        }).execute()
        acc_id = ins.data[0]["id"]

        # Update user total submitted count
        u_res = supabase.table("users").select("total_submitted").eq("user_id", uid).execute()
        if u_res.data:
            supabase.table("users").update({"total_submitted": u_res.data[0]["total_submitted"] + 1}).eq("user_id", uid).execute()

        await state.clear()
        wait_s = country["confirm_time_sec"]
        await status_msg.edit_text(
            f"✅ <b>Login Successful!</b>\n\n"
            f"📱 <b>Number:</b> <code>{phone}</code>\n"
            f"🛡️ <b>Spam Status:</b> {spam_status}\n"
            f"💵 <b>Reward:</b> ${country['price']}\n"
            f"⏱️ <b>Confirmation Time:</b> {wait_s} seconds\n\n"
            f"⚠️ <b>IMPORTANT:</b> Please log out from your phone/device now! After {wait_s}s, if only the bot is logged in, your balance will be added automatically.",
            parse_mode="HTML"
        )

        # Start background timer
        asyncio.create_task(schedule_confirmation_check(acc_id, uid, phone, wait_s))

    except Exception as e:
        try:
            await client.disconnect()
        except Exception:
            pass
        await state.clear()
        await status_msg.edit_text(f"❌ Error during verification: {e}")

# Catch phone numbers sent by user
@dp.message(F.text)
async def handle_phone_submission(message: Message, state: FSMContext):
    if message.text.startswith("/"):
        return
    cfg = get_settings()
    if not cfg.get("bot_active", True) and message.from_user.id != ADMIN_ID:
        return await message.answer("⚠️ Bot is currently under maintenance. Please try again later.")

    phone = clean_phone(message.text)
    if not phone:
        return await message.answer("❌ Invalid phone number format! Send a valid number with country code (e.g. <code>+88017...</code> or <code>+57300...</code>).", parse_mode="HTML")

    # 1. Duplicate Check (Block if pending, waiting_logout, confirmed, or downloaded)
    existing = supabase.table("accounts").select("status").eq("phone", phone).execute()
    if existing.data:
        st = existing.data[0]["status"]
        if st in ["pending", "waiting_logout", "confirmed", "downloaded"]:
            return await message.answer(f"⚠️ <b>Duplicate Number!</b>\n<code>{phone}</code> is already <b>{st.upper()}</b> in our system and cannot be submitted again.", parse_mode="HTML")
        else:
            # If previously rejected, remove old row to allow fresh retry
            supabase.table("accounts").delete().eq("phone", phone).execute()

    # 2. Match Country
    country = match_country(phone)
    if not country:
        return await message.answer("❌ Bot is not accepting numbers from this country right now. Check /capacity for open countries.")

    wait_msg = await message.answer(f"📡 <b>Connecting to {country['country_name']} server & sending code to {phone}...</b>", parse_mode="HTML")

    dev = random.choice(DEVICE_POOL)
    client = TelegramClient(
        StringSession(), API_ID, API_HASH,
        device_model=dev["device"],
        system_version=dev["sdk"],
        app_version=dev["app"],
        proxy=parse_proxy(country.get("proxy_url", ""))
    )

    try:
        await client.connect()
        await client.send_code_request(phone)
        active_logins[message.from_user.id] = {
            "client": client,
            "phone": phone,
            "country": country,
            "device": dev
        }
        await state.set_state(UserFlow.waiting_otp)
        await wait_msg.edit_text(
            f"📩 <b>Code Sent to {phone}!</b>\n\n"
            f"Please enter the 5-digit verification code below:\n"
            f"<i>(To cancel, send /cancel)</i>",
            parse_mode="HTML"
        )
    except FloodWaitError as e:
        await client.disconnect()
        await wait_msg.edit_text(f"⏳ Telegram FloodWait: Please wait {e.seconds} seconds before trying this number.")
    except Exception as e:
        await client.disconnect()
        await wait_msg.edit_text(f"❌ Failed to send code: {e}")

# ==================== MENU SETUP & RENDER WEB SERVER ====================
async def setup_bot_commands():
    # Exact menu commands from your screenshot
    commands = [
        BotCommand(command="start", description="Start"),
        BotCommand(command="cancel", description="Cancel"),
        BotCommand(command="capacity", description="Capacity"),
        BotCommand(command="balance", description="Balance"),
        BotCommand(command="help", description="Help")
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())

async def health_check(request):
    return web.Response(text="Bot is running 24/7!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def main():
    await setup_bot_commands()
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
