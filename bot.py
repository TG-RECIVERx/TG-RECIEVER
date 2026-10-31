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
from telethon.tl.functions.account import GetAuthorizationsRequest
from telethon.tl.functions.auth import ResetAuthorizationsRequest
import python_socks
from supabase import create_client, Client

# ==================== CONFIGURATION ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "9096733224:AAEQ36LOZoPZpO78kjH-1WRvG9KnWo_vqs0")
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://mchijfbwfxetqqfycwwv.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "YOUR_SERVICE_ROLE_KEY")
ADMIN_ID = 7266067201
FORWARD_USERNAME = "@syedmahinislam"

API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

active_logins = {}

DEVICE_POOL = [
    {"device": "ASUS ROG Strix G15", "sdk": "Windows 11 x64", "app": "4.16.8 x64"},
    {"device": "Dell XPS 15 9530", "sdk": "Windows 11 x64", "app": "5.1.2 x64"},
    {"device": "Lenovo ThinkPad X1 Carbon", "sdk": "Windows 10 x64", "app": "4.15.2 x64"},
    {"device": "HP Spectre x360", "sdk": "Windows 11 x64", "app": "5.2.3 x64"},
    {"device": "MSI Stealth 16 Studio", "sdk": "Windows 10 x64", "app": "4.16.4 x64"},
    {"device": "Acer Predator Helios 300", "sdk": "Windows 11 x64", "app": "5.0.1 x64"}
]

class UserFlow(StatesGroup):
    waiting_otp = State()
    waiting_2fa = State()

class AdminFlow(StatesGroup):
    add_c_code = State()
    add_c_name = State()
    add_c_price = State()
    add_c_cap = State()
    add_c_time = State()
    add_c_proxy = State()
    set_2fa_pass = State()
    broadcast_text = State()

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
    if not res.data: return None
    sorted_c = sorted(res.data, key=lambda x: len(x["country_code"]), reverse=True)
    for c in sorted_c:
        if phone.startswith(c["country_code"]):
            return c
    return None

async def check_single_device(session_str: str, dev_info: dict, proxy_str: str = "") -> tuple[bool, int]:
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
        try: await client.disconnect()
        except: pass
        return False, 0

