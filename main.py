import asyncio
import logging
import os
import re

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler, filters, ContextTypes
import httpx
from pathlib import Path
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

from pyrogram import Client as PyroClient
from pyrogram.types import Message as PyroMessage

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration — all from environment variables
TELEGRAM_TOKEN = os.environ.get('BOT2_TELEGRAM_TOKEN')
TERABOX_API_KEY = os.environ.get('BOT2_TERABOX_API_KEY')
TERABOX_API_BASE = "https://api.playterabox.com/api/proxy"
ADMIN_ID = int(os.environ.get('BOT2_ADMIN_ID', '0'))

# Pyrogram credentials — from https://my.telegram.org
# Required for large file uploads (up to 2 GB via MTProto)
PYRO_API_ID   = int(os.environ.get('BOT2_API_ID', '0'))
PYRO_API_HASH = os.environ.get('BOT2_API_HASH', '')

# Global Pyrogram client — initialised in post_init
pyro_client: PyroClient | None = None

# Download limits
DAILY_LIMIT = 4
REFERRAL_BONUS = 4
RESET_HOURS = 24
VIDEO_DELETE_AFTER_MINUTES = 30

# Conversation states
BROADCAST_TYPE, BROADCAST_TEXT, BROADCAST_MEDIA = range(3)
SUPPORT_MESSAGE, SUPPORT_CONFIRM = range(2)

# MongoDB connection — from environment variable
MONGO_URL = os.environ.get('BOT2_MONGO_URL')
mongo_client = AsyncIOMotorClient(MONGO_URL)
db = mongo_client['terabox_bot']
users_collection = db.users
support_tickets_collection = db.support_tickets

# Regex pattern to detect Terabox URLs
TERABOX_URL_PATTERN = re.compile(
    r'(https?://)?(www\.)?(terabox\.com|1024terabox\.com|teraboxapp\.com|freeterabox\.com|4funbox\.com|terabox\.fun)/s/[a-zA-Z0-9_-]+',
    re.IGNORECASE
)

async def get_or_create_user(user_id: int, username: str = None, referred_by: int = None):
    try:
        user = await users_collection.find_one({"user_id": user_id})
        if not user:
            user = {
                "user_id": user_id,
                "username": username,
                "downloads_count": DAILY_LIMIT,
                "downloads_used": 0,
                "referrals_count": 0,
                "referred_by": referred_by,
                "last_reset": datetime.now(timezone.utc).isoformat(),
                "joined_at": datetime.now(timezone.utc).isoformat()
            }
            await users_collection.insert_one(user)
            if referred_by:
                await users_collection.update_one(
                    {"user_id": referred_by},
                    {"$inc": {"referrals_count": 1, "downloads_count": REFERRAL_BONUS}}
                )
                logger.info(f"User {referred_by} got {REFERRAL_BONUS} bonus downloads for referring {user_id}")
        return user
    except Exception as e:
        logger.error(f"Error in get_or_create_user: {e}")
        return None

async def check_and_reset_limit(user_id: int):
    try:
        user = await users_collection.find_one({"user_id": user_id})
        if not user:
            return
        last_reset = datetime.fromisoformat(user["last_reset"])
        now = datetime.now(timezone.utc)
        if now - last_reset >= timedelta(hours=RESET_HOURS):
            await users_collection.update_one(
                {"user_id": user_id},
                {"$set": {"downloads_count": DAILY_LIMIT, "downloads_used": 0, "last_reset": now.isoformat()}}
            )
            logger.info(f"Reset limit for user {user_id}")
    except Exception as e:
        logger.error(f"Error in check_and_reset_limit: {e}")

async def get_remaining_downloads(user_id: int) -> int:
    try:
        await check_and_reset_limit(user_id)
        user = await users_collection.find_one({"user_id": user_id})
        if not user:
            return DAILY_LIMIT
        total_available = user.get("downloads_count", DAILY_LIMIT)
        used = user.get("downloads_used", 0)
        return max(0, total_available - used)
    except Exception as e:
        logger.error(f"Error in get_remaining_downloads: {e}")
        return 0

async def use_download(user_id: int):
    try:
        await users_collection.update_one(
            {"user_id": user_id},
            {"$inc": {"downloads_used": 1}}
        )
    except Exception as e:
        logger.error(f"Error in use_download: {e}")

async def get_time_until_reset(user_id: int) -> str:
    try:
        user = await users_collection.find_one({"user_id": user_id})
        if not user:
            return "24 hours"
        last_reset = datetime.fromisoformat(user["last_reset"])
        reset_time = last_reset + timedelta(hours=RESET_HOURS)
        now = datetime.now(timezone.utc)
        time_left = reset_time - now
        hours = int(time_left.total_seconds() // 3600)
        minutes = int((time_left.total_seconds() % 3600) // 60)
        return f"{hours}h {minutes}m"
    except Exception as e:
        logger.error(f"Error in get_time_until_reset: {e}")
        return "Unknown"

async def schedule_video_deletion(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    try:
        await asyncio.sleep(VIDEO_DELETE_AFTER_MINUTES * 60)
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ Video deleted after {VIDEO_DELETE_AFTER_MINUTES} minutes due to copyright compliance.\n\n"
                 f"💡 Tip: Save videos immediately after receiving them!"
        )
        logger.info(f"Auto-deleted video message {message_id} from chat {chat_id}")
    except Exception as e:
        logger.error(f"Failed to auto-delete video message {message_id}: {e}")

def get_main_keyboard():
    keyboard = [
        [InlineKeyboardButton("📊 Status", callback_data="status")],
        [InlineKeyboardButton("🎁 Refer & Earn", callback_data="refer")],
        [InlineKeyboardButton("💬 Support", callback_data="support")],
        [InlineKeyboardButton("❓ Help", callback_data="help")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_admin_keyboard():
    keyboard = [
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
        [InlineKeyboardButton("📊 Bot Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("🔙 Back", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_broadcast_keyboard():
    keyboard = [
        [InlineKeyboardButton("📝 Text Only", callback_data="broadcast_text")],
        [InlineKeyboardButton("🖼️ Image + Text", callback_data="broadcast_image")],
        [InlineKeyboardButton("🎥 Video + Text", callback_data="broadcast_video")],
        [InlineKeyboardButton("❌ Cancel", callback_data="broadcast_cancel")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        username = update.effective_user.username
        referred_by = None
        if context.args:
            try:
                referred_by = int(context.args[0])
                if referred_by == user_id:
                    referred_by = None
            except ValueError:
                pass
        user = await get_or_create_user(user_id, username, referred_by)
        if not user:
            await update.message.reply_text("⚠️ Database error. Please try again later.")
            return
        is_new_user = user.get("downloads_used", 0) == 0 and user.get("referrals_count", 0) == 0
        if referred_by and is_new_user:
            welcome_message = f"""
🎉 Welcome to Terabox Downloader Bot!

You joined via referral!
Your friend got {REFERRAL_BONUS} bonus downloads! 🎁

🔥 You can download {DAILY_LIMIT} videos/images every 24 hours!

Supported formats:
📹 Videos (all qualities)
🖼️ Images

Just send me a Terabox link! ✨
            """
        else:
            welcome_message = f"""
👋 Welcome to Terabox Downloader Bot!

🔥 You can download {DAILY_LIMIT} videos/images every 24 hours!

Want more? Invite friends!
Each referral gives you {REFERRAL_BONUS} more downloads! 🎁

Supported formats:
📹 Videos (all qualities)
🖼️ Images

Just send me a Terabox link! ✨
            """
        await update.message.reply_text(welcome_message, reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f"Error in start command: {e}")
        await update.message.reply_text("⚠️ An error occurred. Please try again.")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id

        if query.data == "main_menu":
            await query.edit_message_text("🏠 Main Menu\nChoose an option below:", reply_markup=get_main_keyboard())

        elif query.data == "status":
            await get_or_create_user(user_id, update.effective_user.username)
            remaining = await get_remaining_downloads(user_id)
            user = await users_collection.find_one({"user_id": user_id})
            referrals = user.get("referrals_count", 0) if user else 0
            if remaining > 0:
                status_text = f"""
📊 Your Status:

✅ Downloads Remaining: {remaining}
👥 Total Referrals: {referrals}

💡 Invite friends to get more downloads!
                """
            else:
                time_left = await get_time_until_reset(user_id)
                status_text = f"""
📊 Your Status:

❌ Downloads Remaining: 0
⏰ Limit resets in: {time_left}
👥 Total Referrals: {referrals}

💡 Invite 1 friend to get {REFERRAL_BONUS} more downloads!
                """
            await query.edit_message_text(status_text, reply_markup=get_main_keyboard())

        elif query.data == "refer":
            bot_username = context.bot.username
            user = await get_or_create_user(user_id, update.effective_user.username)
            if not user:
                await query.edit_message_text("⚠️ Database error. Please try again later.")
                return
            referral_link = f"https://t.me/{bot_username}?start={user_id}"
            referrals_count = user.get("referrals_count", 0)
            refer_text = f"""
🎁 Your Referral Link:

{referral_link}

📊 Stats:
• Total Referrals: {referrals_count}
• Bonus Earned: {referrals_count * REFERRAL_BONUS} downloads

💰 Get {REFERRAL_BONUS} downloads per referral!

Share this link with friends! 🚀
            """
            await query.edit_message_text(refer_text, reply_markup=get_main_keyboard())

        elif query.data == "help":
            help_text = f"""
🤖 How to use this bot:

1️⃣ Copy any Terabox link
2️⃣ Send it to me
3️⃣ Receive your video/image!

📊 Download Limits:
• {DAILY_LIMIT} downloads per 24 hours
• Invite friends for {REFERRAL_BONUS} more downloads per referral!

⚠️ Important:
• Videos are auto-deleted after {VIDEO_DELETE_AFTER_MINUTES} minutes due to copyright compliance
• Save videos immediately after receiving!

Example links:
• https://terabox.com/s/xxxxxx
• https://1024terabox.com/s/xxxxxx

Note: Only videos and images are supported.
            """
            await query.edit_message_text(help_text, reply_markup=get_main_keyboard())

        elif query.data == "support":
            support_text = """
💬 Support

Please describe your issue or query related to promotions on our bot.

Type your message below:
            """
            await query.edit_message_text(support_text)
            context.user_data['awaiting_support_message'] = True

        elif query.data == "admin_panel" and user_id == ADMIN_ID:
            await query.edit_message_text("👨‍💼 Admin Panel\nChoose an option:", reply_markup=get_admin_keyboard())

        elif query.data == "admin_broadcast" and user_id == ADMIN_ID:
            await query.edit_message_text("📢 Select broadcast type:", reply_markup=get_broadcast_keyboard())

        elif query.data == "admin_stats" and user_id == ADMIN_ID:
            total_users = await users_collection.count_documents({})
            total_downloads = await users_collection.aggregate([
                {"$group": {"_id": None, "total": {"$sum": "$downloads_used"}}}
            ]).to_list(1)
            total_dl = total_downloads[0]['total'] if total_downloads else 0
            stats_text = f"""
📊 Bot Statistics:

👥 Total Users: {total_users}
🔥 Total Downloads: {total_dl}
🎁 Active Referrals: {await users_collection.count_documents({"referrals_count": {"$gt": 0}})}
            """
            await query.edit_message_text(stats_text, reply_markup=get_admin_keyboard())

    except Exception as e:
        logger.error(f"Error in button_handler: {e}")
        try:
            await query.edit_message_text("⚠️ An error occurred. Please try again.")
        except:
            pass

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if user_id != ADMIN_ID:
            await update.message.reply_text("⛔ You don't have permission to access admin panel.")
            return
        await update.message.reply_text("👨‍💼 Admin Panel\nChoose an option:", reply_markup=get_admin_keyboard())
    except Exception as e:
        logger.error(f"Error in admin_command: {e}")

async def broadcast_text_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        if query:
            await query.answer()
        context.user_data['broadcast_type'] = 'text'
        if query:
            await query.edit_message_text("📝 Send the text message you want to broadcast:")
        else:
            await update.message.reply_text("📝 Send the text message you want to broadcast:")
        return BROADCAST_TEXT
    except Exception as e:
        logger.error(f"Error in broadcast_text_start: {e}")
        return ConversationHandler.END

async def broadcast_image_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        if query:
            await query.answer()
        context.user_data['broadcast_type'] = 'image'
        if query:
            await query.edit_message_text("🖼️ Send the image with caption you want to broadcast:")
        else:
            await update.message.reply_text("🖼️ Send the image with caption you want to broadcast:")
        return BROADCAST_MEDIA
    except Exception as e:
        logger.error(f"Error in broadcast_image_start: {e}")
        return ConversationHandler.END

async def broadcast_video_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        if query:
            await query.answer()
        context.user_data['broadcast_type'] = 'video'
        if query:
            await query.edit_message_text("🎥 Send the video with caption you want to broadcast:")
        else:
            await update.message.reply_text("🎥 Send the video with caption you want to broadcast:")
        return BROADCAST_MEDIA
    except Exception as e:
        logger.error(f"Error in broadcast_video_start: {e}")
        return ConversationHandler.END

async def broadcast_receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.effective_user.id != ADMIN_ID:
            return ConversationHandler.END
        text = update.message.text
        status_msg = await update.message.reply_text("📤 Starting broadcast...")
        users = await users_collection.find({}).to_list(None)
        success = 0
        failed = 0
        for user in users:
            try:
                await context.bot.send_message(chat_id=user['user_id'], text=f"📢 Broadcast Message:\n\n{text}")
                success += 1
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.error(f"Failed to send to {user['user_id']}: {e}")
                failed += 1
        await status_msg.edit_text(f"✅ Broadcast completed!\n\n✅ Successful: {success}\n❌ Failed: {failed}")
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in broadcast_receive_text: {e}")
        return ConversationHandler.END

async def broadcast_receive_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.effective_user.id != ADMIN_ID:
            return ConversationHandler.END
        broadcast_type = context.user_data.get('broadcast_type', 'image')
        caption = update.message.caption or ""
        status_msg = await update.message.reply_text("📤 Starting broadcast...")
        users = await users_collection.find({}).to_list(None)
        success = 0
        failed = 0
        for user in users:
            try:
                if broadcast_type == 'image' and update.message.photo:
                    await context.bot.send_photo(
                        chat_id=user['user_id'],
                        photo=update.message.photo[-1].file_id,
                        caption=f"📢 Broadcast:\n\n{caption}"
                    )
                elif broadcast_type == 'video' and update.message.video:
                    await context.bot.send_video(
                        chat_id=user['user_id'],
                        video=update.message.video.file_id,
                        caption=f"📢 Broadcast:\n\n{caption}"
                    )
                success += 1
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.error(f"Failed to send to {user['user_id']}: {e}")
                failed += 1
        await status_msg.edit_text(f"✅ Broadcast completed!\n\n✅ Successful: {success}\n❌ Failed: {failed}")
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in broadcast_receive_media: {e}")
        return ConversationHandler.END

async def broadcast_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        if query:
            await query.answer()
            await query.edit_message_text("❌ Broadcast cancelled.", reply_markup=get_admin_keyboard())
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in broadcast_cancel: {e}")
        return ConversationHandler.END

async def support_receive_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not context.user_data.get('awaiting_support_message'):
            return
        user_id = update.effective_user.id
        username = update.effective_user.username or "No username"
        message = update.message.text
        context.user_data['support_message'] = message
        context.user_data['awaiting_support_message'] = False
        keyboard = [
            [InlineKeyboardButton("✅ Confirm & Send", callback_data="support_confirm")],
            [InlineKeyboardButton("❌ Cancel", callback_data="support_cancel")]
        ]
        await update.message.reply_text(
            f"📝 Your message:\n\n{message}\n\nConfirm to send this to bot owner?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(f"Error in support_receive_message: {e}")

async def support_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id
        username = update.effective_user.username or "No username"
        message = context.user_data.get('support_message', '')
        ticket = {
            "user_id": user_id,
            "username": username,
            "message": message,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending"
        }
        await support_tickets_collection.insert_one(ticket)
        admin_message = f"""
🎫 New Support Ticket

👤 User: @{username}
🆔 User ID: {user_id}

📝 Message:
{message}

Reply using: /replyuser {user_id} [your response]
        """
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=admin_message)
            await query.edit_message_text(
                "✅ Your message has been sent to the bot owner!\nYou will receive a response soon.",
                reply_markup=get_main_keyboard()
            )
        except Exception as e:
            logger.error(f"Failed to send support ticket: {e}")
            await query.edit_message_text("❌ Failed to send message. Please try again later.", reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f"Error in support_confirm: {e}")

async def support_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        await query.answer()
        context.user_data['awaiting_support_message'] = False
        context.user_data['support_message'] = None
        await query.edit_message_text("❌ Support request cancelled.", reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f"Error in support_cancel: {e}")

async def replyuser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("⛔ You don't have permission to use this command.")
            return
        if update.message.photo:
            if len(context.args) < 1:
                await update.message.reply_text("Usage: /replyuser [user_id] (as caption with image)")
                return
            try:
                target_user_id = int(context.args[0])
                caption = update.message.caption
                response_text = ' '.join(caption.split()[1:]) if caption else "Response from admin"
                await context.bot.send_photo(
                    chat_id=target_user_id,
                    photo=update.message.photo[-1].file_id,
                    caption=f"💬 Response from Admin:\n\n{response_text}"
                )
                await update.message.reply_text(f"✅ Reply sent to user {target_user_id}")
            except ValueError:
                await update.message.reply_text("❌ Invalid user ID")
            except Exception as e:
                await update.message.reply_text(f"❌ Failed to send reply: {e}")
        else:
            if len(context.args) < 2:
                await update.message.reply_text("Usage: /replyuser [user_id] [message]")
                return
            try:
                target_user_id = int(context.args[0])
                response_text = ' '.join(context.args[1:])
                await context.bot.send_message(
                    chat_id=target_user_id,
                    text=f"💬 Response from Admin:\n\n{response_text}"
                )
                await update.message.reply_text(f"✅ Reply sent to user {target_user_id}")
            except ValueError:
                await update.message.reply_text("❌ Invalid user ID")
            except Exception as e:
                await update.message.reply_text(f"❌ Failed to send reply: {e}")
    except Exception as e:
        logger.error(f"Error in replyuser_command: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# API + Download helpers
# ─────────────────────────────────────────────────────────────────────────────

def parse_duration_seconds(duration_str: str) -> int:
    """
    Convert a duration string to integer seconds.
    Handles formats: "MM:SS", "HH:MM:SS", plain integer strings.
    Returns 0 if parsing fails.
    """
    if not duration_str:
        return 0
    try:
        parts = str(duration_str).strip().split(":")
        if len(parts) == 2:           # MM:SS
            return int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3:         # HH:MM:SS
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        else:
            return int(duration_str)  # already seconds
    except (ValueError, AttributeError):
        return 0


def thumbnail_filename(base_name: str, thumbnail_url: str) -> str:
    """
    Derive the correct local filename for a thumbnail by inspecting the URL.
    Falls back to .jpg if the extension cannot be determined.
    """
    import urllib.parse
    try:
        path = urllib.parse.urlparse(thumbnail_url).path
        ext = os.path.splitext(path)[1].lower()   # e.g. ".webp", ".jpg", ""
        if ext not in (".jpg", ".jpeg", ".png", ".webp"):
            ext = ".jpg"
    except Exception:
        ext = ".jpg"
    return f"thumb_{base_name}{ext}"


async def extract_terabox_info(url: str):
    """Call the Terabox API and return the parsed JSON, or None on failure."""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            params = {"secret": TERABOX_API_KEY, "url": url}
            response = await client.get(TERABOX_API_BASE, params=params)
            response.raise_for_status()
            data = response.json()
            logger.info(f"API response for {url}: status={data.get('status')}, files={len(data.get('list', []))}")
            if data.get("status") == "success" and data.get("list"):
                return data
            else:
                logger.error(f"API returned non-success or empty list: {data}")
                return None
    except Exception as e:
        logger.error(f"Error calling Terabox API: {e}")
        return None


async def download_file_streaming(url: str, filename: str) -> str | None:
    """
    Download a file using chunked streaming so large files don't exhaust memory.
    Uses per-chunk timeout so a stalled CDN connection is detected quickly.
    Returns the local path on success, None on failure.
    """
    CHUNK_SIZE      = 256 * 1024   # 256 KB per chunk
    CHUNK_TIMEOUT   = 30           # seconds to wait for a single chunk
    LOG_EVERY_BYTES = 10 * 1024 * 1024  # log progress every 10 MB

    temp_dir = Path("/tmp/terabox_downloads")
    temp_dir.mkdir(exist_ok=True)
    file_path = temp_dir / filename

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "*/*",
    }

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30.0, read=CHUNK_TIMEOUT, write=30.0, pool=10.0),
            follow_redirects=True,
            headers=headers,
        ) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                total      = int(response.headers.get("content-length", 0))
                total_mb   = total / (1024 * 1024)
                downloaded = 0
                next_log   = LOG_EVERY_BYTES
                logger.info(f"⬇️  Starting download: {filename} ({total_mb:.1f} MB)")

                with open(file_path, "wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=CHUNK_SIZE):
                        await asyncio.to_thread(f.write, chunk)
                        downloaded += len(chunk)
                        if downloaded >= next_log:
                            pct = (downloaded / total * 100) if total else 0
                            logger.info(
                                f"⬇️  {filename}: {downloaded/(1024*1024):.1f}/{total_mb:.1f} MB ({pct:.0f}%)"
                            )
                            next_log += LOG_EVERY_BYTES

        logger.info(f"✅ Download complete: {filename} ({downloaded/(1024*1024):.1f} MB)")
        return str(file_path)
    except Exception as e:
        logger.error(f"❌ Download failed for '{filename}': {e}")
        if file_path.exists():
            file_path.unlink(missing_ok=True)
        return None


async def send_video_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE,
                              file_info: dict, user_id: int, status_message) -> bool:
    """
    Send a video to the user.

    Upload path (chosen automatically):
      • Pyrogram available  → MTProto upload, up to 2 GB, no size limit
      • Pyrogram not set up → PTB HTTP Bot API, hard limit 50 MB

    For very large files with no Pyrogram, falls back to a download button.
    Direct URL-to-Telegram is intentionally skipped — Cloudflare Workers
    block Telegram's IP ranges, wasting time and expiring the token.
    """
    TELEGRAM_PTB_MAX = 50 * 1024 * 1024   # 50 MB — PTB / HTTP Bot API limit

    file_name     = file_info.get("name", "video.mp4")
    file_size     = file_info.get("size_formatted", "Unknown size")
    file_bytes    = file_info.get("size", 0)
    duration_str  = file_info.get("duration", "")
    quality       = file_info.get("quality", "")
    thumbnail_url = file_info.get("thumbnail")
    chat_id       = update.effective_chat.id

    # fast_stream_url is a dict of HLS m3u8 qualities — never use as a direct URL
    raw_fast = file_info.get("fast_stream_url")
    fast_stream_str = raw_fast if isinstance(raw_fast, str) else None

    # Build ordered list of candidate URLs to try (normal_dlink first, stream_url as fallback)
    candidate_urls = [u for u in [
        file_info.get("normal_dlink"),
        file_info.get("stream_url"),
        fast_stream_str,
        file_info.get("zip_dlink"),
        file_info.get("download_link"),
    ] if u]

    # Pick a button URL (the freshest direct link) for the download button fallback
    download_url = candidate_urls[0] if candidate_urls else None

    if not candidate_urls:
        logger.error(f"No download link for {file_name}. Keys: {list(file_info.keys())}")
        await update.message.reply_text(f"❌ Could not get download link for {file_name}")
        return False

    # Duration as string for display, integer seconds for Pyrogram
    duration_secs = parse_duration_seconds(duration_str)

    caption = f"📹 {file_name}\n📦 Size: {file_size}"
    if quality:
        caption += f"\n🎬 Quality: {quality}"
    if duration_str:
        caption += f"\n⏱️ Duration: {duration_str}"
    caption += f"\n\n⚠️ Video will be deleted in {VIDEO_DELETE_AFTER_MINUTES} minutes due to copyright compliance. Save it now!"

    # ── Case 1: No Pyrogram + file > 50 MB → send download button ────────────
    if not pyro_client and file_bytes > TELEGRAM_PTB_MAX:
        logger.info(f"{file_name} is {file_size} > 50 MB and Pyrogram not available — sending button")
        detail = f"📹 {file_name}\n📦 {file_size}"
        if quality:
            detail += f"\n🎬 {quality}"
        if duration_str:
            detail += f"\n⏱️ {duration_str}"
        await status_message.edit_text(
            f"⚠️ File is larger than 50 MB.\n\n{detail}\n\n👇 Download directly:"
        )
        await update.message.reply_text(
            f"🎬 *{file_name}*\n📦 {file_size}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬇️ Download Video", url=download_url)]
            ])
        )
        return True

    # ── Download file to disk (streaming chunks, retry with each candidate URL) ─
    logger.info(f"Downloading {file_name} ({file_size}), {len(candidate_urls)} URL(s) to try")
    await status_message.edit_text(f"⬇️ Downloading {file_name} ({file_size})...")

    local_path = None
    for attempt, url in enumerate(candidate_urls, 1):
        logger.info(f"Download attempt {attempt}/{len(candidate_urls)}: {url[:60]}...")
        local_path = await download_file_streaming(url, file_name)
        if local_path:
            break
        logger.warning(f"Attempt {attempt} failed, trying next URL..." if attempt < len(candidate_urls) else "All URLs failed.")

    if not local_path:
        await update.message.reply_text(
            f"❌ Download failed for {file_name} after {len(candidate_urls)} attempt(s).\n"
            f"The links may have expired — please try again.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬇️ Download Manually", url=download_url)]
            ])
        )
        return False

    thumbnail_path = None
    if thumbnail_url:
        # Preserve the real extension (.webp, .jpg, etc.) so Telegram accepts it
        thumb_fname = thumbnail_filename(file_name, thumbnail_url)
        thumbnail_path = await download_file_streaming(thumbnail_url, thumb_fname)

    await status_message.edit_text(f"📤 Uploading {file_name}...")

    # ── Case 2: Pyrogram available → MTProto upload (up to 2 GB) ─────────────
    if pyro_client:
        try:
            logger.info(f"Uploading {file_name} via Pyrogram MTProto")
            sent: PyroMessage = await pyro_client.send_video(
                chat_id=chat_id,
                video=local_path,
                caption=caption,
                thumb=thumbnail_path,
                duration=duration_secs or None,
                supports_streaming=True,
                progress=lambda current, total: None,  # silent progress
            )
            asyncio.create_task(schedule_video_deletion(context, chat_id, sent.id))
            logger.info(f"✅ Sent {file_name} via Pyrogram ({file_size})")
            return True
        except Exception as e:
            logger.error(f"Pyrogram upload failed for {file_name}: {e}")
            # fall through to PTB if within size limit
            if file_bytes > TELEGRAM_PTB_MAX:
                await update.message.reply_text(
                    f"❌ Upload failed for {file_name}: {e}\n\nTry the direct download:",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⬇️ Download Video", url=download_url)]
                    ])
                )
                return False

    # ── Case 3: PTB upload (≤ 50 MB) ─────────────────────────────────────────
    try:
        logger.info(f"Uploading {file_name} via PTB")
        thumb_file = open(thumbnail_path, "rb") if thumbnail_path else None
        with open(local_path, "rb") as video_file:
            sent_message = await update.message.reply_video(
                video=video_file,
                caption=caption,
                filename=file_name,
                supports_streaming=True,
                thumbnail=thumb_file,
                read_timeout=300,
                write_timeout=300,
                connect_timeout=30,
            )
        if thumb_file:
            thumb_file.close()
        asyncio.create_task(schedule_video_deletion(context, user_id, sent_message.message_id))
        logger.info(f"✅ Sent {file_name} via PTB")
        return True
    except Exception as e:
        logger.error(f"PTB upload failed for {file_name}: {e}")
        try:
            await update.message.reply_text(
                f"❌ Upload failed for {file_name}.\n\nDownload directly:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬇️ Download Video", url=download_url)]
                ])
            )
        except:
            pass
        return False
    finally:
        if local_path and os.path.exists(local_path):
            os.remove(local_path)
        if thumbnail_path and os.path.exists(thumbnail_path):
            os.remove(thumbnail_path)