# ==================== CONFIRMATION & MULTI-DEVICE LOGIC ====================
async def schedule_confirmation_check(account_id: int, user_id: int, phone: str, wait_sec: int):
    await asyncio.sleep(wait_sec)
    res = supabase.table("accounts").select("*").eq("id", account_id).execute()
    if not res.data: return
    acc = res.data[0]
    if acc["status"] != "pending": return

    c_res = supabase.table("countries").select("*").eq("country_code", acc["country_code"]).execute()
    proxy_url = c_res.data[0]["proxy_url"] if c_res.data else ""
    cfg = get_settings()

    dev_info = {"device": acc["device_model"], "sdk": acc["system_version"], "app": acc["app_version"]}
    is_alive, dev_count = await check_single_device(acc["session_string"], dev_info, proxy_url)

    if not is_alive:
        supabase.table("accounts").update({"status": "rejected", "reject_reason": "Session dead before timer."}).eq("id", account_id).execute()
        await bot.send_message(user_id, f"❌ <b>Account Rejected!</b>\nNumber: <code>{phone}</code>\nReason: Session logged out.", parse_mode="HTML")
        return

    # Multi-device logic: Bot chara onno device (phone) login thakle
    if dev_count > 1:
        if cfg.get("accept_multiple_devices", False):
            await finalize_account_confirmation(acc)
        else:
            supabase.table("accounts").update({"status": "waiting_logout"}).eq("id", account_id).execute()
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 I Logged Out / Check Again", callback_data=f"chklogout_{account_id}")],
                [InlineKeyboardButton(text="⚡ Force Logout Other Devices", callback_data=f"forcelogout_{account_id}")]
            ])
            await bot.send_message(
                user_id,
                f"⚠️ <b>Multiple Devices Detected ({dev_count} devices)!</b>\n\n"
                f"📱 Number: <code>{phone}</code>\n"
                f"Confirmation time is over, but your phone/device is still logged in. Please log out from your phone and click below to get your balance!",
                reply_markup=kb, parse_mode="HTML"
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
        supabase.table("users").update({"balance": new_bal, "total_confirmed": new_conf}).eq("user_id", acc["submitted_by"]).execute()

    await bot.send_message(
        acc["submitted_by"],
        f"✅ <b>Account Confirmed!</b>\n\n📱 Number: <code>{acc['phone']}</code>\n💵 Reward Added: <b>+${acc['price']}</b>",
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("chklogout_"))
async def cb_check_logout(call: CallbackQuery):
    acc_id = int(call.data.split("_")[1])
    res = supabase.table("accounts").select("*").eq("id", acc_id).execute()
    if not res.data: return await call.answer("Not found!", show_alert=True)
    acc = res.data[0]
    if acc["status"] == "confirmed": return await call.message.edit_text("✅ Already confirmed!", parse_mode="HTML")

    await call.answer("🔍 Checking active devices...")
    dev_info = {"device": acc["device_model"], "sdk": acc["system_version"], "app": acc["app_version"]}
    is_alive, dev_count = await check_single_device(acc["session_string"], dev_info)

    if not is_alive:
        supabase.table("accounts").update({"status": "rejected", "reject_reason": "Bot session dead."}).eq("id", acc_id).execute()
        return await call.message.edit_text("❌ Session is dead or invalid.", parse_mode="HTML")

    if dev_count > 1:
        return await call.answer(f"❌ Still {dev_count} devices active! Please log out from your phone first.", show_alert=True)

    await call.message.delete()
    await finalize_account_confirmation(acc)

@dp.callback_query(F.data.startswith("forcelogout_"))
async def cb_force_logout(call: CallbackQuery):
    acc_id = int(call.data.split("_")[1])
    res = supabase.table("accounts").select("*").eq("id", acc_id).execute()
    if not res.data: return
    acc = res.data[0]
    client = TelegramClient(
        StringSession(acc["session_string"]), API_ID, API_HASH,
        device_model=acc["device_model"], system_version=acc["system_version"], app_version=acc["app_version"]
    )
    try:
        await client.connect()
        await client(ResetAuthorizationsRequest())
        await client.disconnect()
        await call.message.delete()
        await finalize_account_confirmation(acc)
    except Exception:
        try: await client.disconnect()
        except: pass
        await call.answer("⚠️ Please log out manually from your phone settings.", show_alert=True)

# ==================== USER COMMANDS & WITHDRAW ====================
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    supabase.table("users").upsert({"user_id": message.from_user.id, "username": message.from_user.username or "None"}, on_conflict="user_id").execute()
    await message.answer(f"Welcome <b>{message.from_user.full_name}</b> ✨\nSend your phone number with country code.", parse_mode="HTML")

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    uid = message.from_user.id
    if uid in active_logins:
        try: await active_logins[uid]["client"].disconnect()
        except: pass
        del active_logins[uid]
    await state.clear()
    await message.answer("❌ Process Canceled.")

@dp.message(Command("capacity"))
async def cmd_capacity(message: Message):
    res = supabase.table("countries").select("*").eq("is_active", True).execute()
    if not res.data: return await message.answer("❌ No active countries.")
    text = "📊 <b>Available Countries & Capacity:</b>\n\n"
    for c in res.data:
        text += f"🌍 <b>{c['country_code']} {c['country_name']}</b>\nPrice: ${c['price']} | Cap: {c['capacity']} | Time: {c['confirm_time_sec']}s\n──────────────────\n"
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    res = supabase.table("users").select("*").eq("user_id", message.from_user.id).execute()
    if not res.data: return await message.answer("Send /start first.")
    u = res.data[0]
    await message.answer(f"👤 <b>Your Balance:</b> <b>${float(u['balance']):.2f}</b>\nSubmitted: {u['total_submitted']} | Confirmed: {u['total_confirmed']}", parse_mode="HTML")

# --- WITHDRAW MENU & BUTTONS ---
@dp.message(Command("withdraw"))
async def cmd_withdraw(message: Message):
    uid = message.from_user.id
    u_res = supabase.table("users").select("*").eq("user_id", uid).execute()
    if not u_res.data: return await message.answer("Send /start first.")
    u = u_res.data[0]
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 Withdraw Now", callback_data="user_withdraw_action")]
    ])
    await message.answer(
        f"💰 <b>Withdrawal Dashboard</b>\n\n"
        f"👤 User: {message.from_user.full_name}\n"
        f"🆔 User ID: <code>{uid}</code>\n"
        f"✅ Confirmed Accounts: <b>{u['total_confirmed']}</b>\n"
        f"💵 Available Balance: <b>${float(u['balance']):.2f}</b>",
        reply_markup=kb, parse_mode="HTML"
    )