async def send_image_to_user(update: Update, file_info: dict, status_message) -> bool:
    """
    Send an image to the user.
    Downloads via streaming chunks then uploads to Telegram.
    Direct URL is intentionally skipped — CDN workers block Telegram's IPs.
    """
    file_name = file_info.get("name", "image.jpg")
    file_size = file_info.get("size_formatted", "Unknown size")

    download_url = (
        file_info.get("normal_dlink") or
        file_info.get("zip_dlink") or
        file_info.get("download_link") or
        file_info.get("fast_download_link")
    )

    if not download_url:
        logger.error(f"No download link for image {file_name}")
        await update.message.reply_text(f"❌ Could not get download link for {file_name}")
        return False

    caption = f"🖼️ {file_name}\n📦 Size: {file_size}"

    await status_message.edit_text(f"⬇️ Downloading {file_name}...")
    local_path = await download_file_streaming(download_url, file_name)
    if not local_path:
        await update.message.reply_text(
            f"❌ Failed to download {file_name}\n"
            f"Try again — the download link may have expired."
        )
        return False

    await status_message.edit_text(f"📤 Uploading {file_name}...")
    try:
        if pyro_client:
            logger.info(f"Uploading image {file_name} via Pyrogram")
            await pyro_client.send_photo(
                chat_id=update.effective_chat.id,
                photo=local_path,
                caption=caption,
            )
        else:
            logger.info(f"Uploading image {file_name} via PTB")
            with open(local_path, "rb") as image_file:
                await update.message.reply_photo(
                    photo=image_file,
                    caption=caption,
                    filename=file_name,
                    read_timeout=120,
                    write_timeout=120,
                )
        logger.info(f"✅ Sent image {file_name} successfully")
        return True
    except Exception as e:
        logger.error(f"Upload failed for image {file_name}: {e}")
        await update.message.reply_text(f"❌ Failed to send {file_name}: {e}")
        return False
    finally:
        if local_path and os.path.exists(local_path):
            os.remove(local_path)


# ─────────────────────────────────────────────────────────────────────────────
# Core download processor — runs as an independent asyncio task per user
# ─────────────────────────────────────────────────────────────────────────────

async def process_download(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            terabox_url: str, user_id: int, status_message):
    """
    Heavy-lifting coroutine: call API → iterate files → send to user.
    Runs as an independent asyncio.Task so it never blocks other users.
    """
    try:
        api_response = await extract_terabox_info(terabox_url)

        if not api_response or not api_response.get("list"):
            await status_message.edit_text(
                "❌ Failed to fetch file information. Please check the link and try again."
            )
            return

        files = api_response.get("list", [])
        total_files = len(files)

        if total_files == 0:
            await status_message.edit_text("❌ No files found in the provided link.")
            return

        await status_message.edit_text(f"🔥 Found {total_files} file(s). Processing...")

        files_sent = 0

        for idx, file_info in enumerate(files, 1):
            file_type = file_info.get("type", "").lower()
            file_name = file_info.get("name", "unknown")

            logger.info(
                f"[User {user_id}] File {idx}/{total_files}: "
                f"name={file_name}, type={file_type}"
            )

            if file_type == "video":
                success = await send_video_to_user(update, context, file_info, user_id, status_message)
                if success:
                    files_sent += 1

            elif file_type == "image":
                success = await send_image_to_user(update, file_info, status_message)
                if success:
                    files_sent += 1

            else:
                logger.info(f"Skipping file {file_name} (unsupported type: {file_type})")

        if files_sent > 0:
            await use_download(user_id)
            remaining_after = await get_remaining_downloads(user_id)
            success_message = f"✅ Done! {files_sent} file(s) sent.\n\n📊 Downloads remaining: {remaining_after}"
            if remaining_after == 0:
                time_left = await get_time_until_reset(user_id)
                success_message += f"\n\n⏰ Limit resets in: {time_left}"
                success_message += f"\n💡 Or invite friends to get more downloads!"
            elif remaining_after <= 2:
                success_message += f"\n\n💡 Running low? Invite friends for more!"
            await status_message.edit_text(success_message)
        else:
            await status_message.edit_text(
                "❌ No supported files could be sent. Please check the link or try again."
            )

    except Exception as e:
        logger.error(f"[User {user_id}] Error in process_download: {e}")
        try:
            await status_message.edit_text(
                "❌ An error occurred while processing your request. Please try again."
            )
        except:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Message handler — validates quickly, then fires a task per user