@dp.callback_query(F.data == "user_withdraw_action")
async def cb_user_withdraw(call: CallbackQuery):
    cfg = get_settings()
    if not cfg.get("withdraw_enabled", True):
        return await call.answer("❌ Withdrawals are currently turned OFF by Admin!", show_alert=True)

    uid = call.from_user.id
    u_res = supabase.table("users").select("*").eq("user_id", uid).execute()
    if not u_res.data: return call.answer("User not found!", show_alert=True)
    u = u_res.data[0]
    bal = float(u["balance"])

    if bal <= 0:
        return await call.answer("❌ You have zero balance to withdraw!", show_alert=True)

    # Reset balance to 0
    supabase.table("users").update({"balance": 0.0}).eq("user_id", uid).execute()

    # Forward message to @syedmahinislam
    forward_text = (
        f"🚨 <b>New Withdrawal Request!</b>\n\n"
        f"👤 Name: {call.from_user.full_name}\n"
        f"🆔 User ID: <code>{uid}</code>\n"
        f"🔗 Username: @{call.from_user.username or 'None'}\n"
        f"💵 Amount Withdrawn: <b>${bal:.2f}</b>\n"
        f"✅ Total Confirmed: {u['total_confirmed']}"
    )
    try:
        await bot.send_message(FORWARD_USERNAME, forward_text, parse_mode="HTML")
    except Exception:
        try: await bot.send_message(ADMIN_ID, f"[Forward Failed to {FORWARD_USERNAME}]\n\n" + forward_text, parse_mode="HTML")
        except: pass

    await call.message.edit_text(
        f"✅ <b>Withdrawal Complete!</b>\n\n"
        f"Successfully requested withdrawal of <b>${bal:.2f}</b>.\n"
        f"Notification sent to admin successfully!",
        parse_mode="HTML"
    )

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer("ℹ️ Send phone number -> Enter OTP -> Wait for timer / logout -> Use /withdraw to cash out!", parse_mode="HTML")

# ==================== ADMIN PANEL (FIXED WITHDRAW TOGGLE) ====================
def admin_main_kb():
    cfg = get_settings()
    b_st = "🟢 ON" if cfg.get("bot_active", True) else "🔴 OFF"
    tfa_st = "🟢 ON" if cfg.get("enable_2fa", True) else "🔴 OFF"
    wd_st = "🟢 ON" if cfg.get("withdraw_enabled", True) else "🔴 OFF"
    multi_st = "🟢 Accept" if cfg.get("accept_multiple_devices", False) else "🔴 Strict"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌍 Country Settings", callback_data="adm_countries"), InlineKeyboardButton(text="📦 Export Accounts", callback_data="adm_export")],
        [InlineKeyboardButton(text=f"🤖 Bot: {b_st}", callback_data="adm_toggle_bot"), InlineKeyboardButton(text=f"💵 Withdraw: {wd_st}", callback_data="adm_toggle_withdraw")],
        [InlineKeyboardButton(text=f"🔐 2FA: {tfa_st}", callback_data="adm_toggle_2fa"), InlineKeyboardButton(text=f"👥 Multi-Device: {multi_st}", callback_data="adm_toggle_multi")],
        [InlineKeyboardButton(text="❌ View Invalid / Rejected", callback_data="adm_rejected_list"), InlineKeyboardButton(text="⚡ Admin Force Logout Manager", callback_data="adm_force_mgr")],
        [InlineKeyboardButton(text="📢 Broadcast", callback_data="adm_broadcast"), InlineKeyboardButton(text="🔄 Restart", callback_data="adm_restart")]
    ])

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID: return
    await message.answer("🛠️ <b>Admin Control Panel</b>", reply_markup=admin_main_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "adm_back")