# ─────────────────────────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Handle support flow first
        if context.user_data.get('awaiting_support_message'):
            await support_receive_message(update, context)
            return

        if not update.message or not update.message.text:
            return

        user_id = update.effective_user.id
        username = update.effective_user.username
        message_text = update.message.text

        await get_or_create_user(user_id, username)

        match = TERABOX_URL_PATTERN.search(message_text)
        if not match:
            await update.message.reply_text(
                "❌ Please send a valid Terabox link.\n\nExample: https://terabox.com/s/xxxxx",
                reply_markup=get_main_keyboard()
            )
            return

        remaining = await get_remaining_downloads(user_id)
        if remaining <= 0:
            time_left = await get_time_until_reset(user_id)
            limit_text = f"""
⛔ Download Limit Reached!

Your daily limit is exhausted.

Options:
1️⃣ Wait {time_left} for automatic reset
2️⃣ Invite 1 friend to get {REFERRAL_BONUS} more downloads instantly!

Use /status to check your current status.
            """
            await update.message.reply_text(limit_text, reply_markup=get_main_keyboard())
            return

        terabox_url = match.group(0)
        if not terabox_url.startswith('http'):
            terabox_url = 'https://' + terabox_url

        logger.info(f"[User {user_id}] Queuing download for: {terabox_url}")

        # Send an immediate acknowledgement so the user knows we got their request
        status_message = await update.message.reply_text("⏳ Processing your request...")

        # Fire-and-forget: each user's download runs as its own asyncio task.
        # This means multiple users are handled fully in parallel — one slow
        # download can never block another user's request.
        asyncio.create_task(
            process_download(update, context, terabox_url, user_id, status_message)
        )

    except Exception as e:
        logger.error(f"Error in handle_message: {e}")
        try:
            if update.message:
                await update.message.reply_text("⚠️ An error occurred. Please try again.")
        except:
            pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    try:
        logger.error(f"Update {update} caused error {context.error}", exc_info=context.error)
        if isinstance(update, Update) and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "⚠️ An error occurred. The bot is still running. Please try again."
                )
            except:
                pass
    except Exception as e:
        logger.error(f"Error in error_handler: {e}")

async def keep_alive_ping(application: Application):
    logger.info("⏰ Keep-alive ping task started")
    while True:
        try:
            await asyncio.sleep(60)
            try:
                bot_info = await application.bot.get_me()
                logger.info(f"💚 Keep-alive: Bot @{bot_info.username} is active")
            except Exception as bot_error:
                logger.warning(f"⚠️ Keep-alive: Bot ping failed: {bot_error}")
            try:
                await users_collection.find_one({})
                logger.info("💾 Keep-alive: MongoDB connection active")
            except Exception as db_error:
                logger.warning(f"⚠️ Keep-alive: MongoDB check failed: {db_error}")
        except asyncio.CancelledError:
            logger.info("Keep-alive ping task cancelled")
            break
        except Exception as e:
            logger.error(f"❌ Keep-alive ping error: {e}")
            await asyncio.sleep(30)

async def post_init(application: Application):
    global pyro_client
    try:
        asyncio.create_task(keep_alive_ping(application))

        # Start Pyrogram client for large-file (up to 2 GB) MTProto uploads
        if PYRO_API_ID and PYRO_API_HASH and TELEGRAM_TOKEN:
            try:
                pyro_client = PyroClient(
                    name="terabox_bot",
                    api_id=PYRO_API_ID,
                    api_hash=PYRO_API_HASH,
                    bot_token=TELEGRAM_TOKEN,
                    in_memory=True,        # no session file on disk
                    no_updates=True,       # PTB handles updates; Pyrogram only sends
                )
                await pyro_client.start()
                me = await pyro_client.get_me()
                logger.info(f"✅ Pyrogram client started as @{me.username} — 2 GB uploads enabled")
            except Exception as pe:
                logger.error(f"❌ Pyrogram failed to start: {pe} — falling back to PTB (50 MB limit)")
                pyro_client = None
        else:
            logger.warning(
                "⚠️ BOT2_API_ID / BOT2_API_HASH not set — "
                "Pyrogram disabled, falling back to PTB (50 MB limit). "
                "Get credentials from https://my.telegram.org"
            )

        logger.info("🚀 Background tasks (web server + keep-alive) started")
    except Exception as e:
        logger.error(f"Error in post_init: {e}")