async def cb_adm_back(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID: return
    await state.clear()
    await call.message.edit_text("🛠️ <b>Admin Control Panel</b>", reply_markup=admin_main_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "adm_toggle_bot")
async def cb_toggle_bot(call: CallbackQuery):
    cfg = get_settings()
    supabase.table("bot_settings").update({"bot_active": not cfg["bot_active"]}).eq("id", 1).execute()
    await call.message.edit_reply_markup(reply_markup=admin_main_kb())

@dp.callback_query(F.data == "adm_toggle_withdraw")
async def cb_toggle_wd(call: CallbackQuery):
    cfg = get_settings()
    current_status = cfg.get("withdraw_enabled", True)
    supabase.table("bot_settings").update({"withdraw_enabled": not current_status}).eq("id", 1).execute()
    await call.message.edit_reply_markup(reply_markup=admin_main_kb())

@dp.callback_query(F.data == "adm_toggle_2fa")
async def cb_toggle_2fa(call: CallbackQuery):
    cfg = get_settings()
    supabase.table("bot_settings").update({"enable_2fa": not cfg["enable_2fa"]}).eq("id", 1).execute()
    await call.message.edit_reply_markup(reply_markup=admin_main_kb())

@dp.callback_query(F.data == "adm_toggle_multi")
async def cb_toggle_multi(call: CallbackQuery):
    cfg = get_settings()
    supabase.table("bot_settings").update({"accept_multiple_devices": not cfg["accept_multiple_devices"]}).eq("id", 1).execute()
    await call.message.edit_reply_markup(reply_markup=admin_main_kb())

@dp.callback_query(F.data == "adm_rejected_list")
async def cb_rejected_list(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    res = supabase.table("accounts").select("*").eq("status", "rejected").execute()
    if not res.data: return await call.answer("No rejected accounts!", show_alert=True)
    text = "❌ <b>Rejected Accounts List:</b>\n\n"
    for acc in res.data[:15]:
        text += f"📱 <code>{acc['phone']}</code>\n👤 User: <code>{acc['submitted_by']}</code>\nReason: {acc['reject_reason']}\n──────────────────\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="adm_back")]])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")

# Admin Force Logout Hub + ALL Force Logout Button
@dp.callback_query(F.data == "adm_force_mgr")
async def cb_force_mgr(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    res = supabase.table("accounts").select("*").eq("status", "waiting_logout").execute()
    count = len(res.data) if res.data else 0
    
    buttons = [
        [InlineKeyboardButton(text=f"🚀 Force Logout ALL Waiting Accounts ({count})", callback_data="adforcelog_all")]
    ]
    if res.data:
        for acc in res.data[:10]:
            buttons.append([InlineKeyboardButton(text=f"⚡ Logout {acc['phone']}", callback_data=f"adforcelog_{acc['id']}")])
    buttons.append([InlineKeyboardButton(text="🔙 Back", callback_data="adm_back")])
    
    await call.message.edit_text(
        f"⚡ <b>Force Logout Manager</b>\nWaiting for logout: {count} accounts.\nClick below to force logout all at once or individually:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("adforcelog_"))
async def cb_admin_force_action(call: CallbackQuery):
    target = call.data.split("_")[1]
    
    if target == "all":
        await call.answer("🚀 Processing force logout for ALL waiting accounts...", show_alert=True)
        res = supabase.table("accounts").select("*").eq("status", "waiting_logout").execute()
        if not res.data: return await call.answer("No accounts to logout!", show_alert=True)
        
        success_count = 0
        for acc in res.data:
            client = TelegramClient(
                StringSession(acc["session_string"]), API_ID, API_HASH,
                device_model=acc["device_model"], system_version=acc["system_version"], app_version=acc["app_version"]
            )
            try:
                await client.connect()
                await client(ResetAuthorizationsRequest())
                await client.disconnect()
                await finalize_account_confirmation(acc)
                success_count += 1
            except Exception:
                try: await client.disconnect()
                except: pass
        await call.message.edit_text(f"✅ Successfully force logged out <b>{success_count}</b> accounts!", reply_markup=admin_main_kb(), parse_mode="HTML")
    else:
        acc_id = int(target)
        res = supabase.table("accounts").select("*").eq("id", acc_id).execute()
        if not res.data: return
        acc = res.data[0]
        client = TelegramClient(
            StringSession(acc["session_string"]), API_ID, API_HASH,
            device_model=acc["device_model"], system_version=acc["system_version"], app_version=acc["app_version"]
        )
        try:
            await client.connect()
            await client(ResetAuthorizationsRequest())
            await client.disconnect()
            await finalize_account_confirmation(acc)
            await call.answer("✅ Successfully forced logout and confirmed account!", show_alert=True)
        except Exception as e:
            try: await client.disconnect()
            except: pass
            await call.answer(f"❌ Failed: {e}", show_alert=True)

# --- COUNTRY MANAGEMENT ---
@dp.callback_query(F.data == "adm_countries")
async def cb_adm_countries(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    res = supabase.table("countries").select("*").execute()
    buttons = [[InlineKeyboardButton(text="➕ Add New Country", callback_data="adm_add_country")]]
    for c in res.data:
        st = "✅" if c["is_active"] else "❌"
        buttons.append([
            InlineKeyboardButton(text=f"{st} {c['country_name']} ({c['country_code']}) - ${c['price']} | Cap: {c['capacity']}", callback_data=f"togc_{c['country_code']}"),
            InlineKeyboardButton(text="🗑️", callback_data=f"delc_{c['country_code']}")
        ])
    buttons.append([InlineKeyboardButton(text="🔙 Back", callback_data="adm_back")])
    await call.message.edit_text("🌍 <b>Country & Capacity Manager</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

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
    await call.message.edit_text("Send Country Code (e.g. <code>+57</code>):", parse_mode="HTML")

@dp.message(AdminFlow.add_c_code)
async def step_c_code(message: Message, state: FSMContext):
    code = message.text.strip()
    if not code.startswith("+"): code = "+" + code
    await state.update_data(code=code)
    await state.set_state(AdminFlow.add_c_name)
    await message.answer("Enter Country Name (e.g. <code>Colombia</code>):", parse_mode="HTML")

@dp.message(AdminFlow.add_c_name)
async def step_c_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(AdminFlow.add_c_price)
    await message.answer("Enter Price in USD (e.g. <code>0.25</code>):", parse_mode="HTML")

@dp.message(AdminFlow.add_c_price)
async def step_c_price(message: Message, state: FSMContext):
    await state.update_data(price=float(message.text.strip()))
    await state.set_state(AdminFlow.add_c_cap)
    await message.answer("Enter Capacity limit (e.g. <code>500</code>):", parse_mode="HTML")

@dp.message(AdminFlow.add_c_cap)
async def step_c_cap(message: Message, state: FSMContext):
    await state.update_data(cap=int(message.text.strip()))
    await state.set_state(AdminFlow.add_c_time)
    await message.answer("Enter Confirmation Wait Time in seconds (e.g. <code>600</code>):", parse_mode="HTML")

@dp.message(AdminFlow.add_c_time)
async def step_c_time(message: Message, state: FSMContext):
    await state.update_data(time_sec=int(message.text.strip()))
    await state.set_state(AdminFlow.add_c_proxy)
    await message.answer("Enter SOCKS5 Proxy (<code>ip:port:user:pass</code>) or type <code>none</code>:", parse_mode="HTML")

@dp.message(AdminFlow.add_c_proxy)
async def step_c_proxy(message: Message, state: FSMContext):
    data = await state.get_data()
    proxy = "" if message.text.strip().lower() == "none" else message.text.strip()
    supabase.table("countries").upsert({
        "country_code": data["code"],
        "country_name": data["name"],
        "price": data["price"],
        "capacity": data["cap"],
        "confirm_time_sec": data["time_sec"],
        "proxy_url": proxy,
        "is_active": True
    }).execute()
    await state.clear()
    await message.answer(f"✅ Country <b>{data['name']}</b> saved!", reply_markup=admin_main_kb(), parse_mode="HTML")

# --- EXPORT & BROADCAST & RESTART ---
@dp.callback_query(F.data == "adm_export")
async def cb_adm_export(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    conf = supabase.table("accounts").select("id", count="exact").eq("status", "confirmed").execute().count or 0
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📦 Download Confirmed (Both .session + .json) [{conf}]", callback_data="exp_both_confirmed")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="adm_back")]
    ])
    await call.message.edit_text("📦 <b>Account Exporter</b>", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("exp_"))
async def cb_run_export(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    _, fmt, target_status = call.data.split("_")
    res = supabase.table("accounts").select("*").eq("status", target_status).execute()
    if not res.data: return await call.answer("No accounts!", show_alert=True)

    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for acc in res.data:
            clean_p = acc["phone"].replace("+", "")
            sub_id = acc["submitted_by"]
            prefix = f"User_{sub_id}/{clean_p}"
            j_content = {"phone": acc["phone"], "user_id": acc["tg_user_id"], "twoFA": acc["two_fa"]}
            zf.writestr(f"{prefix}.json", json.dumps(j_content, indent=4))
            supabase.table("accounts").update({"status": "downloaded"}).eq("id", acc["id"]).execute()
    zip_buffer.seek(0)
    await call.message.answer_document(BufferedInputFile(zip_buffer.read(), filename="Accounts.zip"))
    await cb_adm_export(call)

@dp.callback_query(F.data == "adm_broadcast")
async def cb_broadcast(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFlow.broadcast_text)
    await call.message.edit_text("📢 Send broadcast message:")

@dp.message(AdminFlow.broadcast_text)
async def run_broadcast(message: Message, state: FSMContext):
    await state.clear()
    users = supabase.table("users").select("user_id").execute().data
    for u in users:
        try: await bot.send_message(u["user_id"], message.text)
        except: pass
    await message.answer("✅ Broadcast complete!", reply_markup=admin_main_kb())

@dp.callback_query(F.data == "adm_restart")
async def cb_restart(call: CallbackQuery):
    await call.message.edit_text("🔄 Restarting...")
    os.execv(sys.executable, ['python'] + sys.argv)

# ==================== PHONE & OTP HANDLER ====================
@dp.message(UserFlow.waiting_otp)
async def handle_otp_input(message: Message, state: FSMContext):
    uid = message.from_user.id
    if uid not in active_logins:
        await state.clear()
        return await message.answer("⚠️ Session expired! Send number again.")
    code = re.sub(r'\D', '', message.text.strip())
    session_info = active_logins[uid]
    client = session_info["client"]
    phone = session_info["phone"]
    try:
        await client.sign_in(phone=phone, code=code)
        await complete_account_setup(message, state, uid)
    except SessionPasswordNeededError:
        await state.set_state(UserFlow.waiting_2fa)
        await message.answer("🔐 Enter 2FA Password:", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Error: {e}")

@dp.message(UserFlow.waiting_2fa)
async def handle_existing_2fa(message: Message, state: FSMContext):
    uid = message.from_user.id
    if uid not in active_logins:
        await state.clear()
        return
    client = active_logins[uid]["client"]
    try:
        await client.sign_in(password=message.text.strip())
        await complete_account_setup(message, state, uid, old_2fa=message.text.strip())
    except Exception:
        await message.answer("❌ Wrong 2FA password!")

async def complete_account_setup(message: Message, state: FSMContext, uid: int, old_2fa: str = None):
    info = active_logins.pop(uid)
    client = info["client"]
    phone = info["phone"]
    country = info["country"]
    dev = info["device"]
    cfg = get_settings()

    status_msg = await message.answer("⏳ Securing account...", parse_mode="HTML")
    try:
        me = await client.get_me()
        await asyncio.sleep(3)
        await client.send_message("me", cfg.get("saved_msg_text", "Colombia Empire By Mahin"))
        
        final_2fa = old_2fa or ""
        if cfg.get("enable_2fa", True) and cfg.get("two_fa_pass"):
            try:
                await client.edit_2fa(current_password=old_2fa, new_password=cfg["two_fa_pass"], hint="")
                final_2fa = cfg["two_fa_pass"]
            except: pass

        sess_str = client.session.save()
        await client.disconnect()

        ins = supabase.table("accounts").insert({
            "phone": phone, "country_code": country["country_code"], "tg_user_id": me.id,
            "submitted_by": uid, "session_string": sess_str, "two_fa": final_2fa,
            "device_model": dev["device"], "system_version": dev["sdk"], "app_version": dev["app"],
            "price": country["price"], "status": "pending"
        }).execute()
        acc_id = ins.data[0]["id"]

        u_res = supabase.table("users").select("total_submitted").eq("user_id", uid).execute()
        if u_res.data:
            supabase.table("users").update({"total_submitted": u_res.data[0]["total_submitted"] + 1}).eq("user_id", uid).execute()

        await state.clear()
        wait_s = country["confirm_time_sec"]
        await status_msg.edit_text(f"✅ Success! Please log out from your phone. Timer: {wait_s}s", parse_mode="HTML")
        asyncio.create_task(schedule_confirmation_check(acc_id, uid, phone, wait_s))
    except Exception as e:
        await client.disconnect()
        await state.clear()
        await status_msg.edit_text(f"❌ Error: {e}")

@dp.message(F.text)
async def handle_phone_submission(message: Message, state: FSMContext):
    if message.text.startswith("/"): return
    phone = clean_phone(message.text)
    if not phone: return await message.answer("❌ Invalid format!")

    country = match_country(phone)
    if not country: return await message.answer("❌ Country not supported.")

    wait_msg = await message.answer(f"📡 Connecting & sending code...", parse_mode="HTML")
    dev = random.choice(DEVICE_POOL)
    client = TelegramClient(StringSession(), API_ID, API_HASH, device_model=dev["device"], system_version=dev["sdk"], app_version=dev["app"], proxy=parse_proxy(country.get("proxy_url", "")))
    try:
        await client.connect()
        await client.send_code_request(phone)
        active_logins[message.from_user.id] = {"client": client, "phone": phone, "country": country, "device": dev}
        await state.set_state(UserFlow.waiting_otp)
        await wait_msg.edit_text("📩 Enter 5-digit OTP code:")
    except Exception as e:
        await client.disconnect()
        await wait_msg.edit_text(f"❌ Failed: {e}")

async def health_check(request):
    return web.Response(text="Bot is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8080)))
    await site.start()

async def main():
    await bot.set_my_commands([
        BotCommand(command="start", description="Start"),
        BotCommand(command="cancel", description="Cancel"),
        BotCommand(command="capacity", description="Capacity"),
        BotCommand(command="balance", description="Balance"),
        BotCommand(command="withdraw", description="Withdraw"),
        BotCommand(command="help", description="Help")
    ], scope=BotCommandScopeDefault())
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