async def post_shutdown(application: Application):
    global pyro_client
    if pyro_client:
        try:
            await pyro_client.stop()
            logger.info("Pyrogram client stopped")
        except Exception as e:
            logger.error(f"Error stopping Pyrogram client: {e}")

def main():
    logger.info("Starting Terabox Bot...")
    try:
        application = (
            Application.builder()
            .token(TELEGRAM_TOKEN)
            .concurrent_updates(True)
            .post_init(post_init)
            .post_shutdown(post_shutdown)
            .build()
        )

        broadcast_conv_handler = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(broadcast_text_start, pattern="^broadcast_text$"),
                CallbackQueryHandler(broadcast_image_start, pattern="^broadcast_image$"),
                CallbackQueryHandler(broadcast_video_start, pattern="^broadcast_video$"),
            ],
            states={
                BROADCAST_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_receive_text)],
                BROADCAST_MEDIA: [
                    MessageHandler(filters.PHOTO, broadcast_receive_media),
                    MessageHandler(filters.VIDEO, broadcast_receive_media)
                ],
            },
            fallbacks=[CallbackQueryHandler(broadcast_cancel, pattern="^broadcast_cancel$")],
            per_user=True,
        )

        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("admin", admin_command))
        application.add_handler(CommandHandler("replyuser", replyuser_command))
        application.add_handler(broadcast_conv_handler)
        application.add_handler(CallbackQueryHandler(support_confirm, pattern="^support_confirm$"))
        application.add_handler(CallbackQueryHandler(support_cancel, pattern="^support_cancel$"))
        application.add_handler(CallbackQueryHandler(button_handler))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        application.add_error_handler(error_handler)

        logger.info("🤖 Bot is running. Press Ctrl+C to stop.")
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

    except Exception as e:
        logger.error(f"Fatal error in main: {e}")

if __name__ == '__main__':
    main()
